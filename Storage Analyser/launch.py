"""Start Storage Analyser on Linux or Windows: install what's missing, then start the local server.

Used by run.sh (Linux), run.bat (Windows) and `python app.py`. Standard library only.
Everything is relative to this file, so the app folder can live anywhere and be copied
to another computer. If this Python lacks the packages in requirements.txt, they are
installed into .venv inside the app folder (internet needed once); nothing system-wide.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import urllib.request
import webbrowser

APP_DIR = os.path.dirname(os.path.abspath(__file__))
IS_WINDOWS = os.name == "nt"

# Exits 0 only if every package in requirements.txt is installed at its minimum version.
CHECK_PACKAGES = r"""
import re, sys
from importlib.metadata import version
def v(text):
    return tuple(int(n) for n in re.findall(r"\d+", text)[:3])
for line in open("requirements.txt", encoding="utf-8"):
    line = line.split("#")[0].strip()
    if line:
        name, _, minimum = line.partition(">=")
        if v(version(name.strip())) < v(minimum):
            sys.exit(1)
"""


def fail(message: str) -> None:
    print(f"Storage Analyser: {message}", file=sys.stderr)
    sys.exit(1)


def has_packages(python: str) -> bool:
    try:
        return subprocess.run([python, "-c", CHECK_PACKAGES], cwd=APP_DIR, capture_output=True,
                              timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def venv_python(venv: str) -> str:
    return os.path.join(venv, "Scripts", "python.exe") if IS_WINDOWS else os.path.join(venv, "bin", "python")


def pick_python() -> str:
    """A Python that has the app's packages, setting up .venv on the first run on this computer."""
    from analyser.system import is_writable, user_data_dir

    venv = os.path.join(APP_DIR, ".venv")
    if not is_writable(APP_DIR):  # e.g. copied to a read-only location
        venv = os.path.join(user_data_dir(), "venv")
    vpy = venv_python(venv)
    if os.path.exists(vpy) and has_packages(vpy):
        return vpy
    if has_packages(sys.executable):
        return sys.executable

    print(f"Setting up Python packages in {venv} (first run on this computer; needs internet)…", flush=True)
    pip_ok = os.path.exists(vpy) and subprocess.run([vpy, "-m", "pip", "--version"],
                                                    capture_output=True).returncode == 0
    if not pip_ok:  # missing, half-created, or copied from another computer (venvs don't move between machines)
        shutil.rmtree(venv, ignore_errors=True)
        os.makedirs(os.path.dirname(venv), exist_ok=True)
        if subprocess.run([sys.executable, "-m", "venv", venv]).returncode != 0:
            shutil.rmtree(venv, ignore_errors=True)
            fail("Could not create a Python virtual environment."
                 + ("" if IS_WINDOWS else " On Debian/Ubuntu: sudo apt install python3-venv"))
    if subprocess.run([vpy, "-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements.txt"],
                      cwd=APP_DIR).returncode != 0:
        fail("Installing the Python packages failed (see the messages above).")
    if not has_packages(vpy):
        fail("The Python packages were installed but don't meet requirements.txt.")
    return vpy


def server_port(args: list[str]) -> int:
    """Port from .streamlit/config.toml, or from --server.port given on the command line."""
    port = 8517
    try:
        with open(os.path.join(APP_DIR, ".streamlit", "config.toml"), encoding="utf-8") as fh:
            m = re.search(r"^port\s*=\s*(\d+)", fh.read(), re.MULTILINE)
            port = int(m.group(1)) if m else port
    except OSError:
        pass
    for i, arg in enumerate(args):
        if arg.startswith("--server.port="):
            port = int(arg.split("=", 1)[1])
        elif arg == "--server.port" and i + 1 < len(args):
            port = int(args[i + 1])
    return port


def already_running(port: int) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=2)
        return True
    except OSError:
        return False


def main() -> int:
    if sys.version_info < (3, 10):
        fail(f"Python 3.10 or newer is needed, but this is Python {sys.version.split()[0]}.")
    os.chdir(APP_DIR)  # Streamlit reads .streamlit/config.toml (local-only settings) from here
    sys.path.insert(0, APP_DIR)
    args = sys.argv[1:]
    python = pick_python()

    port = server_port(args)
    url = f"http://localhost:{port}"
    if already_running(port):  # e.g. launched twice: just open it
        print(f"Storage Analyser is already running at {url}")
        webbrowser.open(url)
        return 0

    print(f"Starting Storage Analyser at {url}  (press Ctrl+C here, or close this window, to stop it)", flush=True)
    proc = subprocess.Popen([python, "-m", "streamlit", "run", "app.py", *args], cwd=APP_DIR)
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            continue  # Ctrl+C also reaches Streamlit; wait for it to shut down cleanly


if __name__ == "__main__":
    sys.exit(main())
