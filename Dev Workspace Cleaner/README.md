# Dev Workspace Cleaner

Finds rebuildable build folders (node_modules, virtualenvs, caches, build outputs) across your projects and shows which projects still hold uncommitted or unpushed git work.

Part of [Streamlit Utilities](../README.md). **Read-only:** it only looks and never changes, deletes, installs or
stops anything. Suggested commands are shown for you to review and run yourself.

## Start it

| | |
|---|---|
| **Linux** | `./run.sh` (from any folder), or right-click `run.sh` → *Run as a Program* |
| **Windows** | double-click `run.bat` |
| **Either** | `python app.py` |

Opens http://localhost:8518 (reachable from this computer only). The first run installs missing Python
packages into `.venv/` here (internet needed once).

## What it shows

Press **Scan for build folders** (Home folder by default; Desktop or any folder from the sidebar). The scan walks
the folder without following symlinks or junctions, skips caches, the Trash, app data and installed tools
(`~/.cache`, `~/.local/share`, `~/snap`, hidden folders in your home folder; `AppData` on Windows) and `.git`
internals, and only measures the inside of each build folder. A home folder usually takes seconds.

- **Build folders, only when they are genuine:** each name needs a marker beside it, e.g. `node_modules` next to
  `package.json`, `target` next to `Cargo.toml`/`pom.xml`, `build`/`dist` next to `package.json`, `setup.py`,
  `pyproject.toml`, `build.gradle`, `CMakeLists.txt` or `pubspec.yaml`, `bin`+`obj` next to a `.csproj`, `Pods` next
  to a `Podfile`. Virtualenvs are found by their `pyvenv.cfg`. Also `__pycache__`, `.pytest_cache`, `.mypy_cache`,
  `.ruff_cache`, `.tox`, `.nox`, `.next`, `.nuxt`, `.svelte-kit`, `.parcel-cache`, `.angular`, `.turbo`, `.expo`,
  `.gradle`, `cmake-build-*` and `.dart_tool`.
- **Levels:** 🟢 Safe = caches that rebuild themselves; 🟡 Low = dependencies and build output that need a reinstall
  or rebuild; 🔍 Review = virtualenvs with no `requirements.txt`, `pyproject.toml`, `Pipfile` or `environment.yml`
  beside them (hard to recreate).
- **Overview:** reclaimable space, projects, build folders and repositories with unsaved work; bars by type and for
  the biggest projects. The sidebar filter "Untouched for at least" (1–12 months) uses each project's last activity
  (newest change to its own files, not counting build folders).
- **Projects:** a table per project (size, build folders, last activity, git branch, uncommitted changes, unpushed
  commits, stashes, last commit) with search and filters, and a CSV download.
- **Findings:** one per project and level, with a command for you to review and run that deletes just those folders
  (`rm -rf '…'` on Linux, `Remove-Item -LiteralPath '…' -Recurse -Force` on Windows) plus how to get them back
  (`npm ci`, `python -m venv .venv && …/pip install -r requirements.txt`, `cargo build`, …).
- **Git: unsaved work:** repositories under the scan root with uncommitted changes, commits on no remote, or stashes.
  It's separate from build folders, but worth checking before deleting a whole project. Read with
  `git --no-optional-locks` (status, stash list, for-each-ref, rev-list, log) inside each repository.

## Layout

```
app.py              Streamlit UI
core/               helpers shared by all Streamlit Utilities apps, plus this app's own logic
core/workspace.py   this app's scanner, git checks and findings
launch.py           launcher used by run.sh / run.bat / python app.py
tests/              python3 -m pytest -q   (includes the read-only check)
```
