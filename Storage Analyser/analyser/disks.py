"""Partition and block-device information (no root or administrator rights needed).

Linux reads /proc/self/mounts and `lsblk`; Windows asks PowerShell's Storage module.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import psutil

from .system import IS_WINDOWS

# Filesystems that hold no disk data (kernel views, RAM, snap loop images, desktop portals).
PSEUDO_FS = {
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "ramfs", "cgroup", "cgroup2", "securityfs", "debugfs",
    "tracefs", "fusectl", "configfs", "pstore", "bpf", "efivarfs", "mqueue", "hugetlbfs", "autofs",
    "binfmt_misc", "squashfs", "nsfs", "rpc_pipefs", "overlay", "fuse.portal", "fuse.gvfsd-fuse",
    "fuse.snapfuse",
}
NOT_A_PARTITION = PSEUDO_FS | {"vfat-efivars"}


def _unescape(field: str) -> str:
    """/proc/mounts escapes space, tab, newline and backslash as octal (\\040 ...)."""
    return (field.replace("\\040", " ").replace("\\011", "\t")
            .replace("\\012", "\n").replace("\\134", "\\"))


def read_mounts() -> list[tuple[str, str, str]]:
    """(device, mountpoint, fstype) for every mount, from /proc/self/mounts."""
    mounts = []
    try:
        with open("/proc/self/mounts", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 3:
                    mounts.append((_unescape(parts[0]), _unescape(parts[1]), parts[2]))
    except OSError:
        pass
    return mounts


def pseudo_mountpoints() -> set[str]:
    """Mountpoints a scan should never enter, even when crossing filesystems."""
    return {mp for _dev, mp, fstype in read_mounts() if fstype in PSEUDO_FS}


def mountpoint_of(path: str) -> str:
    path = os.path.realpath(path)
    while not os.path.ismount(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def partitions() -> list[dict]:
    """Mounted disk partitions with space and inode usage (one row per device)."""
    rows: dict[str, dict] = {}
    for p in psutil.disk_partitions(all=False):
        if p.fstype in NOT_A_PARTITION or p.mountpoint.startswith(("/snap/", "/var/snap/")):
            continue
        if IS_WINDOWS and "cdrom" in p.opts.split(","):
            continue
        try:
            usage = psutil.disk_usage(p.mountpoint)
            vfs = os.statvfs(p.mountpoint) if hasattr(os, "statvfs") else None  # Windows has no inodes
        except OSError:
            continue
        row = {
            "device": p.device,
            "mountpoint": p.mountpoint,
            "fstype": p.fstype,
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": usage.percent,
            "inodes_total": vfs.f_files if vfs else 0,
            "inodes_free": vfs.f_ffree if vfs else 0,
            "read_only": "ro" in p.opts.split(","),
        }
        # Keep the shortest mountpoint when a device is mounted more than once (bind mounts).
        prev = rows.get(p.device)
        if prev is None or len(p.mountpoint) < len(prev["mountpoint"]):
            rows[p.device] = row
    return sorted(rows.values(), key=lambda r: r["mountpoint"])


def _to_int(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def block_devices() -> list[dict]:
    """Every disk and partition, including ones that are not mounted (e.g. another OS's)."""
    if IS_WINDOWS:
        return _windows_block_devices()
    if not shutil.which("lsblk"):
        return []
    cols = "NAME,PATH,TYPE,SIZE,FSTYPE,LABEL,MOUNTPOINT,FSUSED,FSAVAIL,MODEL,ROTA,TRAN"
    try:
        proc = subprocess.run(["lsblk", "-J", "-b", "-o", cols], capture_output=True, text=True,
                              timeout=10, check=False)
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []

    out: list[dict] = []
    to_int = _to_int

    def walk(devs, disk_model=None, disk_kind=None):
        for d in devs or []:
            if d.get("type") == "loop":
                continue
            model = (d.get("model") or "").strip() or disk_model
            kind = disk_kind
            if d.get("type") == "disk":
                rota = d.get("rota")
                tran = (d.get("tran") or "").upper()
                kind = ("HDD" if rota in (True, "1", 1) else "SSD") + (f" ({tran})" if tran else "")
            out.append({
                "device": d.get("path") or f"/dev/{d.get('name')}",
                "type": d.get("type"),
                "size": to_int(d.get("size")),
                "fstype": d.get("fstype") or "",
                "label": d.get("label") or "",
                "mountpoint": d.get("mountpoint") or "",
                "used": to_int(d.get("fsused")),
                "free": to_int(d.get("fsavail")),
                "model": model or "",
                "kind": kind or "",
            })
            walk(d.get("children"), model, kind)

    walk(data.get("blockdevices"))
    return out


# --------------------------------------------------------------------------- Windows

# One JSON row per disk, followed by its partitions. Partitions without a volume (e.g. the
# reserved MSR partition or a Linux partition) have no filesystem or free space.
_PS_DISKS = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$media = @{}
Get-PhysicalDisk | ForEach-Object { $media[[string]$_.DeviceId] = [string]$_.MediaType }
$rows = foreach ($d in Get-Disk) {
    [pscustomobject]@{ type = 'disk'; disk = $d.Number; size = $d.Size; model = [string]$d.FriendlyName;
                       bus = [string]$d.BusType; media = $media[[string]$d.Number] }
    foreach ($p in Get-Partition -DiskNumber $d.Number) {
        $v = Get-Volume -Partition $p
        $letter = ''
        if ([string]$p.DriveLetter -match '^[A-Za-z]$') { $letter = [string]$p.DriveLetter }
        [pscustomobject]@{ type = 'part'; disk = $d.Number; part = $p.PartitionNumber; letter = $letter;
                           size = $p.Size; fs = [string]$v.FileSystem; label = [string]$v.FileSystemLabel;
                           vsize = $v.Size; free = $v.SizeRemaining }
    }
}
ConvertTo-Json -InputObject @($rows) -Compress
"""
# Older Windows versions report these as numbers instead of names.
_MEDIA = {"3": "HDD", "4": "SSD", "5": "SCM"}
_BUS = {"1": "SCSI", "3": "ATA", "7": "USB", "8": "RAID", "10": "SAS", "11": "SATA", "12": "SD", "17": "NVMe"}


def _windows_block_devices() -> list[dict]:
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        return []
    try:
        proc = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", _PS_DISKS], capture_output=True,
                              encoding="utf-8", errors="replace", timeout=40, check=False,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return parse_windows_disks(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def parse_windows_disks(text: str) -> list[dict]:
    """Rows in the same shape as the Linux (lsblk) ones, from the PowerShell script's JSON."""
    data = json.loads(text.strip() or "[]") if text else []
    if isinstance(data, dict):
        data = [data]
    out: list[dict] = []
    disk_info: dict = {}
    for r in data:
        disk = r.get("disk")
        if r.get("type") == "disk":
            media = str(r.get("media") or "")
            media = _MEDIA.get(media, media if media in ("HDD", "SSD", "SCM") else "")
            bus = str(r.get("bus") or "")
            bus = _BUS.get(bus, "" if bus.isdigit() or bus == "Unknown" else bus)
            kind = " ".join(x for x in (media, f"({bus})" if bus else "") if x)
            disk_info[disk] = ((r.get("model") or "").strip(), kind)
            out.append({"device": f"Disk {disk}", "type": "disk", "size": _to_int(r.get("size")), "fstype": "",
                        "label": "", "mountpoint": "", "used": None, "free": None,
                        "model": disk_info[disk][0], "kind": kind})
            continue
        vsize, free = _to_int(r.get("vsize")), _to_int(r.get("free"))
        letter = (r.get("letter") or "").strip()
        model, kind = disk_info.get(disk, ("", ""))
        out.append({
            "device": f"Disk {disk}, partition {r.get('part')}" + (f" ({letter}:)" if letter else ""),
            "type": "part",
            "size": _to_int(r.get("size")),
            "fstype": r.get("fs") or "",
            "label": r.get("label") or "",
            "mountpoint": f"{letter}:\\" if letter else "",
            "used": vsize - free if vsize is not None and free is not None else None,
            "free": free,
            "model": model,
            "kind": kind,
        })
    return out
