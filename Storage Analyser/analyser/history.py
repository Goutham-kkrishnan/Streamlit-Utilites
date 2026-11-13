"""Scan snapshots on local disk, so you can see what grew or shrank between scans.

Each snapshot is a small JSON file in the app's `snapshots/` folder (owner-only
permissions) with folder totals down to a fixed depth and partition usage.

Nothing here depends on where the app lives: the folder is found relative to this
file, and each snapshot is tagged with an anonymous ID of the computer that made it.
If the app folder is copied to another machine, each machine sees only its own history.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
from datetime import datetime
from pathlib import Path

from .scanner import ScanResult
from .system import IS_WINDOWS, is_writable, user_data_dir
from .utils import MB, display_path

APP_DIR = Path(__file__).resolve().parent.parent
SUMMARY_DEPTH = 4
SUMMARY_MIN_BYTES = 1 * MB
MAX_PER_ROOT = 40


def _snapshot_dir() -> Path:
    """`snapshots/` next to the app; if the app folder is read-only, the user's data folder."""
    local = APP_DIR / "snapshots"
    if is_writable(str(local if local.is_dir() else APP_DIR)):
        return local
    return Path(user_data_dir()) / "snapshots"


def _windows_machine_guid() -> str:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            return str(winreg.QueryValueEx(key, "MachineGuid")[0])
    except (ImportError, OSError):
        return ""


def _linux_machine_id() -> str:
    for f in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(f, encoding="ascii") as fh:
                raw = fh.read().strip()
        except (OSError, ValueError):
            continue
        if raw:
            return raw
    return ""


def _machine_id() -> str:
    """Stable, anonymous ID for this computer (a hash, so the real machine ID is never stored)."""
    raw = (_windows_machine_guid() if IS_WINDOWS else _linux_machine_id()) or socket.gethostname()
    return hashlib.sha256(f"storage-analyser:{raw}".encode()).hexdigest()[:16]


SNAP_DIR = _snapshot_dir()
MACHINE = _machine_id()


def _depth(path: str, root: str) -> int:
    if path == root:
        return 0
    return path.count(os.sep) - root.rstrip(os.sep).count(os.sep)


def dir_summary(res: ScanResult, depth: int = SUMMARY_DEPTH, min_bytes: int = SUMMARY_MIN_BYTES) -> dict:
    """{relative_path: [on_disk, apparent, files]} for folders down to `depth` ('/'-separated on every OS)."""
    out = {}
    root = res.root
    for d, alloc in res.tot_alloc.items():
        if alloc < min_bytes and d != root:
            continue
        dep = _depth(d, root)
        if dep > depth:
            continue
        rel = "." if d == root else os.path.relpath(d, root).replace(os.sep, "/")
        out[rel] = [alloc, res.tot_size[d], res.tot_files[d]]
    return out


def _slug(root: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", root).strip("_") or "root"


def save_snapshot(res: ScanResult, partitions: list[dict], summary: dict | None = None) -> Path:
    SNAP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = {
        "version": 1,
        "machine": MACHINE,
        "host": socket.gethostname(),
        "root": res.root,
        "scanned_at": res.scanned_at,
        "one_filesystem": res.one_filesystem,
        "total_alloc": res.tot_alloc.get(res.root, 0),
        "total_size": res.tot_size.get(res.root, 0),
        "file_count": res.file_count,
        "dir_count": res.dir_count,
        "partitions": [{k: p[k] for k in ("mountpoint", "device", "total", "used", "free")} for p in partitions],
        "dirs": summary if summary is not None else dir_summary(res),
    }
    stamp = datetime.fromtimestamp(res.scanned_at).strftime("%Y%m%d-%H%M%S")
    path = SNAP_DIR / f"{stamp}_{_slug(res.root)}.json"
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass  # e.g. a FAT/exFAT USB stick, which has no Unix permissions
    os.replace(tmp, path)
    _prune(res.root)
    return path


def _prune(root: str) -> None:
    mine = [s for s in list_snapshots() if s["root"] == root]
    for old in mine[:-MAX_PER_ROOT]:
        try:
            os.remove(old["file"])
        except OSError:
            pass


def list_snapshots(other_machines: bool = False) -> list[dict]:
    """Snapshot metadata, oldest first. Only this computer's, unless `other_machines`.

    Snapshots saved before machine tagging have no ID and count as this computer's.
    """
    if not SNAP_DIR.is_dir():
        return []
    out = []
    for f in SNAP_DIR.glob("*.json"):
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        here = data.get("machine", MACHINE) == MACHINE
        if not (here or other_machines):
            continue
        out.append({
            "this_machine": here,
            "host": data.get("host", ""),
            "file": str(f),
            "root": data.get("root", "?"),
            "scanned_at": data.get("scanned_at", 0),
            "total_alloc": data.get("total_alloc", 0),
            "total_size": data.get("total_size", 0),
            "file_count": data.get("file_count", 0),
            "partitions": data.get("partitions", []),
        })
    out.sort(key=lambda s: s["scanned_at"])
    return out


def load_dirs(file: str) -> dict:
    with open(file, encoding="utf-8") as fh:
        return json.load(fh).get("dirs", {})


def compare(current: dict, previous: dict, metric: str, depth: int) -> list[dict]:
    """Per-folder change between two summaries, limited to folders at exactly `depth`."""
    idx = 0 if metric == "alloc" else 1
    rows = []
    for rel in set(current) | set(previous):
        dep = 0 if rel == "." else rel.count("/") + 1
        if dep != depth:
            continue
        before = previous.get(rel, [0, 0, 0])[idx]
        after = current.get(rel, [0, 0, 0])[idx]
        if before == after:
            continue
        rows.append({"folder": display_path(rel.replace("/", os.sep)), "before": before, "after": after,
                     "change": after - before})
    rows.sort(key=lambda r: abs(r["change"]), reverse=True)
    return rows
