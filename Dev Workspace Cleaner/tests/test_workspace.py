"""Unit tests for core/workspace.py (artifact rules, the scan, git parsing, findings and commands)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import system, workspace as w  # noqa: E402


def make(path: Path, text: str = "x", size: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text * max(1, size // max(len(text), 1)) if size else text)
    return path


# --------------------------------------------------------------------------- rules

def test_markers_are_required():
    assert w.artifact_kind("node_modules", {"package.json"}) == "node_modules"
    assert w.artifact_kind("node_modules", set()) is None
    assert w.artifact_kind("build", {"README.md"}) is None
    assert w.artifact_kind("build", {"CMakeLists.txt"}) == "build"
    assert w.artifact_kind("dist", {"pyproject.toml"}) == "dist"
    assert w.artifact_kind("target", {"Cargo.toml"}) == "target"
    assert w.artifact_kind("target", {"Makefile"}) is None
    assert w.artifact_kind("bin", {"App.csproj"}) == "bin/obj"
    assert w.artifact_kind("obj", {"Lib.fsproj"}) == "bin/obj"
    assert w.artifact_kind("bin", {"script.sh"}) is None  # a plain ~/bin is not a build folder
    assert w.artifact_kind(".gradle", set()) is None  # ~/.gradle is Gradle's own cache, not a project's
    assert w.artifact_kind(".gradle", {"settings.gradle.kts"}) == ".gradle"
    assert w.artifact_kind(".next", {"package.json"}) == ".next"
    assert w.artifact_kind(".next", set()) is None
    assert w.artifact_kind("Pods", {"Podfile"}) == "Pods"
    assert w.artifact_kind("cmake-build-debug", set()) == "cmake-build-*"
    assert w.artifact_kind("__pycache__", set()) == "__pycache__"
    assert w.artifact_kind(".ruff_cache", set()) == ".ruff_cache"
    assert w.artifact_kind("anything", set(), has_pyvenv_cfg=True) == "virtualenv"
    assert w.artifact_kind("src", {"package.json"}) is None


def test_levels():
    assert w.artifact_level("__pycache__", set()) == "Safe"
    assert w.artifact_level(".next", {"package.json"}) == "Safe"
    assert w.artifact_level("node_modules", {"package.json"}) == "Low"
    assert w.artifact_level("virtualenv", {"requirements.txt"}) == "Low"
    assert w.artifact_level("virtualenv", {"main.py"}) == "Review"


def test_rebuild_hints(monkeypatch):
    monkeypatch.setattr(system, "IS_WINDOWS", False)
    assert w.rebuild_hint("node_modules", {"package.json", "yarn.lock"}) == "yarn install"
    assert w.rebuild_hint("node_modules", {"package.json", "package-lock.json"}) == "npm ci"
    assert w.rebuild_hint("node_modules", {"package.json"}) == "npm install"
    assert w.rebuild_hint("virtualenv", {"requirements.txt"}, "venv") == \
        "python -m venv venv && venv/bin/pip install -r requirements.txt"
    assert w.rebuild_hint("target", {"Cargo.toml"}) == "cargo build"
    assert w.rebuild_hint("bin/obj", {"a.csproj"}) == "dotnet build"
    monkeypatch.setattr(system, "IS_WINDOWS", True)
    assert w.rebuild_hint("virtualenv", {"requirements.txt"}, ".venv") == \
        "python -m venv .venv && .venv\\Scripts\\pip install -r requirements.txt"
    assert w.rebuild_hint("build", {"build.gradle"}) == ".\\gradlew build"


def test_skip_paths_per_os(monkeypatch, tmp_path):
    monkeypatch.setattr(system, "IS_WINDOWS", False)
    linux = w.skip_paths(str(tmp_path), str(tmp_path))
    assert str(tmp_path / ".cache") in linux and str(tmp_path / ".local" / "share") in linux and "/proc" in linux
    monkeypatch.setattr(system, "IS_WINDOWS", True)
    win = w.skip_paths(str(tmp_path), str(tmp_path))
    assert str(tmp_path / "AppData") in win and "/proc" not in win
    # The folder the user picked is always scanned, even when it is on the skip list.
    assert str(tmp_path / "AppData") not in w.skip_paths(str(tmp_path / "AppData"), str(tmp_path))


# --------------------------------------------------------------------------- scan

@pytest.fixture
def tree(tmp_path):
    home = tmp_path / "home"
    web = home / "code" / "web"
    make(web / "package.json", "{}")
    make(web / "package-lock.json", "{}")
    make(web / "node_modules" / "left-pad" / "index.js", "a", 5000)
    make(web / "node_modules" / "left-pad" / "package.json", "{}")
    make(web / "node_modules" / "left-pad" / "node_modules" / "x" / "i.js", "b", 100)  # nested: not separate
    make(web / "dist" / "bundle.js", "c", 3000)
    make(web / "src" / "app.js", "code")
    (web / ".git" / "objects").mkdir(parents=True)
    make(web / ".git" / "HEAD", "ref: refs/heads/main")
    make(web / ".git" / "build" / "x", "y")  # inside .git: never entered

    py = home / "code" / "tool"
    make(py / "pyproject.toml", "[project]")
    make(py / "tool" / "core" / "__pycache__" / "m.cpython-310.pyc", "p", 2000)
    make(py / ".venv" / "pyvenv.cfg", "home = /usr/bin")
    make(py / ".venv" / "lib" / "site.py", "s", 4000)

    loose = home / "scratch"
    make(loose / "env" / "pyvenv.cfg", "home = /usr")  # venv without requirements beside it
    make(loose / "notes.txt", "hi")
    make(loose / "build" / "out.txt", "not a build folder: no marker beside it")

    make(home / ".cache" / "proj" / "package.json", "{}")
    make(home / ".cache" / "proj" / "node_modules" / "a.js", "x")  # cache folder is skipped
    make(home / ".vscode-like" / "ext" / "package.json", "{}")
    make(home / ".vscode-like" / "ext" / "node_modules" / "a.js", "x")  # hidden home folder: skipped
    if hasattr(os, "symlink"):
        os.symlink(web, home / "link-to-web")  # symlinks are never followed
    return home


def test_scan_finds_genuine_artifacts(tree):
    res = w.scan(str(tree), home=str(tree))
    kinds = {(os.path.relpath(a.path, tree), a.kind) for a in res.artifacts}
    assert kinds == {
        ("code/web/node_modules", "node_modules"),
        ("code/web/dist", "dist"),
        ("code/tool/tool/core/__pycache__", "__pycache__"),
        ("code/tool/.venv", "virtualenv"),
        ("scratch/env", "virtualenv"),
    }
    # __pycache__ deep in the package is grouped under the project root (the folder with pyproject.toml).
    assert set(res.projects) == {str(tree / "code" / "web"), str(tree / "code" / "tool"), str(tree / "scratch")}
    tool = res.projects[str(tree / "code" / "tool")]
    assert {a.level for a in tool.artifacts} == {"Safe", "Low"}
    assert res.projects[str(tree / "scratch")].level == "Review"
    web = res.projects[str(tree / "code" / "web")]
    assert web.repo == str(tree / "code" / "web")
    nm = next(a for a in web.artifacts if a.kind == "node_modules")
    assert nm.files == 3 and nm.size > 5000 and nm.hint == "npm ci"
    assert web.last_activity > 0
    assert str(tree / "code" / "web") in res.repo_roots
    assert res.skipped_hidden_home and str(tree / ".cache") not in res.skipped


def test_scan_progress_and_bad_root(tree):
    calls = []
    w.scan(str(tree), home=str(tree), progress=lambda *a: calls.append(a), progress_every=0)
    assert calls and calls[-1][1] == 5
    with pytest.raises(NotADirectoryError):
        w.scan(str(tree / "missing"))


def test_measure_counts_hard_links_once(tmp_path):
    a = make(tmp_path / "store" / "a.bin", "z", 40000)
    os.link(a, tmp_path / "store" / "b.bin")
    seen: set = set()
    size, files = w.measure(str(tmp_path / "store"), seen)
    assert files == 1
    assert w.measure(str(tmp_path / "missing")) == (0, 0)


def test_untouched_filter():
    now = time.time()
    old = w.Project("/p/old", last_activity=now - 400 * 86400)
    new = w.Project("/p/new", last_activity=now - 5 * 86400)
    unknown = w.Project("/p/unknown")
    assert w.untouched([old, new, unknown], 0) == [old, new, unknown]
    assert w.untouched([old, new, unknown], 365, now) == [old, unknown]


# --------------------------------------------------------------------------- git

@pytest.mark.parametrize("line, expect", [
    ("## main...origin/main", {"branch": "main", "upstream": "origin/main", "ahead": 0, "behind": 0}),
    ("## main...origin/main [ahead 2, behind 1]", {"branch": "main", "ahead": 2, "behind": 1}),
    ("## feature/x...origin/feature/x [gone]", {"branch": "feature/x", "gone": True}),
    ("## dev", {"branch": "dev", "upstream": ""}),
    ("## HEAD (no branch)", {"branch": "(detached HEAD)"}),
    ("## No commits yet on main", {"branch": "main", "no_commits": True}),
    ("## Initial commit on master", {"branch": "master", "no_commits": True}),
])
def test_parse_branch_line(line, expect):
    got = w.parse_branch_line(line)
    for k, v in expect.items():
        assert got[k] == v


def test_parse_status_counts_changes():
    out = "## main...origin/main [ahead 1]\n M app.py\nA  new.py\n?? scratch/\n?? notes.txt\n"
    s = w.parse_status(out)
    assert s["changes"] == 4 and s["untracked"] == 2 and s["ahead"] == 1


def _git(repo, *args):
    subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                    *args], cwd=repo, check=True, capture_output=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_state_on_a_real_repo(tmp_path):
    repo = tmp_path / "repo"
    make(repo / "a.txt", "one")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "first")
    make(repo / "a.txt", "two")
    make(repo / "b.txt", "new")
    g = w.git_state(str(repo))
    assert g.ok and g.branch == "main" and not g.upstream
    assert g.changes == 2 and g.untracked == 1
    assert not g.has_remote and g.unpushed == 1 and g.unpushed_text() == "1 (no remote)"
    assert g.last_commit and g.unsaved
    assert "2 uncommitted changes" in g.summary()
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q")
    e = w.git_state(str(empty))
    assert e.ok and e.no_commits and e.unpushed_text() == "no commits yet"
    broken = tmp_path / "broken"
    (broken / ".git").mkdir(parents=True)
    states = w.check_repos([str(broken)])
    assert not states[str(broken)].ok and "repository" in states[str(broken)].error


def test_git_state_summary_wording():
    g = w.GitState("/r", changes=1, unpushed=3, has_remote=True, stashes=2)
    assert g.summary() == "1 uncommitted change, 3 commits not on any remote, 2 stashes"
    assert w.GitState("/r").summary() == "everything committed and pushed"
    assert not w.GitState("/r").unsaved


# --------------------------------------------------------------------------- findings and commands

def _arts(project: str, sep: str = "/"):
    return [w.Artifact(project + sep + "node_modules", "node_modules", "Low", project, "npm install", 5_000_000, 10),
            w.Artifact(project + sep + "dist", "dist", "Low", project, "npm run build", 1_000_000, 3)]


def test_delete_command_linux_quotes_paths(monkeypatch):
    monkeypatch.setattr(system, "IS_WINDOWS", False)
    cmd = w.delete_command("/home/me/it's mine", _arts("/home/me/it's mine"))
    assert cmd.startswith("rm -rf '/home/me/it'\\''s mine/dist'")
    assert "'/home/me/it'\\''s mine/node_modules'" in cmd
    assert "# node_modules: npm install" in cmd and "# dist: npm run build" in cmd


def test_delete_command_windows_uses_remove_item(monkeypatch):
    monkeypatch.setattr(system, "IS_WINDOWS", True)
    proj = r"C:\Users\Me\source\O'Brien app"
    cmd = w.delete_command(proj, _arts(proj, "\\"))
    first = cmd.splitlines()[0]
    assert first == (r"Remove-Item -LiteralPath 'C:\Users\Me\source\O''Brien app\dist', "
                     r"'C:\Users\Me\source\O''Brien app\node_modules' -Recurse -Force")
    venv = w.Artifact(proj + r"\env", "virtualenv", "Review", proj, "python -m venv env", 10, 1)
    cmd = w.delete_command(proj, [venv])
    assert "pip freeze" in cmd.splitlines()[0] and r"\env\Scripts\python.exe" in cmd


def test_many_pycache_folders_use_one_find(monkeypatch):
    monkeypatch.setattr(system, "IS_WINDOWS", False)
    arts = [w.Artifact(f"/p/pkg{i}/__pycache__", "__pycache__", "Safe", "/p", "auto", 100, 1) for i in range(40)]
    cmd = w.delete_command("/p", arts)
    assert cmd.splitlines()[0] == "find '/p' -type d -name __pycache__ -prune -exec rm -rf {} +"
    monkeypatch.setattr(system, "IS_WINDOWS", True)
    assert "-Filter __pycache__ | Remove-Item -Recurse -Force" in w.delete_command(r"C:\p", arts)


def test_project_findings_split_by_level(monkeypatch):
    monkeypatch.setattr(system, "IS_WINDOWS", False)
    p = w.Project("/code/app", repo="/code/app", last_activity=time.time() - 3 * 86400)
    p.artifacts = _arts("/code/app") + [
        w.Artifact("/code/app/__pycache__", "__pycache__", "Safe", "/code/app", "auto", 2000, 2)]
    repos = {"/code/app": w.GitState("/code/app", changes=2, has_remote=True)}
    found = {f.level: f for f in w.project_findings([p], repos)}
    assert set(found) == {"Low", "Safe"}
    assert found["Low"].size == 6_000_000 and "node_modules" in found["Low"].title
    assert "2 uncommitted changes" in found["Low"].note
    assert "__pycache__" in found["Safe"].command and "node_modules" not in found["Safe"].command
