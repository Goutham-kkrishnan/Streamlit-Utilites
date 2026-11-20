"""The read-only guard: the only place this app may run other programs.

Apps in Streamlit Utilities never change the system on their own. Every external command goes
through run() or powershell() here and is refused (PermissionError) unless:

  1. it starts with one of this app's ALLOWED prefixes, and
  2. it passes the rules below for programs that can also change things (git, systemctl, ...),
     or the program only ever reads (READ_ONLY_PROGRAMS).

tests/test_read_only.py checks that nothing else in the app starts processes or writes files.
A future "actions" feature (delete, uninstall, disable, ...) must live in its own module behind
an explicit confirmation, and must never be added to ALLOWED.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Optional

# Command prefixes this app runs, e.g. ("git", "--no-optional-locks", "status").
# Only commands that read and never change anything belong here.
ALLOWED: list[tuple[str, ...]] = [
    # Dev Workspace Cleaner: git state of each repository. --no-optional-locks stops git from refreshing
    # (writing) the index while it reads; every subcommand below only reads.
    ("git", "--no-optional-locks", "status"),          # branch, upstream and uncommitted changes
    ("git", "--no-optional-locks", "stash", "list"),   # number of stashes
    ("git", "--no-optional-locks", "for-each-ref"),    # whether any remote branches exist
    ("git", "--no-optional-locks", "rev-list"),        # commits that no remote branch contains
    ("git", "--no-optional-locks", "log"),             # date of the last commit
]

# Programs that only read, whatever their arguments.
READ_ONLY_PROGRAMS = {
    "ss", "lsblk", "findmnt", "dpkg-query", "uname", "df", "free", "lscpu", "lspci", "lsusb", "sensors",
    "who", "uptime", "id", "whoami", "getent", "stat", "systemd-detect-virt", "nproc",
}

# Programs that can also change things: the first non-option arguments must start with one of these
# read-only subcommands. A tuple ending in "" must match exactly (no further non-option arguments).
READ_SUBCOMMANDS: dict[str, set[tuple[str, ...]]] = {
    "git": {("status",), ("rev-list",), ("log",), ("rev-parse",), ("for-each-ref",), ("ls-files",),
            ("stash", "list")},
    "systemctl": {("list-unit-files",), ("list-units",), ("list-timers",), ("show",), ("status",),
                  ("is-enabled",), ("is-active",), ("cat",)},
    "systemd-analyze": {("time",), ("blame",), ("critical-chain",), ("security",), ("verify",)},
    "snap": {("list",), ("info",), ("version",)},
    "flatpak": {("list",), ("info",)},
    "apt-mark": {("showmanual",), ("showauto",), ("showhold",)},
    "pip": {("list",), ("show",), ("freeze",)},
    "pip3": {("list",), ("show",), ("freeze",)},
    "npm": {("ls",), ("list",)},
    "winget": {("list",), ("show",)},
    "docker": {("system", "df"), ("ps",), ("images",), ("info",), ("version",)},
    "gsettings": {("get",), ("list-keys",), ("list-recursively",)},
    "udisksctl": {("info",), ("status",), ("dump",)},
    "nvme": {("list",), ("smart-log",), ("id-ctrl",)},
    "ip": {("addr", ""), ("addr", "show"), ("route", ""), ("route", "show"), ("link", ""), ("link", "show")},
    "ufw": {("status",)},
    "sc": {("query",), ("queryex",), ("qc",)},
    "reg": {("query",)},
    "schtasks": {("/query",)},
    "netsh": {("advfirewall", "show"), ("interface", "show"), ("wlan", "show")},
}
# Programs whose read-only use is chosen by options.
REQUIRED_FLAGS = {"apt-get": {"-s", "--simulate", "--dry-run", "--just-print", "--no-act", "--recon"},
                  "crontab": {"-l"}}  # without -l, `crontab FILE` would install FILE
ALLOWED_FLAGS = {
    "crontab": {"-l"},
    "upower": {"-e", "--enumerate", "-i", "--show-info", "-d", "--dump"},
    "firewall-cmd": {"--state", "--get-default-zone", "--get-active-zones", "--list-all"},
    "smartctl": {"-a", "--all", "-x", "--xall", "-i", "--info", "-H", "--health", "-A", "--attributes",
                 "-j", "--json", "-c", "--capabilities", "--scan", "--scan-open"},
}
ALLOWED_ARGS = {"powercfg": {"/a", "/availablesleepstates", "/l", "/list", "/q", "/query", "/getactivescheme",
                             "/lastwake"}}
FORBIDDEN_FLAG_PREFIXES = {"journalctl": ("--vacuum", "--rotate", "--flush", "--sync", "--relinquish-var",
                                          "--smart-relinquish-var", "--setup-keys", "--update-catalog")}
PYTHON_ARG_PREFIXES = {("-m", "pip", "list"), ("-m", "pip", "show"), ("-m", "pip", "freeze")}
PYTHONS = {"python", "python3", "py"}
RULED = (set(READ_SUBCOMMANDS) | set(REQUIRED_FLAGS) | set(ALLOWED_FLAGS) | set(ALLOWED_ARGS)
         | set(FORBIDDEN_FLAG_PREFIXES) | PYTHONS)

# PowerShell: only verbs that read. Scripts using any other verb (or a write alias such as rm) are
# refused. Build objects with [pscustomobject]@{...}, not New-Object.
READ_VERBS = {"Get", "Select", "Where", "ForEach", "Sort", "Measure", "Group", "ConvertTo", "ConvertFrom",
              "Format", "Test", "Join", "Split", "Compare", "Resolve"}
_CMDLET = re.compile(r"(?<![\w$.-])([A-Za-z]+)-([A-Za-z]+)\b")
_PS_WRITE_ALIASES = re.compile(r"(?<![\w$.-])(rm|del|erase|rd|rmdir|ri|mv|mi|move|cp|cpi|copy|ni|md|mkdir|sc|ac|"
                               r"clc|kill|spps|saps|start|sasv|spsv|icm|ii|sv|si|rni|ren|rp|rv|iwr|irm|curl|wget)"
                               r"(?![\w-])", re.IGNORECASE)
_PS_FORBIDDEN = re.compile(r"Format-Volume|\.(Delete|Kill|Terminate|Create|Put|SetValue|DeleteValue)\(|"
                           r"\[System\.IO\.(File|Directory)\]::(Write|Delete|Move|Copy|Create|Append)|"
                           r"\biex\b|Invoke-", re.IGNORECASE)
_PS_NULL_REDIRECT = re.compile(r"\d?>\s*\$null")
POWERSHELL_PRELUDE = "$ErrorActionPreference = 'SilentlyContinue'\n[Console]::OutputEncoding = [Text.Encoding]::UTF8\n"


def _program(cmd: list[str] | tuple[str, ...]) -> str:
    name = os.path.basename(cmd[0]).lower()
    return name[:-4] if name.endswith(".exe") else name


def _positionals(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith("-")]


def _follows_rules(prog: str, args: list[str]) -> bool:
    if prog in PYTHONS:
        return any(tuple(args[:len(p)]) == p for p in PYTHON_ARG_PREFIXES)
    if prog in READ_SUBCOMMANDS:
        pos = _positionals(args)
        ok = False
        for verb in READ_SUBCOMMANDS[prog]:
            if verb and verb[-1] == "":
                ok = tuple(pos) == verb[:-1]
            else:
                ok = tuple(pos[:len(verb)]) == verb
            if ok:
                break
        if not ok:
            return False
    if prog in REQUIRED_FLAGS and not REQUIRED_FLAGS[prog] & set(args):
        return False
    if prog in ALLOWED_FLAGS and any(a.startswith("-") and a.split("=")[0] not in ALLOWED_FLAGS[prog] for a in args):
        return False
    if prog in ALLOWED_ARGS and any(a.lower() not in ALLOWED_ARGS[prog] for a in args):
        return False
    if prog in FORBIDDEN_FLAG_PREFIXES and any(a.startswith(FORBIDDEN_FLAG_PREFIXES[prog]) for a in args):
        return False
    return prog in RULED or prog in READ_ONLY_PROGRAMS


def check(cmd: list[str] | tuple[str, ...]) -> None:
    """Raise PermissionError unless `cmd` is an allowed, read-only command."""
    if not cmd:
        raise PermissionError("empty command")
    prog, args = _program(cmd), list(cmd[1:])
    if prog in ("powershell", "pwsh"):
        raise PermissionError("use readonly.powershell() for PowerShell")
    if not any((prog, *args)[:len(p)] == p for p in ALLOWED):
        raise PermissionError(f"not in this app's ALLOWED list: {' '.join(cmd)}")
    if not _follows_rules(prog, args):
        raise PermissionError(f"not a read-only use of {prog}: {' '.join(cmd)}")


def check_powershell(script: str) -> None:
    """Raise PermissionError if a PowerShell script uses anything but read-only verbs."""
    if _PS_FORBIDDEN.search(script):
        raise PermissionError("PowerShell script calls something that can change the system")
    if ">" in _PS_NULL_REDIRECT.sub("", script).replace("->", ""):
        raise PermissionError("PowerShell script redirects output to a file")
    alias = _PS_WRITE_ALIASES.search(_strip_strings(script))
    if alias:
        raise PermissionError(f"PowerShell alias that can change things: {alias.group(1)}")
    bad = sorted({f"{v}-{n}" for v, n in _CMDLET.findall(script)
                  if v.lower() not in _READ_LOWER and v.lower() in _ALL_LOWER})
    if bad:
        raise PermissionError(f"PowerShell verbs that can change things: {', '.join(bad)}")


# Verbs PowerShell uses for cmdlets (from Get-Verb); words like "Microsoft-Windows" aren't cmdlets.
_ALL_VERBS = READ_VERBS | {
    "Add", "Clear", "Close", "Copy", "Enter", "Exit", "Find", "Hide", "Lock", "Move", "New", "Open", "Optimize",
    "Pop", "Push", "Redo", "Remove", "Rename", "Reset", "Resize", "Search", "Set", "Show", "Skip", "Step",
    "Switch", "Undo", "Unlock", "Watch", "Connect", "Disconnect", "Read", "Receive", "Send", "Write", "Backup",
    "Checkpoint", "Expand", "Compress", "Export", "Import", "Initialize", "Limit", "Merge", "Mount", "Dismount",
    "Out", "Publish", "Unpublish", "Restore", "Save", "Sync", "Unblock", "Update", "Approve", "Assert", "Build",
    "Complete", "Confirm", "Deny", "Deploy", "Disable", "Enable", "Install", "Invoke", "Register", "Request",
    "Restart", "Resume", "Start", "Stop", "Submit", "Suspend", "Uninstall", "Unregister", "Wait", "Debug",
    "Repair", "Trace", "Use", "Block", "Grant", "Protect", "Revoke", "Unprotect", "Edit",
}


_READ_LOWER = {v.lower() for v in READ_VERBS}
_ALL_LOWER = {v.lower() for v in _ALL_VERBS}


def _strip_strings(script: str) -> str:
    """Drop quoted strings so words inside them (e.g. 'start') aren't taken for aliases."""
    return re.sub(r"'[^']*'|\"[^\"]*\"", "''", script)


def _exec(argv: list[str], timeout: float, allow_fail: bool, cwd: Optional[str] = None) -> Optional[str]:
    exe = shutil.which(argv[0])
    if not exe:
        return None
    try:
        proc = subprocess.run([exe, *argv[1:]], capture_output=True, encoding="utf-8", errors="replace",
                              timeout=timeout, check=False, stdin=subprocess.DEVNULL, cwd=cwd,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 or allow_fail else None


def run(cmd: list[str], timeout: float = 20, allow_fail: bool = False, cwd: Optional[str] = None) -> Optional[str]:
    """Run an allowed read-only command (in folder `cwd`). Returns stdout, or None if missing or failed."""
    check(cmd)
    return _exec(list(cmd), timeout, allow_fail, cwd)


def powershell(script: str, timeout: float = 40) -> Optional[str]:
    """Run a read-only PowerShell script (Windows). Returns stdout, or None."""
    check_powershell(script)
    exe = "powershell" if shutil.which("powershell") else "pwsh"
    return _exec([exe, "-NoProfile", "-NonInteractive", "-Command", POWERSHELL_PRELUDE + script], timeout, True)


def powershell_json(script: str, timeout: float = 40) -> Optional[list]:
    """Run a read-only PowerShell script that ends in ConvertTo-Json; always returns a list (or None)."""
    out = powershell(script, timeout)
    if not out or not out.strip():
        return None if out is None else []
    try:
        data = json.loads(out)
    except ValueError:
        return None
    return data if isinstance(data, list) else [data]
