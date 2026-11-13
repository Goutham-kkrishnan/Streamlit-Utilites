"""Single-pass filesystem scanner.

Walks a directory tree once (iteratively, never following symlinks) and gathers
everything the UI needs: per-folder totals, large files, extension / age / size
statistics and duplicate candidates. Hard-linked files are counted once, so the
"on disk" totals line up with `du`.

On Windows, junctions and mount points are skipped like symlinks, "on disk" sizes are
estimated from the cluster size, and hard links can't be detected cheaply, so files
hard-linked into several folders (mostly C:\\Windows\\WinSxS) count once per folder.
"""
from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from .system import IS_WINDOWS, alloc_bytes, is_redirect
from .utils import AGE_BUCKETS, MB, SIZE_BUCKETS, age_bucket, file_extension, size_bucket

# progress(files_seen, folders_seen, bytes_seen, current_folder)
ProgressFn = Callable[[int, int, int, str], None]

MAX_ERROR_SAMPLES = 300


@dataclass
class FileRecord:
    path: str
    size: int  # apparent size (file length)
    alloc: int  # bytes actually allocated on disk
    mtime: float
    atime: float


@dataclass
class ScanResult:
    root: str
    scanned_at: float
    one_filesystem: bool
    large_file_min: int
    dup_min: int
    duration: float = 0.0

    file_count: int = 0
    dir_count: int = 0
    symlink_count: int = 0
    special_count: int = 0  # sockets, fifos, device nodes
    hardlink_skipped: int = 0
    unreadable_dirs: int = 0
    error_count: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    skipped_mounts: list[str] = field(default_factory=list)

    # Sizes of what sits directly inside each folder (files + the folder entry itself)
    own_alloc: dict[str, int] = field(default_factory=dict)
    own_size: dict[str, int] = field(default_factory=dict)
    own_files: dict[str, int] = field(default_factory=dict)
    # Cumulative sizes (folder + everything below it)
    tot_alloc: dict[str, int] = field(default_factory=dict)
    tot_size: dict[str, int] = field(default_factory=dict)
    tot_files: dict[str, int] = field(default_factory=dict)
    tot_dirs: dict[str, int] = field(default_factory=dict)
    children: dict[str, list[str]] = field(default_factory=dict)

    large_files: list[FileRecord] = field(default_factory=list)
    ext_stats: dict[str, list[int]] = field(default_factory=dict)  # ext -> [count, size, alloc]
    age_mtime: list[list[int]] = field(default_factory=lambda: [[0, 0, 0] for _ in AGE_BUCKETS])
    age_atime: list[list[int]] = field(default_factory=lambda: [[0, 0, 0] for _ in AGE_BUCKETS])
    size_hist: list[list[int]] = field(default_factory=lambda: [[0, 0, 0] for _ in SIZE_BUCKETS])
    dup_candidates: dict[int, list[str]] = field(default_factory=dict)  # size -> paths

    @property
    def id(self) -> str:
        return f"{self.root}@{self.scanned_at:.3f}"

    def sizes(self, metric: str) -> dict[str, int]:
        """Cumulative folder sizes; metric is 'alloc' (on disk) or 'size' (apparent)."""
        return self.tot_alloc if metric == "alloc" else self.tot_size

    def own(self, metric: str) -> dict[str, int]:
        return self.own_alloc if metric == "alloc" else self.own_size

    def total(self, metric: str) -> int:
        return self.sizes(metric).get(self.root, 0)

    def _error(self, path: str, exc: OSError) -> None:
        self.error_count += 1
        if len(self.errors) < MAX_ERROR_SAMPLES:
            self.errors.append((path, exc.strerror or type(exc).__name__))


def scan(
    root: str,
    *,
    one_filesystem: bool = True,
    large_file_min: int = 10 * MB,
    dup_min: int = 1 * MB,
    exclude: Iterable[str] = (),
    progress: Optional[ProgressFn] = None,
    progress_every: float = 0.25,
) -> ScanResult:
    """Scan `root` and return a fully aggregated ScanResult.

    one_filesystem: do not descend into other mounted filesystems (like `du -x`).
    exclude: folders never entered (e.g. /proc, /sys); the root itself is never excluded.
    """
    root = os.path.realpath(os.path.expanduser(root))
    st_root = os.stat(root)
    if not stat.S_ISDIR(st_root.st_mode):
        raise NotADirectoryError(root)

    now = time.time()
    t0 = time.monotonic()
    last_report = t0
    excluded = {os.path.realpath(p) for p in exclude} - {root}

    res = ScanResult(root=root, scanned_at=now, one_filesystem=one_filesystem,
                     large_file_min=large_file_min, dup_min=dup_min)
    own_alloc, own_size, own_files = res.own_alloc, res.own_size, res.own_files
    children = res.children
    ext_stats = res.ext_stats
    age_m, age_a, size_hist = res.age_mtime, res.age_atime, res.size_hist
    large, dups = res.large_files, res.dup_candidates

    root_dev = st_root.st_dev
    # Windows' cached directory entries report st_dev = 0; the walk stays on one volume there
    # anyway because junctions and mount points are never entered.
    check_dev = one_filesystem and not IS_WINDOWS
    seen_inodes: set[tuple[int, int]] = set()
    dir_entry_size: dict[str, tuple[int, int]] = {root: (alloc_bytes(st_root), st_root.st_size)}
    files_seen = bytes_seen = 0
    S_ISREG = stat.S_ISREG

    stack = [root]
    while stack:
        d = stack.pop()
        alloc_here, size_here = dir_entry_size.pop(d, (0, 0))
        nfiles = 0
        kids: list[str] = []
        try:
            with os.scandir(d) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            res.symlink_count += 1
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            path = entry.path
                            if path in excluded:
                                res.skipped_mounts.append(path)
                                continue
                            st = entry.stat(follow_symlinks=False)
                            if is_redirect(st):  # Windows junction or mount point
                                res.symlink_count += 1
                                continue
                            if check_dev and st.st_dev != root_dev:
                                res.skipped_mounts.append(path)
                                continue
                            dir_entry_size[path] = (alloc_bytes(st), st.st_size)
                            kids.append(path)
                            continue
                        st = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        res._error(entry.path, exc)
                        continue

                    if not S_ISREG(st.st_mode):
                        res.special_count += 1
                        continue
                    if st.st_nlink > 1:
                        key = (st.st_dev, st.st_ino)
                        if key in seen_inodes:
                            res.hardlink_skipped += 1
                            continue
                        seen_inodes.add(key)

                    size = st.st_size
                    alloc = alloc_bytes(st)
                    alloc_here += alloc
                    size_here += size
                    nfiles += 1

                    ext = file_extension(entry.name)
                    es = ext_stats.get(ext)
                    if es is None:
                        ext_stats[ext] = [1, size, alloc]
                    else:
                        es[0] += 1
                        es[1] += size
                        es[2] += alloc
                    b = age_m[age_bucket(st.st_mtime, now)]
                    b[0] += 1; b[1] += size; b[2] += alloc  # noqa: E702
                    b = age_a[age_bucket(st.st_atime, now)]
                    b[0] += 1; b[1] += size; b[2] += alloc  # noqa: E702
                    b = size_hist[size_bucket(size)]
                    b[0] += 1; b[1] += size; b[2] += alloc  # noqa: E702

                    if size >= large_file_min or alloc >= large_file_min:
                        large.append(FileRecord(entry.path, size, alloc, st.st_mtime, st.st_atime))
                    if size >= dup_min:
                        dups.setdefault(size, []).append(entry.path)
        except OSError as exc:
            res.unreadable_dirs += 1
            res._error(d, exc)

        own_alloc[d] = alloc_here
        own_size[d] = size_here
        own_files[d] = nfiles
        if kids:
            children[d] = kids
            stack.extend(kids)
        res.file_count += nfiles
        res.dir_count += 1
        files_seen += nfiles
        bytes_seen += alloc_here

        if progress is not None:
            t = time.monotonic()
            if t - last_report >= progress_every:
                last_report = t
                progress(files_seen, res.dir_count, bytes_seen, d)

    _aggregate(res)
    large.sort(key=lambda f: max(f.size, f.alloc), reverse=True)
    res.duration = time.monotonic() - t0
    if progress is not None:
        progress(files_seen, res.dir_count, bytes_seen, root)
    return res


def _aggregate(res: ScanResult) -> None:
    """Roll every folder's own totals up into all of its ancestors."""
    tot_alloc = dict(res.own_alloc)
    tot_size = dict(res.own_size)
    tot_files = dict(res.own_files)
    tot_dirs = dict.fromkeys(res.own_alloc, 0)
    root = res.root
    dirname = os.path.dirname
    # Deepest folders first, so each child is complete before it is added to its parent.
    sep = os.sep
    for d in sorted(res.own_alloc, key=lambda p: p.count(sep), reverse=True):
        if d == root:
            continue
        parent = dirname(d)
        tot_alloc[parent] += tot_alloc[d]
        tot_size[parent] += tot_size[d]
        tot_files[parent] += tot_files[d]
        tot_dirs[parent] += tot_dirs[d] + 1
    res.tot_alloc, res.tot_size, res.tot_files, res.tot_dirs = tot_alloc, tot_size, tot_files, tot_dirs


def list_folder_files(folder: str, limit: int = 50) -> list[FileRecord]:
    """Largest regular files directly inside `folder` (live, not from the scan)."""
    out: list[FileRecord] = []
    try:
        with os.scandir(folder) as it:
            for entry in it:
                try:
                    if entry.is_file(follow_symlinks=False):
                        st = entry.stat(follow_symlinks=False)
                        out.append(FileRecord(entry.path, st.st_size, alloc_bytes(st), st.st_mtime, st.st_atime))
                except OSError:
                    continue
    except OSError:
        return []
    out.sort(key=lambda f: f.alloc, reverse=True)
    return out[:limit]
