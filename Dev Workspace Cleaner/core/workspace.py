"""Find rebuildable build and dependency folders in projects, plus the git state of each repository.

READ-ONLY: this module only lists folders, measures sizes and runs read-only git commands through
core.readonly. Suggested delete commands are built as text for the user to review and run.

An "artifact" is a folder that a tool recreates (node_modules, a virtualenv, __pycache__, target, ...).
It is only flagged when its name matches AND a marker file proves it belongs to a real project
(e.g. node_modules needs a package.json beside it), so a random folder called "build" is left alone.
"""
from __future__ import annotations

import os
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import readonly, system
from .findings import Finding
from .utils import DAY, display_path, fmt_time, human_size

# --------------------------------------------------------------------------- artifact rules

NODE = {"package.json"}
BUILD_MARKERS = {"package.json", "setup.py", "pyproject.toml", "build.gradle", "build.gradle.kts", "CMakeLists.txt",
                 "pubspec.yaml"}
GRADLE_MARKERS = {"build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "gradlew"}
DOTNET_SUFFIXES = (".csproj", ".fsproj", ".vbproj")
VENV_RECIPES = {"requirements.txt", "pyproject.toml", "Pipfile", "environment.yml", "environment.yaml",
                "setup.py", "setup.cfg", "poetry.lock", "uv.lock", "requirements-dev.txt"}
TOOL_CACHES = {".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox"}
JS_CACHES = {".next", ".nuxt", ".svelte-kit", ".parcel-cache", ".angular", ".turbo", ".expo"}

# kind -> (group shown in charts, level)
KINDS: dict[str, tuple[str, str]] = {
    "node_modules": ("Dependencies", "Low"),
    "virtualenv": ("Virtualenvs", "Low"),  # Review when nothing beside it says how to recreate it
    "__pycache__": ("Caches", "Safe"),
    **{k: ("Caches", "Safe") for k in TOOL_CACHES},
    **{k: ("Caches", "Safe") for k in JS_CACHES},
    ".dart_tool": ("Caches", "Safe"),
    "target": ("Build output", "Low"),
    "build": ("Build output", "Low"),
    "dist": ("Build output", "Low"),
    ".gradle": ("Build output", "Low"),
    "bin/obj": ("Build output", "Low"),
    "cmake-build-*": ("Build output", "Low"),
    "Pods": ("Dependencies", "Low"),
}
LEVEL_RANK = {"Safe": 0, "Low": 1, "Review": 2}

# A folder holding one of these is a project root (artifacts deeper down are grouped under it).
PROJECT_MARKERS = {".git", "package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile",
                   "environment.yml", "Cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
                   "settings.gradle.kts", "CMakeLists.txt", "pubspec.yaml", "go.mod", "Podfile", "composer.json",
                   "Gemfile", "deno.json", "tox.ini", "noxfile.py"}
PROJECT_SUFFIXES = (".sln", ".csproj", ".fsproj", ".vbproj")

# Folders never entered, by name anywhere: git internals and installed (not project) packages.
SKIP_NAMES = {".git", "site-packages", "dist-packages", "$Recycle.Bin", "System Volume Information"}
# Folders in the home folder that hold caches, app data or installed tools rather than projects.
HOME_SKIPS_LINUX = [".cache", ".local/share", "snap", ".nvm", ".pyenv", ".rustup", ".cargo", ".npm", ".m2", ".gradle",
                    ".vscode", ".vscode-server", ".cursor", ".config", ".var", ".mozilla", ".thunderbird", ".conda",
                    "anaconda3", "miniconda3", ".dotnet", ".nuget", "Android/Sdk", ".android"]
HOME_SKIPS_WINDOWS = ["AppData", ".nvm", ".pyenv", ".rustup", ".cargo", ".npm", ".m2", ".gradle", ".vscode", ".cursor",
                      ".conda", "anaconda3", "miniconda3", ".nuget", ".dotnet", "scoop"]
SYSTEM_SKIPS_LINUX = ["/proc", "/sys", "/dev", "/run", "/snap", "/var/lib/docker", "/tmp"]


def _is_dotnet_project(files: set[str]) -> bool:
    return any(n.endswith(DOTNET_SUFFIXES) for n in files)


def artifact_kind(name: str, files: set[str], has_pyvenv_cfg: bool = False) -> Optional[str]:
    """The artifact kind of a subfolder called `name`, given the file names beside it; None if not an artifact.

    has_pyvenv_cfg: the subfolder itself contains pyvenv.cfg (any folder doing so is a virtualenv).
    """
    if has_pyvenv_cfg:
        return "virtualenv"
    if name == "node_modules":
        return name if files & NODE else None
    if name == "__pycache__" or name in TOOL_CACHES or name == ".dart_tool":
        return name
    if name in JS_CACHES:
        return name if files & NODE else None
    if name == "target":
        return name if files & {"Cargo.toml", "pom.xml"} else None
    if name in ("build", "dist"):
        return name if files & BUILD_MARKERS else None
    if name == ".gradle":
        return name if files & GRADLE_MARKERS else None
    if name in ("bin", "obj"):
        return "bin/obj" if _is_dotnet_project(files) else None
    if name.startswith("cmake-build-"):
        return "cmake-build-*"
    if name == "Pods":
        return name if "Podfile" in files else None
    return None


def artifact_level(kind: str, files: set[str]) -> str:
    """Safe / Low / Review. A virtualenv is Review when nothing beside it records its packages."""
    if kind == "virtualenv":
        return "Low" if files & VENV_RECIPES else "Review"
    return KINDS[kind][1]


def rebuild_hint(kind: str, files: set[str], venv_name: str = ".venv") -> str:
    """How to get an artifact back, e.g. 'npm ci'."""
    win = system.IS_WINDOWS
    if kind == "node_modules":
        if "pnpm-lock.yaml" in files:
            return "pnpm install"
        if "yarn.lock" in files:
            return "yarn install"
        if "bun.lockb" in files or "bun.lock" in files:
            return "bun install"
        return "npm ci" if "package-lock.json" in files else "npm install"
    if kind == "virtualenv":
        pip = f"{venv_name}\\Scripts\\pip" if win else f"{venv_name}/bin/pip"
        make = f"python -m venv {venv_name}"
        if "uv.lock" in files:
            return "uv sync"
        if "poetry.lock" in files:
            return "poetry install"
        if "Pipfile" in files:
            return "pipenv install"
        if "requirements.txt" in files:
            return f"{make} && {pip} install -r requirements.txt"
        if files & {"pyproject.toml", "setup.py", "setup.cfg"}:
            return f"{make} && {pip} install -e ."
        if files & {"environment.yml", "environment.yaml"}:
            return "conda env create -f environment.yml"
        return f"{make}, then reinstall the packages you need (save `pip freeze` output first)"
    if kind == "__pycache__":
        return "nothing to do: Python recreates it when the code runs"
    if kind in TOOL_CACHES:
        return "nothing to do: the tool recreates it on its next run"
    if kind in JS_CACHES:
        return "nothing to do: rebuilt on the next dev server start or build"
    if kind == ".dart_tool":
        return "flutter pub get (or dart pub get); builds recreate it automatically"
    if kind == "target":
        return "cargo build" if "Cargo.toml" in files else "mvn package"
    if kind in ("build", "dist"):
        if "package.json" in files:
            return "npm run build"
        if files & {"build.gradle", "build.gradle.kts"}:
            return ".\\gradlew build" if win else "./gradlew build"
        if "CMakeLists.txt" in files:
            return "cmake -B build && cmake --build build"
        if "pubspec.yaml" in files:
            return "flutter build"
        return "python -m build"
    if kind == ".gradle":
        return (".\\gradlew build" if win else "./gradlew build") + " (recreated automatically)"
    if kind == "bin/obj":
        return "dotnet build"
    if kind == "cmake-build-*":
        return "reload the CMake project in your IDE, then build"
    if kind == "Pods":
        return "pod install"
    return ""


def is_project_root(files: set[str], names: set[str]) -> bool:
    return bool(names & PROJECT_MARKERS) or any(n.endswith(PROJECT_SUFFIXES) for n in files)


def skip_paths(root: str, home: Optional[str] = None) -> set[str]:
    """Absolute folders the walk never enters (caches, trash, app data and tool installs)."""
    home = home or system.HOME
    rel = HOME_SKIPS_WINDOWS if system.IS_WINDOWS else HOME_SKIPS_LINUX
    out = {os.path.normpath(os.path.join(home, *r.split("/"))) for r in rel}
    if not system.IS_WINDOWS:
        out |= set(SYSTEM_SKIPS_LINUX)
    out.discard(os.path.normpath(root))  # whatever the user picked is always scanned
    return out


# --------------------------------------------------------------------------- scanning

@dataclass
class Artifact:
    path: str
    kind: str
    level: str
    project: str
    hint: str
    size: int = 0  # bytes on disk
    files: int = 0

    @property
    def group(self) -> str:
        return KINDS[self.kind][0]


@dataclass
class GitState:
    repo: str
    ok: bool = True
    error: str = ""
    branch: str = ""
    upstream: str = ""
    upstream_gone: bool = False
    ahead: int = 0
    behind: int = 0
    changes: int = 0  # modified, staged, deleted and untracked entries
    untracked: int = 0
    has_remote: bool = False
    unpushed: int = 0  # commits on local branches that no remote branch contains
    stashes: int = 0
    last_commit: Optional[float] = None
    no_commits: bool = False

    @property
    def unsaved(self) -> bool:
        return self.ok and bool(self.changes or self.stashes or self.unpushed)

    def unpushed_text(self) -> str:
        if not self.ok:
            return "?"
        if self.no_commits:
            return "no commits yet"
        if not self.has_remote:
            return f"{self.unpushed} (no remote)"
        return str(self.unpushed)

    def summary(self) -> str:
        """One line in plain words, e.g. '3 uncommitted changes, 2 unpushed commits'."""
        if not self.ok:
            return f"git couldn't read it ({self.error})"
        parts = []
        if self.changes:
            parts.append(f"{self.changes} uncommitted change{'s' if self.changes != 1 else ''}")
        if self.unpushed:
            parts.append(f"{self.unpushed} commit{'s' if self.unpushed != 1 else ''} not on any remote"
                         if self.has_remote else f"{self.unpushed} commits and no remote to push to")
        if self.stashes:
            parts.append(f"{self.stashes} stash{'es' if self.stashes != 1 else ''}")
        return ", ".join(parts) or "everything committed and pushed"


@dataclass
class Project:
    path: str
    repo: Optional[str] = None
    artifacts: list[Artifact] = field(default_factory=list)
    last_activity: float = 0.0  # newest mtime of the project's own files (top level + one level down)

    @property
    def name(self) -> str:
        return os.path.basename(self.path.rstrip("\\/")) or self.path

    @property
    def size(self) -> int:
        return sum(a.size for a in self.artifacts)

    @property
    def level(self) -> str:
        return max((a.level for a in self.artifacts), key=LEVEL_RANK.__getitem__, default="Safe")

    @property
    def kinds(self) -> list[str]:
        return sorted({a.kind for a in self.artifacts})


@dataclass
class ScanResult:
    root: str
    scanned_at: float
    duration: float = 0.0
    dirs_seen: int = 0
    errors: int = 0
    projects: dict[str, Project] = field(default_factory=dict)
    repos: dict[str, GitState] = field(default_factory=dict)
    repo_roots: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    skipped_hidden_home: bool = False  # hidden folders directly in the home folder were left out
    git_missing: bool = False

    @property
    def artifacts(self) -> list[Artifact]:
        return [a for p in self.projects.values() for a in p.artifacts]

    @property
    def id(self) -> str:
        return f"{self.root}@{self.scanned_at:.3f}"


# progress(folders_seen, artifacts_found, bytes_found, current_folder)
ProgressFn = Callable[[int, int, int, str], None]


def measure(path: str, seen: Optional[set] = None) -> tuple[int, int]:
    """(bytes on disk, file count) under `path`, never following symlinks or junctions.

    `seen` collects (device, inode) of hard-linked files so they are counted once (pnpm links its store).
    """
    try:
        st0 = os.lstat(path)
    except OSError:
        return 0, 0
    total, files = system.alloc_bytes(st0), 0
    windows = system.IS_WINDOWS
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        total += system.alloc_bytes(st)
                        if not system.is_redirect(st):
                            stack.append(e.path)
                        continue
                    if seen is not None and not windows and st.st_nlink > 1:
                        key = (st.st_dev, st.st_ino)
                        if key in seen:
                            continue
                        seen.add(key)
                    total += system.alloc_bytes(st)
                    files += 1
        except OSError:
            continue
    return total, files


def find_repo_upward(folder: str) -> Optional[str]:
    """The nearest folder at or above `folder` that holds a .git entry (folder or worktree file)."""
    cur = os.path.abspath(folder)
    while True:
        if os.path.lexists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def scan(root: str, *, progress: Optional[ProgressFn] = None, progress_every: float = 0.25,
         home: Optional[str] = None) -> ScanResult:
    """Walk `root` (no symlinks/junctions), find artifacts and group them by project. Git is checked separately."""
    root = os.path.realpath(os.path.expanduser(root))
    if not os.path.isdir(root):
        raise NotADirectoryError(root)
    home = os.path.normpath(home or system.HOME)
    res = ScanResult(root=root, scanned_at=time.time())
    t0 = last = time.monotonic()
    skip = skip_paths(root, home)
    inside = root.rstrip(os.sep) + os.sep
    res.skipped = sorted(p for p in skip if os.path.isdir(p) and p.startswith(inside)
                         and not (os.path.dirname(p) == home and os.path.basename(p).startswith(".")))
    res.skipped_hidden_home = (home + os.sep).startswith(inside) or home == root
    not_projects = {home, os.path.dirname(home), os.path.abspath(os.sep)}
    seen_inodes: set = set()
    windows = system.IS_WINDOWS
    found_bytes = 0
    n_artifacts = 0
    start_repo = find_repo_upward(root)
    repo_roots: set[str] = set()
    if start_repo:
        repo_roots.add(start_repo)

    stack: list[tuple[str, Optional[str], Optional[str]]] = [(root, None, start_repo)]
    while stack:
        d, proj, repo = stack.pop()
        res.dirs_seen += 1
        files: set[str] = set()
        subdirs: list[os.DirEntry] = []
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            if windows and system.is_redirect(e.stat(follow_symlinks=False)):
                                continue  # junction or mount point: skip like a symlink
                            subdirs.append(e)
                        else:
                            files.add(e.name)
                    except OSError:
                        continue
        except OSError:
            res.errors += 1
            continue
        names = files | {e.name for e in subdirs}
        if "conda-meta" in names and d != root:
            continue  # an installed conda environment, not a project
        here_proj = d if d not in not_projects and is_project_root(files, names) else proj
        here_repo = d if ".git" in names else repo
        if ".git" in names:
            repo_roots.add(d)
        for e in subdirs:
            name, path = e.name, e.path
            if name in SKIP_NAMES or path in skip or (d == home and name.startswith(".")):
                continue  # hidden folders in the home folder hold app settings and tool data, not projects
            kind = artifact_kind(name, files, os.path.isfile(os.path.join(path, "pyvenv.cfg")))
            if kind is None:
                stack.append((path, here_proj, here_repo))
                continue
            owner = here_proj or d
            p = res.projects.get(owner)
            if p is None:
                p = res.projects[owner] = Project(owner, here_repo)
            size, nfiles = measure(path, seen_inodes)
            p.artifacts.append(Artifact(path, kind, artifact_level(kind, files), owner,
                                        rebuild_hint(kind, files, name), size, nfiles))
            found_bytes += size
            n_artifacts += 1
        if progress is not None:
            t = time.monotonic()
            if t - last >= progress_every:
                last = t
                progress(res.dirs_seen, n_artifacts, found_bytes, d)

    for p in res.projects.values():
        p.last_activity = last_activity(p.path, {os.path.basename(a.path) for a in p.artifacts
                                                 if os.path.dirname(a.path) == p.path})
    res.repo_roots = sorted(repo_roots)
    res.duration = time.monotonic() - t0
    if progress is not None:
        progress(res.dirs_seen, n_artifacts, found_bytes, root)
    return res


def last_activity(project: str, artifact_names: set[str], limit: int = 3000) -> float:
    """Newest modification time of the project's own files: its top level plus one level down.

    Artifact folders, .git and other hidden folders are left out, so a fresh `npm install` doesn't count.
    """
    newest = 0.0
    seen = 0
    level = [project]
    for depth in range(2):
        nxt = []
        for d in level:
            try:
                with os.scandir(d) as it:
                    for e in it:
                        seen += 1
                        if seen > limit:
                            return newest
                        try:
                            if e.is_symlink():
                                continue
                            if e.is_dir(follow_symlinks=False):
                                if depth == 0 and e.name not in artifact_names and not e.name.startswith(".") \
                                        and e.name not in SKIP_NAMES and artifact_kind(e.name, set()) is None:
                                    nxt.append(e.path)
                                continue
                            newest = max(newest, e.stat(follow_symlinks=False).st_mtime)
                        except OSError:
                            continue
            except OSError:
                continue
        level = nxt
    return newest


# --------------------------------------------------------------------------- git (read-only)

GIT = ("git", "--no-optional-locks")


def parse_branch_line(line: str) -> dict:
    """Parse the '## ...' header of `git status --porcelain=v1 --branch`."""
    out = {"branch": "", "upstream": "", "ahead": 0, "behind": 0, "gone": False, "no_commits": False}
    text = line[3:].strip() if line.startswith("## ") else line.strip()
    for prefix in ("No commits yet on ", "Initial commit on "):
        if text.startswith(prefix):
            out["branch"], out["no_commits"] = text[len(prefix):].strip(), True
            return out
    if text.startswith("HEAD (no branch)"):
        out["branch"] = "(detached HEAD)"
        return out
    info = ""
    if " [" in text and text.endswith("]"):
        text, _, info = text.partition(" [")
        info = info[:-1]
    branch, sep, upstream = text.partition("...")
    out["branch"], out["upstream"] = branch, upstream if sep else ""
    for part in info.split(","):
        part = part.strip()
        if part == "gone":
            out["gone"] = True
        elif part.startswith("ahead "):
            out["ahead"] = int(part[6:] or 0)
        elif part.startswith("behind "):
            out["behind"] = int(part[7:] or 0)
    return out


def parse_status(text: str) -> dict:
    """Branch info plus change counts from `git status --porcelain=v1 --branch` output."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    head = parse_branch_line(lines[0]) if lines and lines[0].startswith("## ") else parse_branch_line("")
    entries = [ln for ln in lines if not ln.startswith("## ")]
    head["changes"] = len(entries)
    head["untracked"] = sum(1 for ln in entries if ln.startswith("??"))
    return head


def git_state(repo: str, timeout: float = 20) -> GitState:
    """Read-only git summary of one repository (every command uses --no-optional-locks and cwd=repo)."""
    g = GitState(repo)
    out = readonly.run([*GIT, "status", "--porcelain=v1", "--branch"], timeout=timeout, cwd=repo)
    if out is None:
        err = readonly.run([*GIT, "status", "--porcelain=v1", "--branch"], timeout=timeout, cwd=repo, allow_fail=True)
        g.ok, g.error = False, ("git doesn't recognise it as a repository (incomplete or damaged .git folder?)"
                                if err is not None else "git failed or timed out")
        return g
    s = parse_status(out)
    g.branch, g.upstream, g.upstream_gone = s["branch"], s["upstream"], s["gone"]
    g.ahead, g.behind, g.changes, g.untracked, g.no_commits = (s["ahead"], s["behind"], s["changes"], s["untracked"],
                                                               s["no_commits"])
    stash = readonly.run([*GIT, "stash", "list"], timeout=timeout, cwd=repo) or ""
    g.stashes = sum(1 for ln in stash.splitlines() if ln.strip())
    remotes = readonly.run([*GIT, "for-each-ref", "--format=%(refname)", "refs/remotes"], timeout=timeout, cwd=repo)
    g.has_remote = bool(remotes and remotes.strip())
    if not g.no_commits:
        count = readonly.run([*GIT, "rev-list", "--count", "--branches", "--not", "--remotes"], timeout=timeout,
                             cwd=repo)
        try:
            g.unpushed = int((count or "0").strip() or 0)
        except ValueError:
            g.unpushed = 0
        when = readonly.run([*GIT, "log", "-1", "--format=%ct"], timeout=timeout, cwd=repo, allow_fail=True)
        try:
            g.last_commit = float(when.strip()) if when and when.strip() else None
        except ValueError:
            g.last_commit = None
    return g


def git_available() -> bool:
    return readonly.run([*GIT, "status", "--porcelain=v1", "--branch"], cwd=system.HOME, allow_fail=True) is not None


def check_repos(repos: list[str], progress: Optional[Callable[[int, int, str], None]] = None,
                cache: Optional[dict[str, GitState]] = None, workers: int = 6) -> dict[str, GitState]:
    """git_state for every repo (a few at a time). `cache` holds results from earlier calls."""
    cache = cache if cache is not None else {}
    todo = [r for r in repos if r not in cache]
    if todo:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, (repo, state) in enumerate(zip(todo, pool.map(git_state, todo)), 1):
                cache[repo] = state
                if progress is not None:
                    progress(i, len(todo), repo)
    return {r: cache[r] for r in repos}


# --------------------------------------------------------------------------- findings

def sh_quote(path: str) -> str:
    return "'" + path.replace("'", "'\\''") + "'"


def ps_quote(path: str) -> str:
    return "'" + path.replace("'", "''") + "'"


MAX_LISTED = 25


def delete_command(project: str, artifacts: list[Artifact]) -> str:
    """Command (for the user to run) that deletes exactly these artifact folders, plus how to rebuild them."""
    win = system.IS_WINDOWS
    paths = sorted(a.path for a in artifacts)
    caches = [a.path for a in artifacts if a.kind == "__pycache__"]
    lines = []
    if len(caches) > MAX_LISTED:  # many __pycache__ folders: one find command instead of a huge list
        paths = [p for p in paths if p not in set(caches)]
        if win:
            lines.append(f"Get-ChildItem -LiteralPath {ps_quote(project)} -Directory -Recurse -Force "
                         f"-Filter __pycache__ | Remove-Item -Recurse -Force")
        else:
            lines.append(f"find {sh_quote(project)} -type d -name __pycache__ -prune -exec rm -rf {{}} +")
    if paths:
        if win:
            lines.append("Remove-Item -LiteralPath " + ", ".join(ps_quote(p) for p in paths) + " -Recurse -Force")
        else:
            lines.append("rm -rf " + " \\\n       ".join(sh_quote(p) for p in paths))
    hints = []
    for a in sorted(artifacts, key=lambda a: a.kind):
        h = f"# {a.kind}: {a.hint}"
        if h not in hints:
            hints.append(h)
    review = [a for a in artifacts if a.level == "Review"]
    sep = "\\" if win else "/"
    quote = ps_quote if win else sh_quote
    for a in review:
        py = a.path.rstrip("\\/") + (r"\Scripts\python.exe" if win else "/bin/python")
        lines.insert(0, f"# First save its package list:  {'& ' if win else ''}{quote(py)} -m pip freeze > "
                        f"{quote(project.rstrip(sep) + sep + 'requirements.txt')}")
    return "\n".join(lines + ["# To get them back:"] + hints)


LEVEL_TITLE = {"Safe": "caches", "Low": "dependencies and build output", "Review": "virtualenv to review"}
LEVEL_NOTE = {
    "Safe": "Caches that the tools recreate on their own. Deleting them is harmless; the next run is a bit slower.",
    "Low": "Downloaded dependencies and build output. They come back with a reinstall or rebuild (see the commands), "
           "which costs some time and possibly internet.",
    "Review": "A Python virtual environment with no requirements.txt, pyproject.toml, Pipfile or environment.yml "
              "beside it, so the exact packages inside may be hard to recreate. Save its package list first.",
}


def project_findings(projects: list[Project], repos: dict[str, GitState], now: Optional[float] = None) -> list[Finding]:
    """One finding per project and level, each with a command that deletes just those folders."""
    now = now or time.time()
    out = []
    for p in projects:
        by_level: dict[str, list[Artifact]] = {}
        for a in p.artifacts:
            by_level.setdefault(a.level, []).append(a)
        g = repos.get(p.repo) if p.repo else None
        for level, arts in by_level.items():
            kinds = sorted({a.kind for a in arts})
            size = sum(a.size for a in arts)
            note = [LEVEL_NOTE[level]]
            if p.last_activity:
                note.append(f"Project files last changed **{fmt_time(p.last_activity)}** "
                            f"({int((now - p.last_activity) // DAY)} days ago).")
            if g is not None:
                where = "this project" if p.repo == p.path else f"`{display_path(p.repo)}`"
                if g.unsaved:
                    note.append(f"Git ({where}): **{g.summary()}**. Deleting build folders doesn't touch that work, "
                                "but keep it in mind before removing the whole project.")
                elif g.ok:
                    note.append(f"Git ({where}): {g.summary()}.")
            details = [f"{human_size(a.size):>9}  {a.files:>8,} file{'s' if a.files != 1 else ' '}  {a.path}" for a in
                       sorted(arts, key=lambda a: a.size, reverse=True)]
            out.append(Finding(
                title=f"{p.name}: {LEVEL_TITLE[level]} ({', '.join(kinds)})", level=level,
                category=" / ".join(sorted({a.group for a in arts})), location=p.path, size=size,
                note="\n\n".join(note), command=delete_command(p.path, arts), details=details))
    return out


AGE_FILTER = {"Any time": 0, "1 month": 30, "3 months": 91, "6 months": 182, "12 months": 365}


def untouched(projects: list[Project], days: int, now: Optional[float] = None) -> list[Project]:
    """Projects whose own files haven't changed for at least `days` (unknown activity counts as untouched)."""
    if not days:
        return list(projects)
    cutoff = (now or time.time()) - days * DAY
    return [p for p in projects if not p.last_activity or p.last_activity < cutoff]
