"""Dev Workspace Cleaner: Finds rebuildable build folders (node_modules, virtualenvs, caches, build outputs) across your projects and shows which projects still hold uncommitted or unpushed git work. Read-only.

Run with ./run.sh (Linux), run.bat (Windows) or `python app.py`, from any folder.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

if __name__ == "__main__" and "streamlit" not in sys.modules:
    # Started as `python app.py`: hand over to the launcher, which installs what's missing and starts the server.
    import runpy

    sys.argv[0] = os.path.join(os.path.dirname(os.path.realpath(__file__)), "launch.py")
    runpy.run_path(sys.argv[0], run_name="__main__")

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from core import system, theme, workspace  # noqa: E402
from core.findings import LEVEL_LABEL  # noqa: E402
from core.ui import date_col, page_setup, render_findings, show, size_col, theme_mode  # noqa: E402
from core.utils import display_path, fmt_time, human_duration, human_size  # noqa: E402

RECORD = page_setup("Dev Workspace Cleaner", "🧹", about="Finds rebuildable build folders (node_modules, virtualenvs, caches, build outputs) across your projects and shows which projects still hold uncommitted or unpushed git work.", history=False)

SS = st.session_state
MODE = theme_mode()


def local_dt(ts: float | None):
    return datetime.fromtimestamp(ts) if ts else None


# ============================================================================ sidebar

PLACES = {"Home folder": system.HOME, "Desktop": system.user_folder("desktop"), "Custom path…": None}

with st.sidebar:
    st.header("Scan")
    choice = st.selectbox("Scan root", list(PLACES), help="Every folder below this one is checked.")
    target = PLACES[choice]
    if target is None:
        target = st.text_input("Folder path", value=SS.get("dwc_custom", system.HOME),
                               placeholder=r"C:\Users\you\source" if system.IS_WINDOWS else "/home/you/projects")
        SS.dwc_custom = target
    else:
        st.caption(f"`{display_path(target)}`")
    age_label = st.selectbox(
        "Untouched for at least", list(workspace.AGE_FILTER),
        help="Only show projects whose own files haven't changed for this long. Build folders of projects you "
             "are still working on would just come back on the next build.")
    st.caption("The scan skips caches, the Trash, app data, installed tools (" +
               ("AppData, .nvm, .cargo, …" if system.IS_WINDOWS else "~/.cache, ~/.local/share, ~/snap, …") +
               ") and git internals, and never follows symlinks" + (" or junctions." if system.IS_WINDOWS else "."))


# ============================================================================ scanning

def run_scan(path: str) -> None:
    path = path.strip().strip('"')
    if system.IS_WINDOWS and len(path) == 2 and path[1] == ":":
        path += "\\"  # "D:" alone means the current folder on D:, not the drive
    path = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(path):
        st.error(f"Not a folder: `{display_path(path)}`")
        return
    with st.status(f"Scanning {display_path(path)} …", expanded=True) as status:
        line = st.empty()

        def progress(dirs: int, arts: int, nbytes: int, current: str) -> None:
            line.markdown(f"**{dirs:,}** folders checked · **{arts:,}** build folders · **{human_size(nbytes)}**  \n"
                          f"`…{display_path(current)[-90:]}`")

        try:
            res = workspace.scan(path, progress=progress)
        except OSError as exc:
            status.update(label=f"Scan failed: {exc}", state="error")
            return
        if res.repo_roots:
            if workspace.git_available():
                status.update(label=f"Checking {len(res.repo_roots)} git repositories …")
                bar = st.progress(0.0)

                def git_progress(i: int, n: int, repo: str) -> None:
                    bar.progress(i / n, text=f"git: {display_path(os.path.basename(repo))} ({i} of {n})")

                res.repos = workspace.check_repos(res.repo_roots, git_progress)
                bar.empty()
            else:
                res.git_missing = True
        status.update(label="Scan complete", state="complete", expanded=False)
    SS.dwc_result = res
    took = human_duration(res.duration) if res.duration >= 1 else f"{res.duration:.1f} s"
    SS.dwc_flash = (f"Found {len(res.artifacts):,} build folders in {len(res.projects):,} projects "
                    f"({human_size(sum(a.size for a in res.artifacts))}) in {took}")
    st.rerun()


res: workspace.ScanResult | None = SS.get("dwc_result")
if res is None:
    st.markdown("Looks for folders such as `node_modules`, virtualenvs, `__pycache__` and build outputs that your "
                "tools can recreate, so you can free space. Pick where to look in the sidebar.")
if st.button("Scan for build folders" if res is None else "Scan again", type="primary", icon=":material/search:"):
    run_scan(target)
if "dwc_flash" in SS:
    st.toast(SS.pop("dwc_flash"), icon="✅")
if res is None:
    st.stop()


# ============================================================================ results

def unique_labels(items: list[workspace.Project]) -> list[str]:
    """Short chart labels; projects with the same folder name get their parent folder added."""
    names = [p.name for p in items]
    out: list[str] = []
    for p, n in zip(items, names):
        label = n if names.count(n) == 1 else os.path.join(os.path.basename(os.path.dirname(p.path)), n)
        label = display_path(label)
        while label in out:
            label += " ·"
        out.append(label)
    return out


days = workspace.AGE_FILTER[age_label]
all_projects = sorted(res.projects.values(), key=lambda p: p.size, reverse=True)
projects = workspace.untouched(all_projects, days)
arts = [a for p in projects for a in p.artifacts]
repos = res.repos
unsaved = sorted((g for g in repos.values() if g.unsaved), key=lambda g: g.repo)

st.caption(f"Scanned **{display_path(res.root)}** on {fmt_time(res.scanned_at)} · {res.dirs_seen:,} folders checked"
           + (f" · {res.errors:,} couldn't be read" if res.errors else ""))
c = st.columns(4)
c[0].metric("Reclaimable", human_size(sum(a.size for a in arts)), border=True,
            help="Disk space the build folders below take. Files hard-linked from a shared store (pnpm) count once.")
c[1].metric("Projects", f"{len(projects):,}", border=True, help="Folders that hold build folders.")
c[2].metric("Build folders", f"{len(arts):,}", border=True)
c[3].metric("Repos with unsaved work", f"{len(unsaved):,}", border=True,
            help="Git repositories with uncommitted changes, commits not on any remote, or stashes. "
                 "See the Git tab.")
by_level = {lv: sum(a.size for a in arts if a.level == lv) for lv in ("Safe", "Low", "Review")}
st.caption(" · ".join(f"{LEVEL_LABEL[lv]} **{human_size(v)}**" for lv, v in by_level.items()) +
           (f" · showing projects untouched for at least {age_label} ({len(projects)} of {len(all_projects)})"
            if days else ""))

tabs = st.tabs(["📊 Overview", "📁 Projects", "🧹 Findings", "🌿 Git: unsaved work"])


def tab_overview() -> None:
    if not arts:
        st.info("No rebuildable build folders found here" + (" for that age filter." if days else "."))
    else:
        left, right = st.columns(2)
        with left:
            st.subheader("By type")
            kinds: dict[str, list[int]] = {}
            for a in arts:
                k = kinds.setdefault(a.kind, [0, 0])
                k[0] += a.size
                k[1] += 1
            top = sorted(kinds.items(), key=lambda kv: kv[1][0], reverse=True)[:12]
            show(theme.ranked_bars([k for k, _ in top], [v[0] for _, v in top], MODE,
                                   hover=[f"{v[1]:,} folders · {workspace.KINDS[k][0]}" for k, v in top]), "ov_types")
        with right:
            st.subheader("Biggest projects")
            top_p = projects[:12]
            show(theme.ranked_bars(unique_labels(top_p), [p.size for p in top_p], MODE,
                                   hover=[display_path(p.path) for p in top_p]), "ov_projects")
        st.caption("**Safe** = caches that rebuild themselves; **Low** = dependencies and build output that need a "
                   "reinstall or rebuild; **Review** = virtualenvs with nothing beside them that records their "
                   "packages.")
    with st.expander("What the scan skipped"):
        lines = [f"- `{display_path(p)}`" for p in res.skipped]
        if res.skipped_hidden_home:
            lines.insert(0, "- Hidden folders directly in your home folder (app settings and tool data)")
        st.markdown("\n".join(lines) or "Nothing from the skip list is under this folder.")
        st.caption("Also never entered: `.git` folders, installed packages (`site-packages`), conda environments, "
                   "symlinks" + (", junctions" if system.IS_WINDOWS else "") + " and the inside of each build "
                   "folder (it is only measured).")


def tab_projects() -> None:
    if not projects:
        st.info("No projects with build folders" + (" for that age filter." if days else "."))
        return
    f = st.columns([3, 3, 2], vertical_alignment="bottom")
    query = f[0].text_input("Search project or folder", placeholder="e.g. api, Desktop")
    kinds = f[1].multiselect("Build folder types", sorted({a.kind for a in arts}), placeholder="All types")
    only_unsaved = f[2].toggle("Only repos with unsaved work", value=False)
    rows = []
    for p in projects:
        g = repos.get(p.repo) if p.repo else None
        ok = bool(g and g.ok)
        rows.append({
            "Project": display_path(p.name), "Size": p.size, "Level": LEVEL_LABEL[p.level],
            "Build folders": p.kinds, "Count": len(p.artifacts), "Last activity": local_dt(p.last_activity),
            "Branch": g.branch if ok else "", "Uncommitted": g.changes if ok else None,
            "Unpushed": g.unpushed_text() if g else "", "Stashes": g.stashes if ok else None,
            "Last commit": local_dt(g.last_commit) if g else None, "Folder": display_path(p.path),
            "_unsaved": bool(g and g.unsaved),
        })
    df = pd.DataFrame(rows)
    if query:
        q = query.lower()
        df = df[df["Project"].str.lower().str.contains(q, regex=False) |
                df["Folder"].str.lower().str.contains(q, regex=False)]
    if kinds:
        df = df[df["Build folders"].apply(lambda ks: any(k in ks for k in kinds))]
    if only_unsaved:
        df = df[df["_unsaved"]]
    df = df.drop(columns="_unsaved")
    st.markdown(f"**{len(df):,}** projects · **{human_size(df['Size'].sum())}**")
    st.dataframe(df, hide_index=True, height=480,
                 column_config={"Size": size_col("Size"), "Last activity": date_col("Last activity"),
                                "Last commit": date_col("Last commit"),
                                "Build folders": st.column_config.ListColumn("Build folders"),
                                "Count": st.column_config.NumberColumn("Folders", help="Build folders found"),
                                "Uncommitted": st.column_config.NumberColumn("Uncommitted", format="%d"),
                                "Stashes": st.column_config.NumberColumn("Stashes", format="%d")})
    st.caption("**Last activity** is the newest change to the project's own files (its top level and one level "
               "down, not counting build folders). Git columns are empty for projects outside a repository.")
    csv = df.assign(**{"Build folders": df["Build folders"].apply(", ".join)})
    st.download_button("Download as CSV", csv.to_csv(index=False).encode(), file_name="dev_projects.csv",
                       mime="text/csv", icon=":material/download:")


def tab_findings() -> None:
    st.info("Read-only: the app only measures. Each finding has a command for you to review and run yourself; it "
            "deletes just that project's build folders and says how to get them back.", icon=":material/shield:")
    render_findings(workspace.project_findings(projects, repos), "dwc_findings",
                    empty="No rebuildable build folders found" + (" for that age filter." if days else "."))


def tab_git() -> None:
    st.caption("This is separate from build folders. It lists git repositories under the scan root holding work "
               "that exists only on this computer: **uncommitted changes**, **unpushed commits** (on local "
               "branches but on no remote branch) or **stashes** (changes put aside with `git stash`). Deleting "
               "build folders never touches this work, but check here before deleting or moving a whole project. "
               "The age filter doesn't apply here.")
    if res.git_missing:
        st.warning("git isn't installed (or not on the PATH), so repositories couldn't be checked.")
        return
    if not repos:
        st.info("No git repositories found under this folder.")
        return
    if unsaved:
        st.dataframe(pd.DataFrame([{
            "Repository": display_path(os.path.basename(g.repo)), "Branch": g.branch,
            "Uncommitted": g.changes, "Untracked": g.untracked, "Unpushed": g.unpushed_text(),
            "Stashes": g.stashes, "Upstream": (g.upstream + (" (gone)" if g.upstream_gone else "")) or "none",
            "Last commit": local_dt(g.last_commit), "Folder": display_path(g.repo)} for g in unsaved]),
            hide_index=True, column_config={
                "Last commit": date_col("Last commit"),
                "Untracked": st.column_config.NumberColumn(
                    "Untracked", help="New files git doesn't track yet (included in Uncommitted)")})
        st.caption("**Upstream** is the remote branch the current branch pushes to; *none* means it was never "
                   "pushed. *(no remote)* means the repository has no remote branches at all, so every commit "
                   "lives only here.")
    else:
        st.success(f"All {len(repos)} repositories are committed and pushed, with no stashes.")
    failed = [g for g in repos.values() if not g.ok]
    with st.expander(f"All repositories checked ({len(repos)})"):
        st.dataframe(pd.DataFrame([{"Repository": display_path(os.path.basename(g.repo)),
                                    "State": g.summary(), "Branch": g.branch, "Folder": display_path(g.repo)}
                                   for g in sorted(repos.values(), key=lambda g: g.repo)]), hide_index=True)
        if failed:
            st.caption(f"{len(failed)} couldn't be read by git (usually an incomplete copy of a .git folder).")
        st.caption("Checked read-only with `git --no-optional-locks status / stash list / for-each-ref / rev-list / "
                   "log`, run inside each repository.")


with tabs[0]:
    tab_overview()
with tabs[1]:
    tab_projects()
with tabs[2]:
    tab_findings()
with tabs[3]:
    tab_git()
