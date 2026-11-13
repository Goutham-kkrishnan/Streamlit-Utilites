"""Storage Analyser — a local, read-only dashboard for disk usage.

Run with ./run.sh (Linux), run.bat (Windows) or `python app.py`, from any folder.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime

if __name__ == "__main__" and "streamlit" not in sys.modules:
    # Started as `python app.py`: hand over to the launcher, which installs what's missing and starts the server.
    import runpy

    sys.argv[0] = os.path.join(os.path.dirname(os.path.realpath(__file__)), "launch.py")
    runpy.run_path(sys.argv[0], run_name="__main__")

import pandas as pd
import streamlit as st

from analyser import charts, cleanup, disks, duplicates, history
from analyser.scanner import ScanResult, list_folder_files, scan
from analyser.system import HOME, IS_WINDOWS, LAUNCHER, OS_NAME, OTHER_OS, SHELL_LANGUAGE, user_folder
from analyser.utils import (AGE_BUCKETS, CATEGORIES, DAY, MB, SIZE_BUCKETS, category_of, display_path,
                            file_extension, fmt_time, human_size)

st.set_page_config(page_title="Storage Analyser", page_icon="💽", layout="wide")

# The app lists your files, so it must only be reachable from this computer. Streamlit applies
# .streamlit/config.toml only when started from this folder (run.sh, run.bat and `python app.py` do that).
if st.get_option("server.address") not in ("127.0.0.1", "localhost", "::1"):
    st.error("Storage Analyser was started without its local-only settings, so it could be reachable from "
             f"other computers. Stop it and start it with `{LAUNCHER}` (or `python app.py`) instead.")
    st.stop()

SS = st.session_state
PLOTLY_CONFIG = {"displaylogo": False, "modeBarButtonsToRemove": ["lasso2d", "select2d", "autoScale2d"]}


def theme_mode() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:
        return "light"


MODE = theme_mode()


def size_col(label: str, help: str | None = None):
    return st.column_config.NumberColumn(label, format="bytes", help=help)


def share_col(label: str = "Share"):
    return st.column_config.ProgressColumn(label, format="%.1f%%", min_value=0.0, max_value=100.0)


def date_col(label: str):
    return st.column_config.DatetimeColumn(label, format="YYYY-MM-DD")


def show(fig, key: str):
    st.plotly_chart(fig, theme=None, config=PLOTLY_CONFIG, key=key)


def local_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts)


def rel(path: str, root: str) -> str:
    return "." if path == root else os.path.relpath(path, root)


# ============================================================================ sidebar

OS_ICON = {"Linux": "🐧", "Windows": "🪟"}


def presets() -> dict[str, str | None]:
    if IS_WINDOWS:
        drives = {f"Whole drive  ( {p['mountpoint'].rstrip(os.sep)} )": p["mountpoint"] for p in disks.partitions()}
        places = {"Home folder": HOME, **drives}
    else:
        places = {"Home folder": HOME, "Whole system  ( / )": "/"}
    return {**places, "Desktop": user_folder("desktop"), "Downloads": user_folder("downloads"), "Custom path…": None}


PRESETS = presets()

with st.sidebar:
    # Follows the computer the app runs on; the other system is shown greyed out.
    st.segmented_control("System", ["Linux", "Windows"], default=OS_NAME, disabled=True,
                         format_func=lambda o: f"{OS_ICON[o]} {o}",
                         help="Detected automatically. Scan locations, the Disks tab and the Cleanup checks "
                              "match this system.")
    st.caption(f"This computer runs **{OS_NAME}**. To analyse a {OTHER_OS} computer, copy this folder there and "
               f"start `{'run.sh' if IS_WINDOWS else 'run.bat'}`.")
    st.divider()
    st.header("Scan")
    choice = st.selectbox("Location", list(PRESETS))
    target = PRESETS[choice]
    if target is None:
        target = st.text_input("Folder path", value=SS.get("custom_path", HOME),
                               placeholder=r"C:\path\to\folder" if IS_WINDOWS else "/path/to/folder")
        SS.custom_path = target
    else:
        st.caption(f"`{target}`")

    with st.expander("Scan options"):
        if IS_WINDOWS:  # junctions and mounted folders are never followed on Windows
            one_fs = True
        else:
            one_fs = st.toggle("Stay on one filesystem", value=True,
                               help="Like `du -x`: don't descend into other mounted disks. Recommended for /.")
        large_mb = st.number_input("Track files from (MB)", min_value=1, max_value=100_000, value=10,
                                   help="Files at least this big are listed under Large files and Age.")
        dup_mb = st.number_input("Duplicate check from (MB)", min_value=1, max_value=100_000, value=1,
                                 help="Smaller files are ignored by the duplicate finder.")
        save_snap = st.toggle("Save snapshot for History", value=True,
                              help="Keeps folder totals in this app's snapshots/ folder to compare later scans.")

    go_scan = st.button("Scan", type="primary", icon=":material/search:", width="stretch")

    st.divider()
    metric_label = st.radio("Measure sizes as", ["On disk", "Apparent size"],
                            help=("On disk: estimated from 4 KB clusters (Windows doesn't report it cheaply); "
                                  "online-only OneDrive files count as 0. Apparent: file lengths, as Explorer's "
                                  "'Size'." if IS_WINDOWS else
                                  "On disk: blocks actually used, matches du/df. Apparent: file lengths — "
                                  "differs for sparse files (VM and Docker images) and many tiny files."))
    METRIC = "alloc" if metric_label == "On disk" else "size"
    st.caption("Sizes use 1 GB = 10⁹ bytes. Explorer counts in GiB but labels them GB, so its numbers look about "
               "7% smaller." if IS_WINDOWS else
               "Sizes use 1 GB = 10⁹ bytes (like the Files app). `df -h` shows GiB, about 7% smaller.")
    st.caption("🔒 Runs locally on 127.0.0.1 and never deletes anything.")


# ============================================================================ scanning

def run_scan(path: str) -> None:
    path = path.strip().strip('"')
    if IS_WINDOWS and len(path) == 2 and path[1] == ":":
        path += "\\"  # "D:" alone means the current folder on D:, not the drive
    path = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(path):
        st.error(f"Not a folder: `{display_path(path)}`")
        return
    if not os.access(path, os.R_OK | os.X_OK):
        st.error(f"No permission to read `{display_path(path)}`")
        return
    with st.status(f"Scanning {display_path(path)} …", expanded=True) as status:
        line = st.empty()

        def progress(files: int, folders: int, nbytes: int, current: str) -> None:
            line.markdown(f"**{files:,}** files · **{folders:,}** folders · **{human_size(nbytes)}**  \n"
                          f"`…{display_path(current)[-100:]}`")

        try:
            result = scan(path, one_filesystem=one_fs, large_file_min=int(large_mb * MB),
                          dup_min=int(dup_mb * MB), exclude=disks.pseudo_mountpoints(), progress=progress)
        except OSError as exc:
            status.update(label=f"Scan failed: {exc}", state="error")
            return
        status.update(label="Scan complete", state="complete", expanded=False)

    summary = history.dir_summary(result)
    SS.result, SS.summary, SS.cwd, SS.nav = result, (result.id, summary), result.root, SS.get("nav", 0) + 1
    SS.pop("dups", None)
    SS.flash = (f"Scanned {display_path(result.root)} in {result.duration:.1f}s — "
                f"{result.file_count:,} files, {human_size(result.total('alloc'))} on disk")
    if save_snap:
        try:
            history.save_snapshot(result, disks.partitions(), summary)
        except OSError as exc:
            SS.flash += f" (snapshot not saved: {exc})"
    st.rerun()


if go_scan:
    run_scan(target)

if "flash" in SS:
    st.toast(SS.pop("flash"), icon="✅")

res: ScanResult | None = SS.get("result")

st.title("💽 Storage Analyser")
if res:
    st.caption(f"Showing **{display_path(res.root)}** · scanned {fmt_time(res.scanned_at)} in {res.duration:.1f}s"
               f" · sizes {metric_label.lower()}")
else:
    st.caption("Pick a location in the sidebar and press **Scan**. The Disks tab works without a scan.")


def need_scan() -> bool:
    if res is None:
        st.info("Run a scan first: choose a location in the sidebar and press **Scan**.", icon=":material/search:")
        return True
    return False


# ============================================================================ tabs

def tab_disks() -> None:
    parts = disks.partitions()
    if not parts:
        st.info("No disk partitions found.")
        return
    cols = st.columns(min(len(parts), 3))
    for i, p in enumerate(parts):
        pct = p["percent"]
        status = ("🔴 Almost full" if pct >= 90 else "🟠 Getting full" if pct >= 75 else "🟢 Healthy")
        with cols[i % len(cols)].container(border=True):
            st.markdown(f"**{p['mountpoint']}** &nbsp; {status}")
            st.caption(f"{p['device']} · {p['fstype']}" + (" · read-only" if p["read_only"] else ""))
            st.progress(min(pct, 100) / 100,
                        text=f"{human_size(p['used'])} used of {human_size(p['total'])} · "
                             f"**{human_size(p['free'])} free**")
            if p["inodes_total"]:
                iu = p["inodes_total"] - p["inodes_free"]
                st.caption(f"Inodes (file slots): {iu:,} of {p['inodes_total']:,} used ({iu / p['inodes_total']:.0%})")
    show(charts.partition_usage(parts, MODE), "disks_usage")

    st.subheader("All disks and partitions")
    devs = disks.block_devices()
    if devs:
        df = pd.DataFrame(devs)
        st.dataframe(
            df, hide_index=True,
            column_order=["device", "type", "size", "fstype", "label", "mountpoint", "used", "free", "kind", "model"],
            column_config={
                "device": "Device", "type": "Type", "size": size_col("Size"), "fstype": "Filesystem",
                "label": "Label", "mountpoint": "Mounted at", "used": size_col("Used"), "free": size_col("Free"),
                "kind": "Drive", "model": "Model",
            })
        st.caption("Includes partitions that aren't mounted, such as another operating system's. "
                   "Used/Free are only known for mounted filesystems." if not IS_WINDOWS else
                   "Includes partitions without a drive letter, such as recovery or another operating system's. "
                   "Used/Free are only known for partitions Windows can read.")
    else:
        st.caption("Couldn't list the disks: " + ("PowerShell's Storage module didn't answer." if IS_WINDOWS
                                                  else "`lsblk` is not available."))


def tab_overview(r: ScanResult) -> None:
    sizes, own = r.sizes(METRIC), r.own(METRIC)
    total = sizes.get(r.root, 0)
    big_total = sum(max(f.size, f.alloc) if METRIC == "size" else f.alloc for f in r.large_files)
    c = st.columns(5)
    c[0].metric("Total size", human_size(total), border=True)
    c[1].metric("Files", f"{r.file_count:,}", border=True)
    c[2].metric("Folders", f"{r.dir_count:,}", border=True)
    c[3].metric(f"Files ≥ {human_size(r.large_file_min)}", f"{len(r.large_files):,}", border=True,
                help=f"Together {human_size(big_total)} ({big_total / (total or 1):.0%} of this scan)")
    c[4].metric("Couldn't read", f"{r.error_count:,}", border=True,
                help="Entries skipped because of permissions or because they vanished during the scan.")

    mp = disks.mountpoint_of(r.root)
    part = next((p for p in disks.partitions() if p["mountpoint"] == mp), None)
    if part and part["used"]:
        scanned = r.total("alloc")
        st.caption(f"This folder accounts for **{scanned / part['used']:.0%}** of the space used on `{mp}` "
                   f"({human_size(part['used'])} used, {human_size(part['free'])} free).")
        hidden = part["used"] - scanned
        if r.root == mp and r.one_filesystem and hidden > max(1e9, 0.03 * part["used"]):
            examples, suspects = (("System Volume Information, other users' folders or WindowsApps",
                                   "the hibernation file, Windows Update and Windows.old") if IS_WINDOWS else
                                  ("/var/lib/docker or /root", "Docker, snaps and logs"))
            st.warning(f"About **{human_size(hidden)}** on `{mp}` sits in folders your user can't read "
                       f"(for example {examples}), so it isn't in the charts. The Cleanup tab "
                       f"measures the usual suspects such as {suspects}.", icon=":material/lock:")
    if IS_WINDOWS:
        st.caption("On Windows, files hard-linked into several folders (mainly C:\\Windows\\WinSxS) are counted "
                   "in each folder, so system folders can look bigger than they are.")

    st.subheader("Where the space goes")
    ctl = st.columns([2, 3, 5])
    kind = ctl[0].segmented_control("Chart", ["Treemap", "Sunburst"], default="Treemap", required=True,
                                    key="ov_kind")
    depth = ctl[1].slider("Levels", 1, 6, 3, key="ov_depth")
    show(charts.folder_map(r, r.root, METRIC, MODE, depth=depth, kind=(kind or "Treemap").lower()), "ov_map")
    st.caption("Colours mark the eight biggest top-level folders; grey is everything else. "
               "Click a box to zoom in, and click the path bar to go back.")

    left, right = st.columns(2)
    with left:
        st.subheader("Top-level folders")
        kids = sorted(r.children.get(r.root, []), key=lambda k: sizes.get(k, 0), reverse=True)
        rows = [{"Folder": display_path(os.path.basename(k)), "Size": sizes[k],
                 "Share": 100 * sizes[k] / (total or 1), "Files": r.tot_files[k]} for k in kids]
        if own.get(r.root):
            rows.append({"Folder": "(files directly here)", "Size": own[r.root],
                         "Share": 100 * own[r.root] / (total or 1), "Files": r.own_files[r.root]})
        rows.sort(key=lambda x: x["Size"], reverse=True)
        st.dataframe(pd.DataFrame(rows), hide_index=True, height=420,
                     column_config={"Size": size_col("Size"), "Share": share_col(),
                                    "Files": st.column_config.NumberColumn("Files", format="localized")})
    with right:
        st.subheader("Folders holding the most files")
        st.caption("Measured by files sitting directly inside each folder, so it points at where the bytes "
                   "physically live rather than at parent folders.")
        heavy = sorted(own.items(), key=lambda kv: kv[1], reverse=True)[:12]
        if heavy:
            show(charts.ranked_bars([rel(d, r.root) for d, _ in heavy], [v for _, v in heavy], MODE,
                                    hover=[f"{r.own_files.get(d, 0):,} files" for d, _ in heavy]), "ov_heavy")

    if r.errors or r.skipped_mounts:
        with st.expander(f"Skipped and unreadable ({r.error_count:,} errors, "
                         f"{len(r.skipped_mounts)} other filesystems)"):
            if r.skipped_mounts:
                st.markdown("**Other filesystems not entered:** " +
                            ", ".join(f"`{display_path(m)}`" for m in r.skipped_mounts[:40]))
            if r.errors:
                st.dataframe(pd.DataFrame([{"Path": display_path(p), "Reason": e} for p, e in r.errors]),
                             hide_index=True)
                if r.error_count > len(r.errors):
                    st.caption(f"Showing the first {len(r.errors)} of {r.error_count:,}.")


def tab_explorer(r: ScanResult) -> None:
    sizes, own = r.sizes(METRIC), r.own(METRIC)
    cwd = SS.get("cwd", r.root)
    if cwd not in sizes:
        cwd = r.root

    def go(path: str) -> None:
        SS.cwd = path
        SS.nav = SS.get("nav", 0) + 1

    nav = st.columns([1, 1, 7], vertical_alignment="center")
    at_top = cwd == r.root
    nav[0].button("Up", icon=":material/arrow_upward:", disabled=at_top, width="stretch",
                  on_click=go, args=(os.path.dirname(cwd),))
    nav[1].button("Top", icon=":material/home:", disabled=at_top, width="stretch", on_click=go, args=(r.root,))
    nav[2].markdown(f"📂 **{display_path(cwd)}** — {human_size(sizes[cwd])} · {r.tot_files[cwd]:,} files · "
                    f"{r.tot_dirs[cwd]:,} subfolders")

    kids = sorted(r.children.get(cwd, []), key=lambda k: sizes.get(k, 0), reverse=True)
    rows = [{"path": k, "Folder": "📁 " + display_path(os.path.basename(k)), "Size": sizes[k],
             "Share": 100 * sizes[k] / (sizes[cwd] or 1), "Files": r.tot_files[k],
             "Subfolders": r.tot_dirs[k]} for k in kids]

    left, right = st.columns([3, 2])
    with left:
        if rows:
            st.caption("Select a row to open that folder.")
            event = st.dataframe(
                pd.DataFrame(rows).drop(columns="path"), key=f"explore_{SS.get('nav', 0)}", on_select="rerun",
                selection_mode="single-row", hide_index=True, height=min(35 * len(rows) + 38, 470),
                column_order=["Folder", "Size", "Share", "Files", "Subfolders"],
                column_config={"Size": size_col("Size"), "Share": share_col(),
                               "Files": st.column_config.NumberColumn("Files", format="localized"),
                               "Subfolders": st.column_config.NumberColumn("Subfolders", format="localized")})
            picked = event.selection.rows
            if picked:
                go(rows[picked[0]]["path"])
                st.rerun()
        else:
            st.info("No subfolders here.")
        st.caption(f"Files directly in this folder: **{human_size(own.get(cwd, 0))}** in "
                   f"{r.own_files.get(cwd, 0):,} files.")
    with right:
        if rows:
            top = rows[:12]
            show(charts.ranked_bars([display_path(os.path.basename(x["path"])) for x in top],
                                    [x["Size"] for x in top], MODE,
                                    hover=[f"{x['Files']:,} files" for x in top]), "ex_bars")

    with st.expander("Map of this folder"):
        show(charts.folder_map(r, cwd, METRIC, MODE, depth=2, height=440), "ex_map")

    st.subheader("Largest files directly in this folder")
    files = list_folder_files(cwd, 40)
    if files:
        st.dataframe(pd.DataFrame([{
            "Name": display_path(os.path.basename(f.path)), "Size": f.size, "On disk": f.alloc,
            "Modified": local_dt(f.mtime), "Type": category_of(file_extension(os.path.basename(f.path))),
        } for f in files]), hide_index=True,
            column_config={"Size": size_col("Size"), "On disk": size_col("On disk"), "Modified": date_col("Modified")})
        st.caption("Read live from disk, so it reflects changes since the scan.")
    else:
        st.caption("No files directly in this folder.")


AGE_CHOICES = {"Any time": 0, "30 days": 30, "90 days": 90, "6 months": 182, "1 year": 365, "2 years": 730}


def large_files_df(r: ScanResult) -> pd.DataFrame:
    cached = SS.get("lf_df")
    if cached and cached[0] == r.id:
        return cached[1]
    recs = []
    for f in r.large_files:
        name = os.path.basename(f.path)
        ext = file_extension(name)
        recs.append({"path": display_path(f.path), "Name": display_path(name), "Folder": display_path(os.path.dirname(f.path)),
                     "Size": f.size, "On disk": f.alloc, "Modified": local_dt(f.mtime),
                     "Accessed": local_dt(f.atime), "mtime": f.mtime, "atime": f.atime,
                     "Type": ext, "Category": category_of(ext)})
    df = pd.DataFrame(recs)
    SS.lf_df = (r.id, df)
    return df


def tab_large(r: ScanResult) -> None:
    if not r.large_files:
        st.info(f"No files of {human_size(r.large_file_min)} or more in this scan. "
                "Lower the threshold under Scan options and scan again.")
        return
    df = large_files_df(r)
    key = "On disk" if METRIC == "alloc" else "Size"
    f = st.columns([3, 3, 2])
    query = f[0].text_input("Search name or folder", placeholder="e.g. .iso, Downloads, model")
    cats = f[1].multiselect("Categories", sorted(df["Category"].unique()), placeholder="All categories")
    age = f[2].selectbox("Not modified for", list(AGE_CHOICES))
    view = df
    if query:
        q = query.lower()
        view = view[view["Name"].str.lower().str.contains(q, regex=False) |
                    view["Folder"].str.lower().str.contains(q, regex=False)]
    if cats:
        view = view[view["Category"].isin(cats)]
    if AGE_CHOICES[age]:
        view = view[view["mtime"] < time.time() - AGE_CHOICES[age] * DAY]
    view = view.sort_values(key, ascending=False)

    st.markdown(f"**{len(view):,}** files · **{human_size(view[key].sum())}** "
                f"({view[key].sum() / (r.total(METRIC) or 1):.1%} of this scan)")
    if not view.empty:
        top = view.head(12)
        show(charts.ranked_bars(top["Name"].tolist(), top[key].tolist(), MODE, hover=top["Folder"].tolist()),
             "lf_bars")
    st.dataframe(view, hide_index=True, height=480,
                 column_order=["Name", "Size", "On disk", "Modified", "Accessed", "Category", "Folder"],
                 column_config={"Size": size_col("Size"), "On disk": size_col("On disk"),
                                "Modified": date_col("Modified"), "Accessed": date_col("Accessed")})
    if (df["On disk"] < df["Size"] * 0.5).any():
        st.caption("When **On disk** is much smaller than **Size**, the file is sparse (common for VM and "
                   "Docker disk images): it only occupies what has actually been written.")
    st.download_button("Download as CSV", view[["path", "Size", "On disk", "Modified", "Category"]]
                       .to_csv(index=False).encode(), file_name="large_files.csv", mime="text/csv",
                       icon=":material/download:")


def tab_types(r: ScanResult) -> None:
    idx = 2 if METRIC == "alloc" else 1
    total = r.total(METRIC) or 1
    by_cat: dict[str, list[int]] = {c: [0, 0, 0] for c in CATEGORIES}
    for ext, s in r.ext_stats.items():
        b = by_cat[category_of(ext)]
        b[0] += s[0]; b[1] += s[1]; b[2] += s[2]  # noqa: E702
    cats = sorted(((c, v) for c, v in by_cat.items() if v[0]), key=lambda cv: cv[1][idx], reverse=True)
    exts = sorted(r.ext_stats.items(), key=lambda kv: kv[1][idx], reverse=True)

    left, right = st.columns(2)
    with left:
        st.subheader("By category")
        show(charts.ranked_bars([c for c, _ in cats], [v[idx] for _, v in cats], MODE,
                                hover=[f"{v[0]:,} files" for _, v in cats]), "ty_cats")
    with right:
        st.subheader("Top extensions")
        top = exts[:12]
        show(charts.ranked_bars([e for e, _ in top], [v[idx] for _, v in top], MODE,
                                hover=[f"{v[0]:,} files · {category_of(e)}" for e, v in top]), "ty_exts")

    st.subheader("All extensions")
    st.dataframe(pd.DataFrame([{
        "Extension": e, "Category": category_of(e), "Files": v[0], "Size": v[idx],
        "Average": v[idx] // max(v[0], 1), "Share": 100 * v[idx] / total} for e, v in exts]),
        hide_index=True, height=360,
        column_config={"Files": st.column_config.NumberColumn("Files", format="localized"),
                       "Size": size_col("Size"), "Average": size_col("Average size"), "Share": share_col()})
    st.caption("`(none)` = no extension (executables, config files), `(numbered)` = rotated logs and split "
               "chunks like `.1`, `(other)` = unusual names.")

    st.subheader("File size distribution")
    hist = r.size_hist
    n_total = sum(b[0] for b in hist) or 1
    small_files = hist[0][0] + hist[1][0]
    small_bytes = hist[0][idx] + hist[1][idx]
    st.caption(f"{small_files / n_total:.0%} of files are under 1 MB, yet they take only "
               f"{small_bytes / total:.1%} of the space.")
    a, b = st.columns(2)
    with a:
        st.markdown("**Number of files**")
        show(charts.ordered_bars(SIZE_BUCKETS, [x[0] for x in hist], MODE, as_bytes=False,
                                 extra=[human_size(x[idx]) for x in hist]), "ty_hist_n")
    with b:
        st.markdown("**Space used**")
        show(charts.ordered_bars(SIZE_BUCKETS, [x[idx] for x in hist], MODE,
                                 extra=[f"{x[0]:,} files" for x in hist]), "ty_hist_b")


def tab_age(r: ScanResult) -> None:
    idx = 2 if METRIC == "alloc" else 1
    total = r.total(METRIC) or 1
    basis = st.segmented_control("Based on", ["Last modified", "Last accessed"], default="Last modified",
                                 required=True, key="age_basis")
    accessed = basis == "Last accessed"
    buckets = r.age_atime if accessed else r.age_mtime
    old = sum(b[idx] for b in buckets[4:])
    c = st.columns(3)
    c[0].metric("Touched in the last month", human_size(sum(b[idx] for b in buckets[:2])), border=True)
    c[1].metric("Untouched for 1+ year", human_size(old), border=True,
                help=f"{old / total:.0%} of this scan")
    c[2].metric("Untouched for 2+ years", human_size(sum(b[idx] for b in buckets[5:])), border=True)
    show(charts.ordered_bars(AGE_BUCKETS, [b[idx] for b in buckets], MODE,
                             extra=[f"{b[0]:,} files" for b in buckets]), "age_bars")
    if accessed:
        st.caption("Windows often doesn't record access times (NTFS can switch it off), so 'last accessed' may "
                   "just repeat the modified or created date." if IS_WINDOWS else
                   "Linux updates access times lazily (relatime: at most once a day), so 'last accessed' is "
                   "approximate. Backup and indexing tools can also refresh it.")

    st.subheader("Large files nobody has touched")
    days = st.select_slider("Untouched for at least", options=[30, 90, 182, 365, 730, 1826], value=365,
                            format_func=lambda d: {30: "1 month", 90: "3 months", 182: "6 months", 365: "1 year",
                                                   730: "2 years", 1826: "5 years"}[d])
    if not r.large_files:
        st.caption("No large files in this scan.")
        return
    df = large_files_df(r)
    col = "atime" if accessed else "mtime"
    stale = df[df[col] < time.time() - days * DAY].sort_values("On disk" if METRIC == "alloc" else "Size",
                                                             ascending=False)
    key = "On disk" if METRIC == "alloc" else "Size"
    st.markdown(f"**{len(stale):,}** files of {human_size(r.large_file_min)} or more · "
                f"**{human_size(stale[key].sum())}**")
    st.dataframe(stale, hide_index=True, height=400,
                 column_order=["Name", "Size", "On disk", "Modified", "Accessed", "Category", "Folder"],
                 column_config={"Size": size_col("Size"), "On disk": size_col("On disk"),
                                "Modified": date_col("Modified"), "Accessed": date_col("Accessed")})


def tab_duplicates(r: ScanResult) -> None:
    cands = {s: p for s, p in r.dup_candidates.items() if len(p) > 1}
    n_files = sum(len(p) for p in cands.values())
    if not cands:
        st.info(f"No two files of {human_size(r.dup_min)} or more share the same size, so there are no duplicates.")
        return
    st.markdown(f"**{n_files:,}** files of {human_size(r.dup_min)} or more share their exact size with another "
                "file. The check compares the first and last 64 KB first, then hashes the full content of files "
                "that still match.")
    if st.button("Find duplicates", type="primary", icon=":material/content_copy:"):
        bar = st.progress(0.0, text="Starting…")
        last = [0.0]

        def prog(done: int, total: int, stage: str) -> None:
            t = time.monotonic()
            if t - last[0] < 0.15 and done < total:
                return
            last[0] = t
            bar.progress(min(done / total, 1.0) if total else 1.0,
                         text=f"{stage} · {human_size(done)} of {human_size(total)}")

        groups = duplicates.find_duplicates(cands, prog)
        bar.empty()
        SS.dups = (r.id, groups)

    found = SS.get("dups")
    if not found or found[0] != r.id:
        return
    groups = found[1]
    if not groups:
        st.success("No duplicate files found.")
        return
    wasted = sum(g.wasted for g in groups)
    c = st.columns(3)
    c[0].metric("Duplicate groups", f"{len(groups):,}", border=True)
    c[1].metric("Extra copies", f"{sum(g.copies - 1 for g in groups):,}", border=True)
    c[2].metric("Space taken by extra copies", human_size(wasted), border=True)
    st.dataframe(pd.DataFrame([{
        "File": display_path(os.path.basename(g.paths[0])), "Copies": g.copies, "Size each": g.size,
        "Extra space": g.wasted, "Locations": [display_path(p) for p in g.paths]} for g in groups]),
        hide_index=True, height=420,
        column_config={"Size each": size_col("Size each"), "Extra space": size_col("Extra space"),
                       "Locations": st.column_config.ListColumn("Locations", width="large")})
    st.markdown("**Biggest groups in full**")
    for g in groups[:15]:
        with st.expander(f"{display_path(os.path.basename(g.paths[0]))} — {g.copies} copies × {human_size(g.size)}"):
            st.code("\n".join(display_path(p) for p in g.paths), language=None)
    st.caption("Hard links are already excluded. Package caches, git objects and app bundles often contain "
               "legitimate duplicates, so delete only copies you recognise.")


RISK_ICON = {"Safe": "🟢 Safe", "Low": "🟡 Low", "Review": "🔍 Review"}


def tab_cleanup(r: ScanResult | None) -> None:
    st.info("Read-only: the app measures and suggests. You review and run any command yourself.",
            icon=":material/shield:")
    if st.button("Check cleanup opportunities", type="primary", icon=":material/cleaning_services:"):
        bar = st.progress(0.0, text="Checking…")

        def prog(i: int, n: int, label: str) -> None:
            bar.progress(i / n, text=f"Checking {label}…")

        SS.cleanup = cleanup.run_checks(r.tot_alloc if r else None, prog)
        bar.empty()
    rep: cleanup.Report | None = SS.get("cleanup")
    if rep is None:
        st.caption("Measures temporary files, the Windows Update cache, Windows.old, the hibernation file, crash "
                   "dumps, the Recycle Bin, Docker, package/editor/browser caches and stale downloads. Takes a "
                   "few seconds." if IS_WINDOWS else
                   "Measures Docker, snaps, logs, old kernels, package/editor/browser caches, old Node versions "
                   "and stale downloads. Takes a few seconds.")
        return

    items = rep.items
    sums = {k: sum(i.size or 0 for i in items if i.risk == k) for k in RISK_ICON}
    c = st.columns(3)
    c[0].metric("🟢 Safe to clear", human_size(sums["Safe"]), border=True, help=cleanup.RISK_HELP["Safe"])
    c[1].metric("🟡 Low risk", human_size(sums["Low"]), border=True, help=cleanup.RISK_HELP["Low"])
    c[2].metric("🔍 Review first", human_size(sums["Review"]), border=True, help=cleanup.RISK_HELP["Review"])
    st.caption(f"Checked {fmt_time(rep.generated_at)} in {rep.duration:.1f}s. " +
               " ".join(f"**{k}:** {v}" for k, v in cleanup.RISK_HELP.items()))

    f = st.columns([4, 2], vertical_alignment="bottom")
    risks = f[0].pills("Risk", list(RISK_ICON), selection_mode="multi", default=list(RISK_ICON),
                       format_func=lambda k: RISK_ICON[k], key="cl_risk")
    hide_empty = f[1].toggle("Hide items with nothing to free", value=True)
    shown = [i for i in items if i.risk in (risks or []) and (i.size or not hide_empty)]

    st.dataframe(pd.DataFrame([{"Item": i.name, "Size": i.size or 0, "Risk": RISK_ICON[i.risk],
                                "Category": i.category, "Location": i.location} for i in shown]),
                 hide_index=True, column_config={"Size": size_col("Size")})

    st.subheader("Details and commands")
    for i in shown:
        with st.expander(f"{RISK_ICON[i.risk]} · {i.name} — {human_size(i.size or 0)}"):
            st.markdown(f"**Where:** `{i.location}`  \n{i.note}")
            if i.details:
                st.code("\n".join(display_path(d) for d in i.details[:40]), language=None)
            if i.command:
                st.code(i.command, language=SHELL_LANGUAGE)
            else:
                st.caption("No command: clear it from inside the app, as described above.")
    if rep.errors:
        with st.expander("Checks that failed"):
            st.write(rep.errors)


def tab_history(r: ScanResult | None) -> None:
    all_snaps = history.list_snapshots(other_machines=True)
    snaps = [s for s in all_snaps if s["this_machine"]]
    elsewhere = len(all_snaps) - len(snaps)
    if r is not None:
        summary = SS.summary[1] if SS.get("summary", (None,))[0] == r.id else history.dir_summary(r)
        earlier = [s for s in snaps if s["root"] == r.root and abs(s["scanned_at"] - r.scanned_at) > 1]
        st.subheader(f"What changed in {display_path(r.root)}")
        if not earlier:
            st.info("No earlier snapshot of this folder yet. Each scan saves one, so scan again later "
                    "(for example after a cleanup) to see what grew or shrank.")
        else:
            options = list(reversed(earlier))
            c = st.columns([3, 3, 2], vertical_alignment="bottom")
            prev = c[0].selectbox("Compare with the scan from", options,
                                  format_func=lambda s: f"{fmt_time(s['scanned_at'])} · "
                                                        f"{human_size(s['total_alloc'])}")
            depth = c[1].segmented_control("Folder level", [1, 2, 3, 4], default=1, required=True,
                                           format_func=lambda d: f"Level {d}", key="hist_depth")
            prev_dirs = history.load_dirs(prev["file"])
            i = 0 if METRIC == "alloc" else 1
            now_total = summary.get(".", [0, 0])[i]
            then_total = prev_dirs.get(".", [0, 0])[i]
            diff = now_total - then_total
            c[2].metric("Total now", human_size(now_total),
                        delta=("+" if diff > 0 else "") + human_size(diff), delta_color="inverse")
            rows = history.compare(summary, prev_dirs, METRIC, depth or 1)
            if rows:
                top = rows[:15]
                show(charts.change_bars([x["folder"] for x in top], [x["change"] for x in top], MODE), "hist_bars")
                st.caption("Red = grew, blue = freed. Folders under 1 MB aren't recorded in snapshots.")
                st.dataframe(pd.DataFrame(rows), hide_index=True, height=360,
                             column_config={"folder": "Folder", "before": size_col("Before"),
                                            "after": size_col("Now"), "change": size_col("Change")})
            else:
                st.success("No folder at this level changed size.")

    points = [{"time": local_dt(s["scanned_at"]), "mountpoint": p["mountpoint"], "used": p["used"]}
              for s in snaps for p in s["partitions"]]
    if len({p["time"] for p in points}) >= 2:
        st.subheader("Disk usage over time")
        show(charts.usage_trend(points, MODE), "hist_trend")

    st.subheader("Saved snapshots")
    if snaps:
        st.dataframe(pd.DataFrame([{"Scanned": local_dt(s["scanned_at"]), "Folder": display_path(s["root"]),
                                    "On disk": s["total_alloc"], "Files": s["file_count"]}
                                   for s in reversed(snaps)]),
                     hide_index=True, column_config={"On disk": size_col("On disk"),
                                                     "Scanned": st.column_config.DatetimeColumn(
                                                         "Scanned", format="YYYY-MM-DD HH:mm"),
                                                     "Files": st.column_config.NumberColumn(format="localized")})
        st.caption(f"Stored in `{display_path(str(history.SNAP_DIR))}` (last {history.MAX_PER_ROOT} per folder). "
                   "Delete the files there to clear history.")
    else:
        st.caption("None yet.")
    if elsewhere:
        hosts = sorted({s["host"] for s in all_snaps if not s["this_machine"] and s["host"]})
        st.caption(f"{elsewhere} snapshot{'s' if elsewhere != 1 else ''} from other computers "
                   f"{'(' + ', '.join(hosts) + ') ' if hosts else ''}are in the same folder but not shown here, "
                   "because they describe different disks.")


tabs = st.tabs(["💽 Disks", "📊 Overview", "📁 Explorer", "📄 Large files", "🧩 File types", "⏳ Age",
                "👯 Duplicates", "🧹 Cleanup", "📈 History"])
with tabs[0]:
    tab_disks()
with tabs[1]:
    if not need_scan():
        tab_overview(res)
with tabs[2]:
    if not need_scan():
        tab_explorer(res)
with tabs[3]:
    if not need_scan():
        tab_large(res)
with tabs[4]:
    if not need_scan():
        tab_types(res)
with tabs[5]:
    if not need_scan():
        tab_age(res)
with tabs[6]:
    if not need_scan():
        tab_duplicates(res)
with tabs[7]:
    tab_cleanup(res)
with tabs[8]:
    tab_history(res)
