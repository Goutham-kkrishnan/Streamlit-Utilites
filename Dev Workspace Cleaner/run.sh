#!/usr/bin/env bash
# Start Dev Workspace Cleaner on http://localhost:8518 (reachable from this computer only). Linux;
# on Windows use run.bat.
#
# Works from any directory (./run.sh, bash run.sh, a symlink, or right-click > "Run as a Program"
# in the Files app). launch.py does the rest: it installs missing Python packages into .venv/ in
# this folder on the first run (needs internet once) and starts the server.
set -euo pipefail

APP_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
cd "$APP_DIR"

# Started from a file manager: there is no terminal, so reopen in one to show progress and
# errors. Closing that window stops the app.
if [ "${1:-}" != "--in-terminal" ] && [ ! -t 0 ] && [ ! -t 1 ] && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    self=(bash "$APP_DIR/run.sh" --in-terminal "$@")
    for t in gnome-terminal kgx konsole xfce4-terminal mate-terminal xterm x-terminal-emulator; do
        command -v "$t" >/dev/null 2>&1 || continue
        case "$t" in
            gnome-terminal | kgx) exec "$t" -- "${self[@]}" ;;
            xfce4-terminal | mate-terminal) exec "$t" -x "${self[@]}" ;;
            *) exec "$t" -e "${self[@]}" ;;
        esac
    done
    # No terminal emulator found: carry on without one (the browser still opens).
fi

if [ "${1:-}" = "--in-terminal" ]; then
    shift
    # Keep the window open after an error so it can be read (130 = stopped with Ctrl+C).
    trap 'rc=$?; if [ "$rc" -ne 0 ] && [ "$rc" -ne 130 ]; then echo; read -rp "Press Enter to close… " _ || true; fi' EXIT
fi

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "$(basename "$APP_DIR"): python3 was not found. Install Python 3.10 or newer" \
        "(Debian/Ubuntu: sudo apt install python3 python3-venv)." >&2
    exit 1
fi
"$PYTHON" launch.py "$@"
