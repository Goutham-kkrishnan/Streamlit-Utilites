"""Page furniture shared by every app: setup, the OS switch, the local-only guard and finding lists."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from .findings import LEVEL_HELP, LEVEL_LABEL, LEVELS, Finding, sort_findings
from .system import IS_WINDOWS, OS_NAME, OTHER_OS, SHELL_LANGUAGE
from .utils import display_path, human_size

OS_ICON = {"Linux": "🐧", "Windows": "🪟"}
PLOTLY_CONFIG = {"displaylogo": False, "modeBarButtonsToRemove": ["lasso2d", "select2d", "autoScale2d"]}
LOCAL_ADDRESSES = ("127.0.0.1", "localhost", "::1")


def page_setup(title: str, icon: str, *, about: str = "", history: bool = False) -> bool:
    """Start the page. Returns True when history recording is switched on (always False without history)."""
    st.set_page_config(page_title=title, page_icon=icon, layout="wide")
    # These apps show details of this computer, so they must only be reachable from it. Streamlit applies
    # .streamlit/config.toml only when started from the app folder (run.sh, run.bat and `python app.py` do that).
    if st.get_option("server.address") not in LOCAL_ADDRESSES:
        st.error(f"{title} was started without its local-only settings, so it could be reachable from other "
                 f"computers. Stop it and start it with `{'run.bat' if IS_WINDOWS else './run.sh'}` instead.")
        st.stop()
    record = False
    with st.sidebar:
        st.segmented_control("System", ["Linux", "Windows"], default=OS_NAME, disabled=True,
                             format_func=lambda o: f"{OS_ICON[o]} {o}",
                             help="Detected automatically. What the app checks matches this system.")
        st.caption(f"This computer runs **{OS_NAME}**. To check a {OTHER_OS} computer, copy this folder there and "
                   f"start `{'run.sh' if IS_WINDOWS else 'run.bat'}`.")
        st.caption("🔒 **Read-only.** This app only looks: it never changes, deletes, installs or stops anything. "
                   "Suggested commands are for you to review and run yourself.")
        if history:
            record = st.toggle("Record history", value=True,
                               help="Keeps small snapshots in this app's history/ folder so you can see trends. "
                                    "That folder is the only thing the app writes.")
        st.divider()
    st.title(f"{icon} {title}")
    if about:
        st.caption(about)
    return record


def theme_mode() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:
        return "light"


def show(fig, key: str) -> None:
    st.plotly_chart(fig, theme=None, config=PLOTLY_CONFIG, key=key)


def size_col(label: str, help: str | None = None):
    return st.column_config.NumberColumn(label, format="bytes", help=help)


def date_col(label: str, with_time: bool = False):
    return st.column_config.DatetimeColumn(label, format="YYYY-MM-DD HH:mm" if with_time else "YYYY-MM-DD")


def pct_col(label: str = "Share"):
    return st.column_config.ProgressColumn(label, format="%.0f%%", min_value=0.0, max_value=100.0)


def render_findings(items: list[Finding], key: str, *, empty: str = "Nothing to report.") -> None:
    """Level filter, a table, then each finding with its note, details and suggested command."""
    if not items:
        st.success(empty)
        return
    items = sort_findings(items)
    present = [lv for lv in reversed(LEVELS) if any(f.level == lv for f in items)]
    picked = st.pills("Level", present, selection_mode="multi", default=present,
                      format_func=lambda lv: LEVEL_LABEL[lv], key=f"{key}_levels") or []
    shown = [f for f in items if f.level in picked]
    has_size = any(f.size for f in shown)
    df = pd.DataFrame([{"Finding": f.title, "Level": LEVEL_LABEL[f.level], "Category": f.category,
                        **({"Size": f.size or 0} if has_size else {}), "Where": display_path(f.location)}
                       for f in shown])
    st.dataframe(df, hide_index=True, column_config={"Size": size_col("Size")} if has_size else None)
    st.caption(" · ".join(f"**{LEVEL_LABEL[lv]}**: {LEVEL_HELP[lv]}" for lv in present))
    for i, f in enumerate(shown[:150]):
        size = f" — {human_size(f.size)}" if f.size else ""
        with st.expander(f"{LEVEL_LABEL[f.level]} · {f.title}{size}"):
            if f.location:
                st.markdown(f"**Where:** `{display_path(f.location)}`")
            if f.note:
                st.markdown(f.note)
            if f.details:
                st.code("\n".join(display_path(d) for d in f.details[:60]), language=None)
            if f.command:
                st.caption("Suggested command — review it, then run it yourself if you agree:")
                st.code(f.command, language=SHELL_LANGUAGE)
    if len(shown) > 150:
        st.caption(f"Showing the first 150 of {len(shown):,}. The table above lists all of them.")
