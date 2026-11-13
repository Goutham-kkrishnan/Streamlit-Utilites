"""Cleanup advisor: measures well-known reclaimable locations.

READ-ONLY by design. Nothing here deletes or modifies anything; every check only
measures sizes and suggests a command for the user to review and run themselves.
Subprocesses are run with argument lists (never a shell) and short timeouts.

Linux and Windows have separate check lists (CHECKS picks the one for this computer).
Suggested commands are bash on Linux and PowerShell on Windows.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import disks
from .system import HOME, IS_WINDOWS, alloc_bytes, is_redirect, user_folder

RISK_HELP = {
    "Safe": "A cache that rebuilds itself automatically.",
    "Low": "Comes back on demand (re-download or rebuild), costing some time or bandwidth.",
    "Review": "May hold data you still want. Look at it before deleting.",
}
RISK_ORDER = {"Safe": 0, "Low": 1, "Review": 2}


@dataclass
class Item:
    category: str
    name: str
    size: Optional[int]
    risk: str
    location: str
    command: str = ""
    note: str = ""
    details: list[str] = field(default_factory=list)


@dataclass
class Report:
    items: list[Item]
    errors: list[str]
    generated_at: float
    duration: float


# --------------------------------------------------------------------------- helpers

def win_short(path: str) -> str:
    """A Windows path shown relative to %LOCALAPPDATA%, %APPDATA% or %USERPROFILE% where it fits."""
    low = path.lower()
    for var in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):  # most specific first
        base = os.environ.get(var, "").rstrip("\\")
        if base and (low == base.lower() or low.startswith(base.lower() + "\\")):
            return f"%{var}%" + path[len(base):]
    return path


def ps_quote(path: str) -> str:
    """A Windows path as a PowerShell string, using $env:LOCALAPPDATA etc. where it fits."""
    short = win_short(path)
    var, rest = "", short
    if short.startswith("%"):
        var, _, rest = short[1:].partition("%")
    rest = rest.replace("`", "``").replace('"', '`"').replace("$", "`$")
    return f'"$env:{var}{rest}"' if var else f'"{rest}"'


def remove_cmd(paths: list[str]) -> str:
    """Command that deletes the given folders (for the user to run, never run here)."""
    if IS_WINDOWS:
        return "Remove-Item -Recurse -Force -ErrorAction SilentlyContinue " + ", ".join(ps_quote(p) for p in paths)
    return "rm -rf " + " \\\n       ".join(tilde(p) for p in paths)


def tilde(path: str) -> str:
    """Display of a path: shell-safe with ~ for the home folder on Linux, %APPDATA%-style on Windows."""
    if IS_WINDOWS:
        return win_short(path)
    if path == HOME:
        return "~"
    if path.startswith(HOME + os.sep):
        rel = path[len(HOME) + 1:]
        quoted = shlex.quote(rel)
        return "~/" + quoted
    return shlex.quote(path)


def dir_size(path: str, known: Optional[dict[str, int]] = None) -> Optional[int]:
    """Bytes on disk under `path` (None if it does not exist). Uses scan results when available."""
    if known is not None and path in known:
        return known[path]
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        return alloc_bytes(st)
    total = 0
    dev = st.st_dev
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        s = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += alloc_bytes(s)
                    # Windows directory entries report st_dev = 0, so rely on skipping junctions there.
                    if stat.S_ISDIR(s.st_mode) and not is_redirect(s) and (IS_WINDOWS or s.st_dev == dev):
                        stack.append(e.path)
        except OSError:
            continue
    return total


def _run(cmd: list[str], timeout: float = 15, allow_fail: bool = False) -> Optional[str]:
    if not shutil.which(cmd[0]):
        return None
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout, check=False,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 or allow_fail else None


_DECIMAL = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12, "PB": 1e15}
_BINARY = {"": 1, "B": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def parse_docker_size(text: str) -> int:
    m = re.match(r"\s*([\d.]+)\s*([kKMGTP]?B)", text or "")
    if not m:
        return 0
    return int(float(m.group(1)) * _DECIMAL[m.group(2).upper()])


# --------------------------------------------------------------------------- system checks

DOCKER_DATA = r"%LOCALAPPDATA%\Docker" if IS_WINDOWS else "/var/lib/docker"


def check_docker(known) -> list[Item]:
    out = _run(["docker", "system", "df", "--format", "{{json .}}"], timeout=30)
    if out is None:
        return []
    spec = {
        "Images": ("Unused Docker images", "docker image prune -a", "Review",
                   "Deletes every image that no container (running or stopped) uses; you would pull or rebuild "
                   "them later. To keep recent ones: docker image prune -a --filter \"until=720h\""),
        "Build Cache": ("Docker build cache", "docker builder prune", "Safe",
                        "Intermediate build layers. Next builds are slower until the cache warms up."),
        "Containers": ("Stopped Docker containers", "docker container prune", "Review",
                       "Removes stopped containers and anything written inside them."),
        "Local Volumes": ("Unused Docker volumes", "docker volume ls -f dangling=true   # inspect first\n"
                          "docker volume prune", "Review",
                          "Volumes often hold databases and other persistent data."),
    }
    items = []
    for line in out.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        kind = row.get("Type")
        if kind not in spec:
            continue
        name, cmd, risk, note = spec[kind]
        reclaim = parse_docker_size(row.get("Reclaimable", ""))
        total, active = row.get("TotalCount", "?"), row.get("Active", "?")
        if kind == "Images" and total == active:
            reclaim = 0  # every image backs a container; docker's "reclaimable" figure is misleading here
        items.append(Item("Docker", name, reclaim, risk, DOCKER_DATA, cmd, note,
                          [f"{total} total, {active} in use, {row.get('Size', '?')} overall"]))
    return items


def check_snaps(known) -> list[Item]:
    out = _run(["snap", "list", "--all"])
    if out is None:
        return []
    total, details = 0, []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4 and "disabled" in parts[-1]:
            name, rev = parts[0], parts[2]
            try:
                size = os.path.getsize(f"/var/lib/snapd/snaps/{name}_{rev}.snap")
            except OSError:
                size = 0
            total += size
            details.append(f"{name} (rev {rev})")
    if not details:
        return [Item("System", "Old snap revisions", 0, "Low", "/var/lib/snapd/snaps", "", "None found.")]
    cmd = ("snap list --all | awk '/disabled/{print $1, $3}' |\n"
           "  while read -r name rev; do sudo snap remove \"$name\" --revision=\"$rev\"; done\n"
           "sudo snap set system refresh.retain=2   # keep fewer old revisions in future")
    return [Item("System", f"{len(details)} old snap revisions", total, "Low", "/var/lib/snapd/snaps", cmd,
                 "Disabled revisions kept for rollback. Removing them only drops the ability to roll back.",
                 details)]


def check_journal(known) -> list[Item]:
    size = 0
    out = _run(["journalctl", "--disk-usage"])
    if out:
        m = re.search(r"take up ([\d.]+)\s*([KMGT]?)", out)
        if m:
            size = int(float(m.group(1)) * _BINARY[m.group(2)])
    size = max(size, dir_size("/var/log/journal") or 0)
    if not size:
        return []
    return [Item("System", "systemd journal logs", size, "Low", "/var/log/journal",
                 "sudo journalctl --vacuum-size=200M\n"
                 "# make it permanent: set SystemMaxUse=200M in /etc/systemd/journald.conf",
                 "Old system logs. Keeps the most recent 200 MB.")]


def check_rotated_logs(known) -> list[Item]:
    pattern = re.compile(r"\.(\d+|gz|xz|bz2|zst|old)$")
    total, count = 0, 0
    for dirpath, _dirs, files in os.walk("/var/log", onerror=lambda e: None):
        for f in files:
            if pattern.search(f):
                try:
                    total += os.lstat(os.path.join(dirpath, f)).st_blocks * 512
                    count += 1
                except OSError:
                    pass
    if not count:
        return []
    return [Item("System", f"{count} rotated log files", total, "Low", "/var/log",
                 "sudo find /var/log -type f -regextype posix-extended "
                 "-regex '.*\\.([0-9]+|gz|xz|bz2|zst|old)$' -delete",
                 "Compressed or rotated copies of old logs.")]


def check_apt(known) -> list[Item]:
    if not os.path.isdir("/var/cache/apt"):
        return []
    size = dir_size("/var/cache/apt") or 0
    return [Item("System", "APT package cache", size, "Safe", "/var/cache/apt", "sudo apt clean",
                 "Downloaded .deb files that are already installed.")]


def _installed_kernels() -> Optional[set[str]]:
    """Kernel versions with an installed linux-image/linux-modules package (None if dpkg is unavailable)."""
    out = _run(["dpkg-query", "-W", "-f=${Package}\t${db:Status-Abbrev}\n", "linux-image-*", "linux-modules-*"],
               allow_fail=True)
    if not out:
        return None
    versions = set()
    for line in out.splitlines():
        pkg, _, status = line.partition("\t")
        m = re.search(r"(\d+\.\d+\.\d+-\d+[-\w]*)$", pkg)
        if m and status.startswith("ii"):
            versions.add(m.group(1))
    return versions


def check_old_kernels(known) -> list[Item]:
    mod_root = "/lib/modules"
    if not os.path.isdir(mod_root):
        return []
    running = os.uname().release
    others = sorted(v for v in os.listdir(mod_root) if v != running and os.path.isdir(os.path.join(mod_root, v)))
    if not others:
        return []
    installed = _installed_kernels()

    def kernel_size(v: str) -> int:
        size = dir_size(os.path.join(mod_root, v)) or 0
        try:
            for f in os.listdir("/boot"):
                if f.endswith("-" + v):
                    size += os.lstat(os.path.join("/boot", f)).st_blocks * 512
        except OSError:
            pass
        return size

    items = []
    old_installed = [v for v in others if installed is None or v in installed]
    if old_installed:
        n = len(old_installed)
        items.append(Item(
            "System", f"{n} older installed kernel{'s' if n > 1 else ''}", sum(kernel_size(v) for v in old_installed),
            "Review" if n == 1 else "Low", "/lib/modules, /boot",
            "dpkg -l 'linux-image-*' | grep ^ii   # see what is installed\nsudo apt autoremove --purge",
            f"You are running {running}. Ubuntu deliberately keeps one older kernel as a fallback in case a new "
            "one fails to boot; autoremove only removes kernels beyond that.", old_installed))
    if installed is not None:
        leftovers = [v for v in others if v not in installed]
        if leftovers:
            folders = " ".join(shlex.quote(os.path.join(mod_root, v)) for v in leftovers)
            items.append(Item(
                "System", f"{len(leftovers)} leftover folders from removed kernels",
                sum(kernel_size(v) for v in leftovers), "Low", mod_root,
                "# purge config left behind by removed kernel packages\n"
                "dpkg -l 'linux-image-*' 'linux-modules-*' | awk '/^rc/{print $2}' | xargs -r sudo apt purge -y\n"
                "# then remove module folders of kernels that are no longer installed\n"
                f"sudo rm -rf {folders}",
                "These kernels were already uninstalled; only stray module files (often from DKMS) remain.",
                leftovers))
    return items


def check_crash(known) -> list[Item]:
    size = dir_size("/var/crash")
    if not size or size < 64 * 1024:
        return []
    return [Item("System", "Crash reports", size, "Safe", "/var/crash", "sudo rm -f /var/crash/*",
                 "Saved crash dumps from apport.")]


# --------------------------------------------------------------------------- user checks

LINUX_USER_LOCATIONS = [
    # category, name, path, command, risk, note
    ("Package caches", "pip download cache", "~/.cache/pip", "pip cache purge", "Safe", ""),
    ("Package caches", "npm cache", "~/.npm/_cacache", "npm cache clean --force", "Safe", ""),
    ("Package caches", "Yarn cache", "~/.cache/yarn", "yarn cache clean", "Safe", ""),
    ("Package caches", "pnpm store", "~/.local/share/pnpm/store", "pnpm store prune", "Safe", ""),
    ("Package caches", "NuGet packages", "~/.nuget/packages", "dotnet nuget locals all --clear", "Low",
     "Restored on the next build."),
    ("Package caches", "Gradle caches", "~/.gradle/caches", "rm -rf ~/.gradle/caches", "Low", ""),
    ("Package caches", "Maven repository", "~/.m2/repository", "rm -rf ~/.m2/repository", "Low", ""),
    ("Package caches", "Go module cache", "~/go/pkg/mod", "go clean -modcache", "Low", ""),
    ("Package caches", "Cargo registry", "~/.cargo/registry", "rm -rf ~/.cargo/registry/cache", "Low", ""),
    ("Package caches", "Conda packages (anaconda3)", "~/anaconda3/pkgs", "conda clean --all", "Safe", ""),
    ("Package caches", "Conda packages (miniconda3)", "~/miniconda3/pkgs", "conda clean --all", "Safe", ""),
    ("Package caches", "nvm download cache", "~/.nvm/.cache", "nvm cache clear", "Safe", ""),
    ("ML caches", "Hugging Face models & datasets", "~/.cache/huggingface", "huggingface-cli delete-cache",
     "Review", "Models download again on next use (often several GB each). The command opens a picker."),
    ("ML caches", "PyTorch hub cache", "~/.cache/torch", "rm -rf ~/.cache/torch", "Review",
     "Pretrained weights download again on next use."),
    ("Desktop", "Thumbnail cache", "~/.cache/thumbnails", "rm -rf ~/.cache/thumbnails/*", "Safe", ""),
    ("Desktop", "Trash", "~/.local/share/Trash", "gio trash --empty", "Review",
     "Permanently deletes everything in the Trash."),
    ("Editors & IDEs", "JetBrains caches", "~/.cache/JetBrains", "rm -rf ~/.cache/JetBrains", "Safe",
     "Close JetBrains IDEs first; indexes rebuild."),
]

WINDOWS_USER_LOCATIONS = [
    # category, name, path (%VAR% and ~ are expanded), PowerShell command, risk, note
    ("Package caches", "pip download cache", r"%LOCALAPPDATA%\pip\Cache", "pip cache purge", "Safe", ""),
    ("Package caches", "npm cache", r"%LOCALAPPDATA%\npm-cache\_cacache", "npm cache clean --force", "Safe", ""),
    ("Package caches", "Yarn cache", r"%LOCALAPPDATA%\Yarn\Cache", "yarn cache clean", "Safe", ""),
    ("Package caches", "pnpm store", r"%LOCALAPPDATA%\pnpm\store", "pnpm store prune", "Safe", ""),
    ("Package caches", "NuGet packages", r"~\.nuget\packages", "dotnet nuget locals all --clear", "Low",
     "Restored on the next build."),
    ("Package caches", "Gradle caches", r"~\.gradle\caches", r'Remove-Item -Recurse -Force "$HOME\.gradle\caches"',
     "Low", ""),
    ("Package caches", "Maven repository", r"~\.m2\repository",
     r'Remove-Item -Recurse -Force "$HOME\.m2\repository"', "Low", ""),
    ("Package caches", "Go module cache", r"~\go\pkg\mod", "go clean -modcache", "Low", ""),
    ("Package caches", "Cargo registry", r"~\.cargo\registry",
     r'Remove-Item -Recurse -Force "$HOME\.cargo\registry\cache"', "Low", ""),
    ("Package caches", "Conda packages (anaconda3)", r"~\anaconda3\pkgs", "conda clean --all", "Safe", ""),
    ("Package caches", "Conda packages (miniconda3)", r"~\miniconda3\pkgs", "conda clean --all", "Safe", ""),
    ("ML caches", "Hugging Face models & datasets", r"~\.cache\huggingface", "huggingface-cli delete-cache",
     "Review", "Models download again on next use (often several GB each). The command opens a picker."),
    ("ML caches", "PyTorch hub cache", r"~\.cache\torch", r'Remove-Item -Recurse -Force "$HOME\.cache\torch"',
     "Review", "Pretrained weights download again on next use."),
    ("Windows", "Thumbnail cache", r"%LOCALAPPDATA%\Microsoft\Windows\Explorer",
     "cleanmgr   # tick 'Thumbnails'", "Safe", "Rebuilt as you browse folders."),
    ("Windows", "Internet cache (INetCache)", r"%LOCALAPPDATA%\Microsoft\Windows\INetCache",
     "cleanmgr   # tick 'Temporary Internet Files'", "Safe", "Cache used by Windows and older apps."),
]
USER_LOCATIONS = WINDOWS_USER_LOCATIONS if IS_WINDOWS else LINUX_USER_LOCATIONS

EDITORS = ["Code", "Code - Insiders", "VSCodium", "Cursor", "Antigravity"]
EDITOR_HOME = r"%APPDATA%" if IS_WINDOWS else "~/.config"
EDITOR_CACHE_DIRS = ["Cache", "CachedData", "CachedExtensionVSIXs", "CachedProfilesData", "Code Cache",
                     "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache", "Service Worker/CacheStorage", "logs"]

# browser: (profile folder, separate cache folder or None)
if IS_WINDOWS:  # the HTTP cache lives inside each profile, in "Cache"
    BROWSERS: dict[str, tuple[str, Optional[str]]] = {
        "Google Chrome": (r"%LOCALAPPDATA%\Google\Chrome\User Data", None),
        "Chromium": (r"%LOCALAPPDATA%\Chromium\User Data", None),
        "Brave": (r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data", None),
        "Microsoft Edge": (r"%LOCALAPPDATA%\Microsoft\Edge\User Data", None),
    }
    FIREFOX_CACHES = [("Firefox cache", r"%LOCALAPPDATA%\Mozilla\Firefox\Profiles")]
else:
    BROWSERS = {
        "Google Chrome": ("~/.config/google-chrome", "~/.cache/google-chrome"),
        "Chromium": ("~/.config/chromium", "~/.cache/chromium"),
        "Brave": ("~/.config/BraveSoftware/Brave-Browser", "~/.cache/BraveSoftware/Brave-Browser"),
        "Microsoft Edge": ("~/.config/microsoft-edge", "~/.cache/microsoft-edge"),
    }
    FIREFOX_CACHES = [("Firefox cache", "~/.cache/mozilla/firefox"),
                      ("Firefox (snap) cache", "~/snap/firefox/common/.cache/mozilla/firefox")]
BROWSER_CACHE_DIRS = (["Cache"] if IS_WINDOWS else []) + [
    "Service Worker/CacheStorage", "Service Worker/ScriptCache", "GPUCache", "Code Cache"]


def _x(path: str) -> str:
    """Expand ~ and environment variables (%LOCALAPPDATA% on Windows), with native separators."""
    return os.path.normpath(os.path.expandvars(os.path.expanduser(path)))


def check_user_locations(known) -> list[Item]:
    items = []
    for category, name, path, cmd, risk, note in USER_LOCATIONS:
        full = _x(path)
        size = dir_size(full, known)
        if size:
            items.append(Item(category, name, size, risk, tilde(full), cmd, note))
    return items


def check_editors(known) -> list[Item]:
    items = []
    for editor in EDITORS:
        base = _x(os.path.join(EDITOR_HOME, editor))
        if not os.path.isdir(base):
            continue
        found = [(os.path.join(base, d), dir_size(os.path.join(base, d), known)) for d in EDITOR_CACHE_DIRS]
        found = [(p, s) for p, s in found if s]
        label = "VS Code" if editor == "Code" else editor
        if found:
            cmd = f"# close {label} first\n" + remove_cmd([p for p, _ in found])
            items.append(Item("Editors & IDEs", f"{label} caches", sum(s for _, s in found), "Safe", tilde(base),
                              cmd, "Caches, cached extension installers and logs. Recreated as needed.",
                              [f"{tilde(p)} — {s / 1e6:,.0f} MB" for p, s in found]))
        ws = os.path.join(base, "User", "workspaceStorage")
        size = dir_size(ws, known)
        if size and size > 50e6:
            items.append(Item("Editors & IDEs", f"{label} per-project state", size, "Review", tilde(ws), "",
                              "One folder per project ever opened (search indexes, extension state). Sort by "
                              "date in the Explorer tab and delete folders for projects you no longer use."))
    return items


def check_browsers(known) -> list[Item]:
    items = []
    for browser, (cfg, cache) in BROWSERS.items():
        cfg = _x(cfg)
        if not os.path.isdir(cfg):
            continue
        parts = []
        if cache:
            cache = _x(cache)
            cache_size = dir_size(cache, known)
            if cache_size:
                parts.append((cache, cache_size))
        try:
            profiles = [p for p in os.listdir(cfg) if p == "Default" or p.startswith("Profile ")]
        except OSError:
            profiles = []
        for prof in profiles:
            for sub in BROWSER_CACHE_DIRS:
                p = os.path.join(cfg, prof, sub)
                s = dir_size(p, known)
                if s:
                    parts.append((p, s))
        model_dir = os.path.join(cfg, "OptGuideOnDeviceModel")
        model_size = dir_size(model_dir, known)
        if model_size and model_size > 100e6:
            items.append(Item("Browsers", f"{browser} on-device AI model", model_size, "Review", tilde(model_dir),
                              "", "A local AI model the browser uses for built-in AI features. If you delete the "
                              "folder, the browser downloads it again unless those AI features are switched off "
                              "in its settings first."))
        total = sum(s for _, s in parts)
        if total:
            items.append(Item("Browsers", f"{browser} caches", total, "Safe", tilde(cfg), "",
                              "Clear from inside the browser: Settings → Privacy and security → Delete browsing "
                              "data → 'Cached images and files'. Deleting these folders while the browser is "
                              "running can corrupt the profile.",
                              [f"{tilde(p)} — {s / 1e6:,.0f} MB" for p, s in sorted(parts, key=lambda x: -x[1])]))
    for label, path in FIREFOX_CACHES:
        size = dir_size(_x(path), known)
        if size:
            items.append(Item("Browsers", label, size, "Safe", tilde(_x(path)), "",
                              "Clear from Firefox: Settings → Privacy & Security → Cookies and Site Data → Clear "
                              "Data → Cached Web Content."))
    return items


def check_node_versions(known) -> list[Item]:
    base = _x("~/.nvm/versions/node")
    if not os.path.isdir(base):
        return []
    versions = sorted(os.listdir(base))
    if len(versions) < 2:
        return []
    keep = set()
    current = (_run(["node", "--version"]) or "").strip()
    if current:
        keep.add(current)
    try:
        with open(_x("~/.nvm/alias/default"), encoding="utf-8") as fh:
            alias = fh.read().strip().lstrip("v")
        keep.update(v for v in versions if alias and v.lstrip("v").startswith(alias))
    except OSError:
        pass
    items = []
    for v in versions:
        if v in keep:
            continue
        size = dir_size(os.path.join(base, v), known)
        if size:
            items.append(Item("Runtimes", f"Node.js {v} (nvm)", size, "Review", tilde(os.path.join(base, v)),
                              f"nvm uninstall {shlex.quote(v)}",
                              "Not your default or current Node version. Check no project needs it."))
    return items


def check_old_downloads(known, min_size: int = 50_000_000, days: int = 90) -> list[Item]:
    base = user_folder("downloads")
    if not os.path.isdir(base):
        return []
    cutoff = time.time() - days * 86400
    total, files = 0, []
    for dirpath, _dirs, names in os.walk(base, onerror=lambda e: None):
        for n in names:
            p = os.path.join(dirpath, n)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and st.st_size >= min_size and st.st_mtime < cutoff:
                total += alloc_bytes(st)
                files.append((st.st_size, p))
    if not files:
        return []
    files.sort(reverse=True)
    if IS_WINDOWS:
        cmd = (f"Get-ChildItem {ps_quote(base)} -File -Recurse |\n"
               f"  Where-Object {{ $_.Length -ge {min_size // 1_000_000}MB -and "
               f"$_.LastWriteTime -lt (Get-Date).AddDays(-{days}) }} |\n"
               "  Sort-Object Length -Descending | Format-Table Length, LastWriteTime, FullName   # list only")
    else:
        cmd = (f"find {tilde(base)} -type f -size +{min_size // 1_000_000}M "
               f"-mtime +{days} -exec ls -lh {{}} +   # list only")
    return [Item("Your files", f"{len(files)} large files in Downloads, untouched for {days}+ days", total,
                 "Review", tilde(base), cmd,
                 "Old installers, archives and exports are often safe to delete — but check first.",
                 [f"{tilde(p)} — {s / 1e6:,.0f} MB" for s, p in files[:25]])]


def check_other_cache(known, counted: list[Item]) -> list[Item]:
    cache = _x("~/.cache")
    total = dir_size(cache, known)
    if not total:
        return []
    prefix = "~/.cache/"
    already = 0
    for it in counted:
        if it.location.startswith(prefix):
            already += it.size or 0
        else:
            already += sum(_detail_size(d) for d in it.details if d.startswith(prefix))
    rest = max(0, total - already)
    return [Item("Desktop", "Other application caches", rest, "Low", "~/.cache", "",
                 "Everything else in ~/.cache. Usually safe to clear while the apps are closed; open ~/.cache "
                 "in the Explorer tab to see which apps it belongs to.")]


def _detail_size(detail: str) -> int:
    m = re.search(r"— ([\d,]+) MB$", detail)
    return int(m.group(1).replace(",", "")) * 1_000_000 if m else 0


# --------------------------------------------------------------------------- Windows checks

WINDIR = os.environ.get("SystemRoot") or r"C:\Windows"
SYSTEM_DRIVE = (os.environ.get("SystemDrive") or "C:") + "\\"
ADMIN_NOTE = "Partly unreadable without administrator rights, so the real size may be larger."


def check_win_temp(known) -> list[Item]:
    items = []
    user_tmp = os.path.realpath(os.environ.get("TEMP") or _x(r"%LOCALAPPDATA%\Temp"))  # realpath: no 8.3 names
    size = dir_size(user_tmp, known)
    if size:
        items.append(Item("System", "Temporary files (your account)", size, "Low", tilde(user_tmp),
                          r'Remove-Item "$env:TEMP\*" -Recurse -Force -ErrorAction SilentlyContinue',
                          "Files apps left behind. Files still in use are skipped, so close apps first."))
    sys_tmp = os.path.join(WINDIR, "Temp")
    size = dir_size(sys_tmp, known)
    if size and size > 1_000_000:
        items.append(Item("System", "Windows temporary files", size, "Low", sys_tmp,
                          "# in an administrator PowerShell:\n"
                          r'Remove-Item "$env:SystemRoot\Temp\*" -Recurse -Force -ErrorAction SilentlyContinue',
                          f"Temporary files of Windows and installers. {ADMIN_NOTE}"))
    return items


def check_win_update(known) -> list[Item]:
    path = os.path.join(WINDIR, "SoftwareDistribution", "Download")
    size = dir_size(path, known)
    if not size:
        return []
    return [Item("System", "Windows Update downloads", size, "Low", path,
                 "cleanmgr   # 'Clean up system files' → tick 'Windows Update Cleanup'\n"
                 "# also shrink the component store (WinSxS), in an administrator PowerShell:\n"
                 "Dism.exe /Online /Cleanup-Image /StartComponentCleanup",
                 f"Installer files of updates that are already installed. {ADMIN_NOTE}")]


def check_windows_old(known) -> list[Item]:
    path = os.path.join(SYSTEM_DRIVE, "Windows.old")
    size = dir_size(path, known)
    if not size:
        return []
    return [Item("System", "Previous Windows installation (Windows.old)", size, "Review", path,
                 "cleanmgr   # 'Clean up system files' → tick 'Previous Windows installation(s)'",
                 "Kept after a major upgrade so you can go back; removing it makes going back impossible. "
                 + ADMIN_NOTE)]


def check_hibernation(known) -> list[Item]:
    path = os.path.join(SYSTEM_DRIVE, "hiberfil.sys")
    try:
        size = os.stat(path).st_size
    except OSError:
        return []
    return [Item("System", "Hibernation file", size, "Review", path,
                 "# in an administrator PowerShell; undo with: powercfg /hibernate on\npowercfg /hibernate off",
                 "Turns off hibernation and Fast Startup, then Windows deletes the file. Fast Startup is best "
                 "kept off anyway when the PC also boots Linux.")]


def check_win_crash(known) -> list[Item]:
    program_data = os.environ.get("ProgramData") or r"C:\ProgramData"
    found = [(p, dir_size(p, known)) for p in (
        _x(r"%LOCALAPPDATA%\CrashDumps"), os.path.join(WINDIR, "Minidump"), os.path.join(WINDIR, "MEMORY.DMP"),
        os.path.join(program_data, "Microsoft", "Windows", "WER"))]
    found = [(p, s) for p, s in found if s]
    total = sum(s for _, s in found)
    if total < 1_000_000:
        return []
    return [Item("System", "Crash dumps and error reports", total, "Safe", "Several folders",
                 "cleanmgr   # tick 'Windows error reports' (and, under 'Clean up system files', "
                 "'System error memory dump files')",
                 "Diagnostic files written when apps or Windows crashed.",
                 [f"{tilde(p)} — {s / 1e6:,.0f} MB" for p, s in found])]


def check_recycle_bin(known) -> list[Item]:
    found = []
    for part in disks.partitions():
        path = os.path.join(part["mountpoint"], "$Recycle.Bin")
        size = dir_size(path, known)
        if size and size > 1_000_000:
            found.append((path, size))
    if not found:
        return []
    return [Item("Your files", "Recycle Bin", sum(s for _, s in found), "Review", "$Recycle.Bin on each drive",
                 "Clear-RecycleBin -Force   # empties it on every drive",
                 "Permanently deletes everything in the Recycle Bin. Only your own deleted files are counted.",
                 [f"{p} — {s / 1e6:,.0f} MB" for p, s in found])]


LINUX_CHECKS: list[tuple[str, Callable]] = [
    ("Docker", check_docker),
    ("Snaps", check_snaps),
    ("Journal", check_journal),
    ("Rotated logs", check_rotated_logs),
    ("APT", check_apt),
    ("Kernels", check_old_kernels),
    ("Crash reports", check_crash),
    ("Caches", check_user_locations),
    ("Editors", check_editors),
    ("Browsers", check_browsers),
    ("Node versions", check_node_versions),
    ("Downloads", check_old_downloads),
]
WINDOWS_CHECKS: list[tuple[str, Callable]] = [
    ("Docker", check_docker),
    ("Temporary files", check_win_temp),
    ("Windows Update", check_win_update),
    ("Windows.old", check_windows_old),
    ("Hibernation file", check_hibernation),
    ("Crash dumps", check_win_crash),
    ("Recycle Bin", check_recycle_bin),
    ("Caches", check_user_locations),
    ("Editors", check_editors),
    ("Browsers", check_browsers),
    ("Downloads", check_old_downloads),
]
CHECKS = WINDOWS_CHECKS if IS_WINDOWS else LINUX_CHECKS


def run_checks(known: Optional[dict[str, int]] = None,
               progress: Optional[Callable[[int, int, str], None]] = None) -> Report:
    t0 = time.monotonic()
    items: list[Item] = []
    errors: list[str] = []
    steps = len(CHECKS) + (0 if IS_WINDOWS else 1)
    for i, (label, fn) in enumerate(CHECKS):
        if progress:
            progress(i, steps, label)
        try:
            items.extend(fn(known))
        except Exception as exc:  # one broken check must not hide the others
            errors.append(f"{label}: {exc}")
    if not IS_WINDOWS:  # whatever is left in ~/.cache (Windows has no single cache folder)
        if progress:
            progress(steps - 1, steps, "Other caches")
        try:
            items.extend(check_other_cache(known, items))
        except Exception as exc:
            errors.append(f"Other caches: {exc}")
    items.sort(key=lambda it: (-(it.size or 0)))
    return Report(items, errors, time.time(), time.monotonic() - t0)
