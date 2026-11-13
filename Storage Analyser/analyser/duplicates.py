"""Duplicate-file finder: group by size, then by a partial hash, then by a full hash.

Only files that share an exact byte size are ever read, and only the first and
last 64 KB of each are hashed before the (expensive) full-content pass.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Callable, Optional

CHUNK = 64 * 1024
READ_BLOCK = 1024 * 1024

# progress(bytes_done, bytes_total, stage_label)
ProgressFn = Callable[[int, int, str], None]


@dataclass
class DuplicateGroup:
    size: int
    digest: str
    paths: list[str]

    @property
    def copies(self) -> int:
        return len(self.paths)

    @property
    def wasted(self) -> int:
        return self.size * (len(self.paths) - 1)


def _partial_hash(path: str, size: int) -> Optional[str]:
    h = hashlib.blake2b(digest_size=16)
    try:
        with open(path, "rb") as fh:
            h.update(fh.read(CHUNK))
            if size > 2 * CHUNK:
                fh.seek(size - CHUNK)
            h.update(fh.read(CHUNK))
    except OSError:
        return None
    return h.hexdigest()


def _full_hash(path: str) -> Optional[str]:
    h = hashlib.blake2b(digest_size=32)
    try:
        with open(path, "rb") as fh:
            while block := fh.read(READ_BLOCK):
                h.update(block)
    except OSError:
        return None
    return h.hexdigest()


def find_duplicates(candidates: dict[int, list[str]], progress: Optional[ProgressFn] = None) -> list[DuplicateGroup]:
    """Return groups of byte-identical files, largest wasted space first."""
    same_size = [(size, paths) for size, paths in candidates.items() if len(paths) > 1]

    # Stage 1: partial hash (first + last 64 KB). Files <= 128 KB are hashed in full here.
    stage1_total = sum(min(size, 2 * CHUNK) * len(paths) for size, paths in same_size)
    done = 0
    buckets: dict[tuple[int, str], list[str]] = {}
    for size, paths in same_size:
        for p in paths:
            digest = _partial_hash(p, size)
            done += min(size, 2 * CHUNK)
            if digest is not None:
                buckets.setdefault((size, digest), []).append(p)
        if progress:
            progress(done, stage1_total, "Comparing file headers")

    groups: list[DuplicateGroup] = []
    needs_full = []
    for (size, digest), paths in buckets.items():
        if len(paths) < 2:
            continue
        if size <= 2 * CHUNK:
            groups.append(DuplicateGroup(size, digest, sorted(paths)))
        else:
            needs_full.append((size, paths))

    # Stage 2: full-content hash for larger files whose headers matched.
    stage2_total = sum(size * len(paths) for size, paths in needs_full)
    done = 0
    for size, paths in needs_full:
        full: dict[str, list[str]] = {}
        for p in paths:
            digest = _full_hash(p)
            done += size
            if digest is not None:
                full.setdefault(digest, []).append(p)
            if progress:
                progress(done, stage2_total, "Hashing full contents")
        for digest, same in full.items():
            if len(same) > 1:
                groups.append(DuplicateGroup(size, digest, sorted(same)))

    groups.sort(key=lambda g: g.wasted, reverse=True)
    return groups


def common_prefix_folder(paths: list[str]) -> str:
    try:
        return os.path.commonpath(paths)
    except ValueError:
        return ""
