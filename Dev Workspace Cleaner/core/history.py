"""This app's own history: the only data the app ever writes.

Records live in history/ inside the app folder (or the user's data folder if the app folder is
read-only). Each one is tagged with an anonymous ID of the computer that made it, so a copied app
folder never mixes two machines. The sidebar's "Record history" switch turns recording off.

  save(kind, data) / load(kind)  small JSON snapshots (one file each), oldest pruned
  TimeSeries(name, columns)       frequent numeric samples in a SQLite file, old rows pruned
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .system import APP_DIR, IS_WINDOWS, is_writable, user_data_dir

KEEP_PER_KIND = 400


def _history_dir() -> Path:
    override = os.environ.get("SU_HISTORY_DIR")  # tests point this at a scratch folder
    if override:
        return Path(override)
    local = Path(APP_DIR) / "history"
    if is_writable(str(local if local.is_dir() else APP_DIR)):
        return local
    return Path(user_data_dir()) / "history"


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
    return hashlib.sha256(f"streamlit-utilities:{raw}".encode()).hexdigest()[:16]


HISTORY_DIR = _history_dir()
MACHINE = _machine_id()
HOST = socket.gethostname()


def _make_private_dir(path: Path) -> None:
    """Create a folder readable only by this user (where the filesystem supports it)."""
    if not path.is_dir():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)


def _kind_dir(kind: str) -> Path:
    if not re.fullmatch(r"[a-z0-9_-]+", kind):
        raise ValueError(f"bad history kind: {kind!r}")
    return HISTORY_DIR / kind


def save(kind: str, data: dict, keep: int = KEEP_PER_KIND) -> Path:
    """Store one snapshot of `data` (JSON-serialisable) under `kind`; keeps the newest `keep` per computer."""
    folder = _kind_dir(kind)
    _make_private_dir(HISTORY_DIR)
    _make_private_dir(folder)
    now = time.time()
    record = {"version": 1, "kind": kind, "machine": MACHINE, "host": HOST, "saved_at": now, "data": data}
    path = folder / f"{datetime.fromtimestamp(now).strftime('%Y%m%d-%H%M%S-%f')}_{MACHINE[:8]}.json"
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(record, fh)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass  # e.g. FAT/exFAT, which has no Unix permissions
    os.replace(tmp, path)
    for old in load(kind)[:-keep]:
        try:
            os.remove(old["file"])
        except OSError:
            pass
    return path


def load(kind: str, other_machines: bool = False) -> list[dict]:
    """Snapshots of `kind`, oldest first: {saved_at, host, this_machine, data, file}."""
    folder = _kind_dir(kind)
    if not folder.is_dir():
        return []
    out = []
    for f in folder.glob("*.json"):
        try:
            with open(f, encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        here = rec.get("machine", MACHINE) == MACHINE
        if here or other_machines:
            out.append({"saved_at": rec.get("saved_at", 0), "host": rec.get("host", ""), "this_machine": here,
                        "data": rec.get("data", {}), "file": str(f)})
    out.sort(key=lambda r: r["saved_at"])
    return out


def latest(kind: str) -> Optional[dict]:
    snaps = load(kind)
    return snaps[-1] if snaps else None


class TimeSeries:
    """Numeric samples over time in history/<name>-<machine>.sqlite (one file per computer)."""

    def __init__(self, name: str, columns: list[str], keep_days: float = 30):
        if not re.fullmatch(r"[a-z0-9_-]+", name) or not all(re.fullmatch(r"[a-z_][a-z0-9_]*", c) for c in columns):
            raise ValueError("bad time series or column name")
        self.path = HISTORY_DIR / f"{name}-{MACHINE[:8]}.sqlite"
        self.columns = list(columns)
        self.keep_days = keep_days

    def _connect(self, create: bool) -> Optional[sqlite3.Connection]:
        if not create and not self.path.exists():
            return None
        new = not self.path.exists()
        if create:
            _make_private_dir(HISTORY_DIR)
        con = sqlite3.connect(self.path, timeout=5)
        if new:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        cols = ", ".join(f"{c} REAL" for c in self.columns)
        con.execute(f"CREATE TABLE IF NOT EXISTS samples (ts REAL PRIMARY KEY, {cols})")
        existing = {row[1] for row in con.execute("PRAGMA table_info(samples)")}
        for c in self.columns:
            if c not in existing:  # a newer app version added a column
                con.execute(f"ALTER TABLE samples ADD COLUMN {c} REAL")
        return con

    def append(self, values: dict, ts: Optional[float] = None) -> None:
        """Add one sample; also drops samples older than keep_days."""
        ts = ts or time.time()
        con = self._connect(create=True)
        with con:
            names = ["ts", *self.columns]
            con.execute(f"INSERT OR REPLACE INTO samples ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                        [ts, *(values.get(c) for c in self.columns)])
            con.execute("DELETE FROM samples WHERE ts < ?", [ts - self.keep_days * 86400])
        con.close()

    def read(self, since: float = 0) -> list[dict]:
        """Samples with ts >= since, oldest first."""
        con = self._connect(create=False)
        if con is None:
            return []
        with con:
            rows = con.execute(f"SELECT ts, {', '.join(self.columns)} FROM samples WHERE ts >= ? ORDER BY ts",
                               [since]).fetchall()
        con.close()
        return [dict(zip(["ts", *self.columns], r)) for r in rows]
