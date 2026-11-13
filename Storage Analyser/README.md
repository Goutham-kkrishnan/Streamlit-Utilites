# Storage Analyser

A local, read-only dashboard that shows where your disk space goes and what you can safely free.
Runs on **Linux** and **Windows**.

## Start it

| | |
|---|---|
| **Linux** | `./run.sh` in this folder, or `"/path/to/Storage Analyser/run.sh"` from anywhere. In the Files app: right-click `run.sh` → *Run as a Program* (it opens a terminal; close it to stop the app). |
| **Windows** | Double-click `run.bat` (close the window to stop the app), or run it from a Command Prompt. |
| **Either** | `python app.py` (or `python3 app.py`) also works. |

The browser opens at http://localhost:8517. Starting it a second time just opens the browser again.

Pick a location in the sidebar (Home, `/` or a drive such as `C:`, Desktop, Downloads or any folder) and
press **Scan**. A full scan of a home folder (~320k files) takes about 6–12 seconds on Linux; Windows is slower.

The **System** switch at the top of the sidebar shows which operating system the app is running on. It is
detected automatically; scan locations, the Disks tab and the Cleanup checks follow it.

## Moving it to another computer

Copy the whole folder anywhere on another Linux or Windows computer and start it as above. Nothing in the
app refers to a fixed location: every path is found relative to the app folder.

- **Needs Python 3.10 or newer.** Linux usually has it (on Debian/Ubuntu also `sudo apt install python3-venv`).
  On Windows, install it from [python.org](https://www.python.org/downloads/) and tick *Add python.exe to PATH*.
- **First run:** if Python doesn't already have the packages in `requirements.txt`, the launcher installs them
  into `.venv/` inside the app folder. That needs internet once, and nothing is installed system-wide.
  A `.venv` copied from another computer is detected and rebuilt automatically.
- **History stays per computer.** Snapshots are tagged with an anonymous ID of the computer that made them
  (a hash, not the real machine ID), so a copied folder never mixes two computers' disks. Snapshots from
  other computers stay in `snapshots/` but are hidden, and the History tab says how many.
- If the app folder is read-only, `.venv` and snapshots go to `~/.local/share/storage-analyser` (Linux) or
  `%LOCALAPPDATA%\StorageAnalyser` (Windows) instead.

## Tabs

| Tab | What it shows |
|---|---|
| **Disks** | Every mounted partition or drive: used/free, inode usage (Linux), and all disks and partitions, including unmounted ones (`lsblk` on Linux, PowerShell on Windows). Works without a scan. |
| **Overview** | Totals, a treemap or sunburst of the folder tree, top-level folders, and the folders that directly hold the most data. Warns when part of a partition is unreadable to your user. |
| **Explorer** | Drill down folder by folder (click a row). Shows subfolder sizes, a chart, a mini treemap and the largest files in the current folder. |
| **Large files** | Every file above the threshold (10 MB by default). Search, filter by category or age, and export as CSV. |
| **File types** | Space by category and by extension, plus the file-size distribution. |
| **Age** | Space by last-modified or last-accessed time, and large files nobody has touched for months or years. |
| **Duplicates** | Byte-identical files (size → partial hash → full BLAKE2 hash), ranked by wasted space. |
| **Cleanup** | **Linux:** Docker, snap revisions, journal and rotated logs, APT cache, old and leftover kernels, crash reports, old nvm Node versions. **Windows:** temporary files, Windows Update downloads, Windows.old, the hibernation file, crash dumps and error reports, the Recycle Bin, Docker. **Both:** pip/npm/NuGet/Gradle/conda and ML caches, VS Code and browser caches, browser AI models and stale downloads. Each item has a risk level and a suggested command (bash on Linux, PowerShell on Windows). |
| **History** | Every scan saves a snapshot, so you can compare scans to see which folders grew or shrank, and how disk usage changes over time. |

## Notes

- **Read-only.** The app never deletes or changes files. The Cleanup tab only *suggests* commands; review them and run them yourself.
- **Local only.** The server binds to `127.0.0.1` (see `.streamlit/config.toml`) and usage statistics are off. Nothing leaves your machine. If it is ever started without those settings (for example `streamlit run` from another folder on an old Streamlit version), the page refuses to show anything.
- **Units.** 1 GB = 10⁹ bytes, as in Ubuntu's Files app. `df -h`, `du -h` and Windows Explorer count in GiB (2³⁰ bytes), so their numbers look about 7% smaller.
- **On disk vs apparent size.** "On disk" counts allocated blocks, the same as `du`; hard links are counted once. "Apparent" is the file length, which is larger for sparse VM and Docker images.
- **Permissions.** As a normal user, folders such as `/var/lib/docker` and `/root` (Linux) or `System Volume Information` and other users' folders (Windows) can't be read. When you scan a whole partition or drive, the Overview shows how much space that hides, and the Cleanup tab measures the usual suspects separately.
- **Windows specifics.** "On disk" is estimated from 4 KB clusters (online-only OneDrive files count as 0). Junctions are skipped like symlinks. Hard links can't be detected cheaply, so files hard-linked into several folders (mainly `C:\Windows\WinSxS`) are counted in each. Windows often doesn't record access times, so "last accessed" in the Age tab may not mean much.
- **Snapshots** are stored in `snapshots/` next to the app (the last 40 per folder, per computer). On Linux the files are owner-only; on Windows they get the folder's permissions, so keep the app inside your user folder if others use the PC. Delete that folder to clear the history.

## Layout

```
run.sh / run.bat        launchers for Linux / Windows (work from any folder)
launch.py               shared launcher: sets up .venv if needed, starts the local server
app.py                  Streamlit UI
analyser/system.py      Linux/Windows differences (OS detection, user folders, on-disk sizes)
analyser/scanner.py     single-pass filesystem walk and aggregation
analyser/disks.py       partitions, inodes, disk list (lsblk / PowerShell), mount handling
analyser/duplicates.py  duplicate finder
analyser/cleanup.py     cleanup advisor (measures only)
analyser/history.py     snapshots and comparisons
analyser/charts.py      Plotly charts (colour-blind-safe palette, light and dark)
tests/                  python3 -m pytest -q
```

Requires Python 3.10+, with `streamlit`, `plotly`, `pandas` and `psutil`. The launchers install them when
they're missing; to do it yourself: `pip install -r requirements.txt`.
