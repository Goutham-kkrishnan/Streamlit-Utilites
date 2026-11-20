"""What differs between Linux and Windows: OS detection, user folders and on-disk sizes.

Standard library only, so the launcher can use it before any packages are installed.
"""
from __future__ import annotations

import os
import re
import tempfile

IS_WINDOWS = os.name == "nt"
OS_NAME = "Windows" if IS_WINDOWS else "Linux"
OTHER_OS = "Linux" if IS_WINDOWS else "Windows"
HOME = os.path.expanduser("~")
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_SLUG = re.sub(r"[^a-z0-9]+", "-", os.path.basename(APP_DIR).lower()).strip("-") or "app"
LAUNCHER = "run.bat" if IS_WINDOWS else "run.sh"
SHELL_LANGUAGE = "powershell" if IS_WINDOWS else "bash"  # for the Cleanup tab's suggested commands

# Windows has no st_blocks, so "on disk" is estimated by rounding up to NTFS's default cluster size.
CLUSTER = 4096
# Cloud-only (OneDrive "online-only") and offline files take no local space.
_NOT_LOCAL = 0x1000 | 0x40000 | 0x400000  # OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS
# Reparse tags with this bit point somewhere else: junctions, symlinks and volume mount points.
_NAME_SURROGATE = 0x20000000


def alloc_bytes(st: os.stat_result) -> int:
    """Bytes a file occupies on disk (exact on Linux, estimated on Windows)."""
    if IS_WINDOWS:
        if getattr(st, "st_file_attributes", 0) & _NOT_LOCAL:
            return 0
        return -(-st.st_size // CLUSTER) * CLUSTER
    return st.st_blocks * 512


def is_redirect(st: os.stat_result) -> bool:
    """True for a Windows junction or mount point, which a walk must not follow (like a symlink)."""
    return bool(getattr(st, "st_reparse_tag", 0) & _NAME_SURROGATE)


_WINDOWS_FOLDERS = {"desktop": "Desktop", "downloads": "{374DE290-123F-4565-9164-39C4925E467B}"}
_XDG_FOLDERS = {"desktop": "XDG_DESKTOP_DIR", "downloads": "XDG_DOWNLOAD_DIR"}


def user_folder(kind: str) -> str:
    """The user's Desktop or Downloads folder, wherever this computer keeps it.

    Windows may move them into OneDrive; Linux names them in the user's language
    (e.g. ~/Schreibtisch). Falls back to ~/Desktop or ~/Downloads.
    """
    default = os.path.join(HOME, kind.capitalize())
    path = ""
    try:
        if IS_WINDOWS:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                path = os.path.expandvars(winreg.QueryValueEx(key, _WINDOWS_FOLDERS[kind])[0])
        else:
            config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
            with open(os.path.join(config_home, "user-dirs.dirs"), encoding="utf-8") as fh:
                for line in fh:
                    name, _, value = line.strip().partition("=")
                    if name == _XDG_FOLDERS[kind]:
                        path = value.strip().strip('"').replace("$HOME", HOME)
    except (ImportError, OSError, KeyError, ValueError):
        return default
    return path if path and os.path.isdir(path) else default


def user_data_dir() -> str:
    """Per-user folder for this app's data, used when the app folder itself is read-only."""
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local")
        return os.path.join(base, "StreamlitUtilities", APP_SLUG)
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share")
    return os.path.join(base, "streamlit-utilities", APP_SLUG)


def is_writable(folder: str) -> bool:
    """Whether files can be created in `folder` (os.access is unreliable on Windows)."""
    try:
        with tempfile.TemporaryFile(dir=folder):
            return True
    except OSError:
        return False
