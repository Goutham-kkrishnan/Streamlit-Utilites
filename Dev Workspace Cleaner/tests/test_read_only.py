"""The read-only guarantee, checked on the code itself (same file in every Streamlit Utilities app).

1. No Python file may write, delete, move, kill, start processes or send data over the network,
   except: launch.py (sets up .venv), core/history.py (the app's own history), core/readonly.py
   (the guarded way to run read-only commands) and core/system.py (a writability probe).
2. Every command in core/readonly.ALLOWED must pass the guard's read-only rules.
3. The guard refuses commands and PowerShell scripts that change things.
Run: python3 -m pytest -q
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))

from core import readonly  # noqa: E402

EXEMPT = {"launch.py", "core/history.py", "core/readonly.py", "core/system.py"}
SKIP_DIRS = {"tests", ".venv", "history", "__pycache__"}

# module -> functions that change things (None = every function in the module)
FORBIDDEN_CALLS: dict[str, set[str] | None] = {
    "os": {"remove", "unlink", "rmdir", "removedirs", "rename", "renames", "replace", "kill", "killpg", "chmod",
           "lchmod", "chown", "lchown", "link", "symlink", "mkdir", "makedirs", "truncate", "system", "popen",
           "startfile", "utime", "setxattr", "removexattr", "mkfifo", "mknod", "open", "fork", "execv", "execve",
           "execvp", "execvpe", "execl", "execle", "execlp", "execlpe", "spawnv", "spawnve", "spawnvp",
           "spawnvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe", "posix_spawn", "posix_spawnp", "setpriority",
           "nice", "sched_setaffinity", "putenv", "unsetenv", "chdir", "chroot", "sync"},
    "shutil": {"rmtree", "move", "copy", "copy2", "copyfile", "copytree", "copymode", "copystat", "chown",
               "make_archive", "unpack_archive"},
    "subprocess": None,
    "pty": None,
    "ctypes": None,
    "sqlite3": {"connect"},
    "tempfile": {"mkstemp", "mkdtemp", "NamedTemporaryFile", "TemporaryDirectory", "TemporaryFile",
                 "SpooledTemporaryFile"},
    "signal": {"pthread_kill", "raise_signal"},
    "urllib.request": None, "http.client": None, "requests": None, "smtplib": None, "ftplib": None,
    "webbrowser": None, "ssl": None,
}
FORBIDDEN_IMPORTS = {"subprocess", "pty", "ctypes", "requests", "urllib.request", "http.client", "smtplib", "ftplib",
                     "webbrowser", "multiprocessing", "asyncio.subprocess"}
# Method names that change files or processes, whatever object they are called on.
FORBIDDEN_METHODS = {"unlink", "rmdir", "touch", "mkdir", "write_text", "write_bytes", "symlink_to", "hardlink_to",
                     "chmod", "lchmod", "rmtree", "kill", "terminate", "suspend", "resume", "send_signal",
                     "makedirs", "removedirs", "write_image", "write_html", "to_excel", "to_parquet",
                     "to_pickle", "to_sql", "to_feather", "to_hdf", "to_stata"}
# Methods that only change things when given arguments (psutil setters, pandas exports to a path).
SETTER_METHODS = {"nice", "ionice", "cpu_affinity", "rlimit", "to_csv", "to_json", "to_markdown", "to_html",
                  "to_latex", "to_string", "to_xml"}
WINREG_WRITE_PREFIXES = ("Set", "Delete", "Create", "Save", "Load", "Replace", "Flush", "Disable", "Enable")


def _app_files() -> list[Path]:
    out = []
    for root, dirs, files in os.walk(APP):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if f.endswith(".py"):
                p = Path(root) / f
                if p.relative_to(APP).as_posix() not in EXEMPT:
                    out.append(p)
    return out


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _write_mode(call: ast.Call, mode_index: int) -> bool:
    mode = None
    if len(call.args) > mode_index:
        mode = call.args[mode_index]
    for kw in call.keywords:
        if kw.arg == "mode":
            mode = kw.value
    if mode is None:
        return False
    if not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)):
        return True  # can't tell, so treat as writing
    return any(c in mode.value for c in "wax+")


def violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rel = path.relative_to(APP).as_posix()
    aliases: dict[str, str] = {}  # local name -> module (or module.function)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                aliases[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
                if a.name in FORBIDDEN_IMPORTS:
                    found.append(f"{rel}:{node.lineno} imports {a.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            mod = node.module
            for a in node.names:
                full = f"{mod}.{a.name}"
                aliases[a.asname or a.name] = full
                banned = FORBIDDEN_CALLS.get(mod, set())
                if mod in FORBIDDEN_IMPORTS or full in FORBIDDEN_IMPORTS or banned is None or a.name in (banned or ()):
                    found.append(f"{rel}:{node.lineno} imports {full}")
                if mod == "winreg" and a.name.startswith(WINREG_WRITE_PREFIXES):
                    found.append(f"{rel}:{node.lineno} imports winreg.{a.name}")
        elif isinstance(node, ast.Call):
            name = _dotted(node.func)
            head, _, rest = name.partition(".")
            resolved = f"{aliases.get(head, head)}.{rest}" if rest else aliases.get(head, head)
            mod, _, func = resolved.rpartition(".")
            banned = FORBIDDEN_CALLS.get(mod, set())
            if mod in FORBIDDEN_CALLS and (banned is None or func in banned):
                found.append(f"{rel}:{node.lineno} calls {resolved}")
            if mod == "winreg" and func.startswith(WINREG_WRITE_PREFIXES):
                found.append(f"{rel}:{node.lineno} calls {resolved}")
            if resolved in ("open", "io.open", "builtins.open") and _write_mode(node, 1):
                found.append(f"{rel}:{node.lineno} opens a file for writing")
            if isinstance(node.func, ast.Attribute):
                meth = node.func.attr
                if meth in FORBIDDEN_METHODS:
                    found.append(f"{rel}:{node.lineno} calls .{meth}()")
                elif meth in SETTER_METHODS and (node.args or any(k.arg in ("path_or_buf", "buf") for k in node.keywords)):
                    found.append(f"{rel}:{node.lineno} calls .{meth}() with arguments")
                elif meth == "open" and _write_mode(node, 0):
                    found.append(f"{rel}:{node.lineno} opens a file for writing")
    return found


def test_no_code_changes_files_or_the_system():
    problems = [v for f in _app_files() for v in violations(f)]
    assert not problems, "Read-only rule broken:\n" + "\n".join(problems)


@pytest.mark.parametrize("prefix", readonly.ALLOWED or [("true",)], ids=lambda p: " ".join(p))
def test_every_allowed_command_is_read_only(prefix):
    if prefix == ("true",):
        pytest.skip("this app runs no external commands")
    readonly.check(list(prefix))


@pytest.mark.parametrize("cmd", [
    ["rm", "-rf", "/tmp/x"], ["git", "push"], ["git", "stash", "drop"], ["systemctl", "disable", "ssh"],
    ["snap", "remove", "firefox"], ["apt-get", "autoremove"], ["crontab", "/tmp/evil"], ["ip", "addr", "add", "x"],
    ["powercfg", "/batteryreport"], ["smartctl", "-t", "long", "/dev/sda"], ["journalctl", "--vacuum-size=1M"],
    ["powershell", "-Command", "Get-Process"], ["schtasks", "/delete", "/tn", "x"],
])
def test_guard_refuses_commands_that_change_things(cmd, monkeypatch):
    monkeypatch.setattr(readonly, "ALLOWED", [tuple(cmd[:1])])  # even if someone allowed the program
    with pytest.raises(PermissionError):
        readonly.check(cmd)


@pytest.mark.parametrize("script", [
    "Remove-Item C:\\x -Recurse", "remove-item x", "Stop-Service wuauserv", "Get-Process | Stop-Process",
    "Set-ItemProperty -Path HKCU:\\x -Name y -Value 1", "Invoke-Expression 'x'", "rm x", "kill 1234",
    "Get-ChildItem > out.txt", "(Get-WmiObject Win32_Process).Terminate()", "New-Item x", "Disable-ScheduledTask x",
])
def test_guard_refuses_powershell_that_changes_things(script):
    with pytest.raises(PermissionError):
        readonly.check_powershell(script)


def test_guard_accepts_read_only_powershell():
    readonly.check_powershell(
        "Get-CimInstance Win32_Service | Where-Object { $_.StartMode -eq 'Auto' } | "
        "Select-Object Name, State | Sort-Object Name | ConvertTo-Json -Compress\n"
        "$x = Get-WinEvent -LogName 'Microsoft-Windows-Diagnostics-Performance/Operational' 2>$null")
