"""Tests for the scanner, duplicate finder and history comparison. Run: python3 -m pytest -q"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analyser import cleanup, disks, duplicates, history, scanner, system  # noqa: E402
from analyser.scanner import scan  # noqa: E402
from analyser.utils import file_extension, human_size  # noqa: E402


def write(path, size, fill=b"a"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(fill * size)


def make_tree(tmp_path):
    root = tmp_path / "root"
    write(str(root / "a.txt"), 1000)
    write(str(root / "sub" / "big.bin"), 300_000, b"x")
    write(str(root / "sub" / "deep" / "copy1.dat"), 200_000, b"z")
    write(str(root / "other" / "copy2.dat"), 200_000, b"z")
    write(str(root / "other" / "notcopy.dat"), 200_000, b"y")
    os.link(root / "sub" / "big.bin", root / "other" / "hardlink.bin")
    os.symlink(root / "sub", root / "link_to_sub")
    return str(root)


def test_scan_totals_and_tree(tmp_path):
    root = make_tree(tmp_path)
    r = scan(root, large_file_min=250_000, dup_min=100_000)

    # hard link counted once, symlink not followed
    assert r.file_count == 5
    assert r.hardlink_skipped == 1
    assert r.symlink_count == 1
    assert r.tot_size[root] >= 1000 + 300_000 + 3 * 200_000

    sub = os.path.join(root, "sub")
    assert r.tot_files[sub] == 2
    assert r.tot_size[sub] >= 500_000
    assert set(r.children[root]) == {sub, os.path.join(root, "other")}
    # parent total equals own + children
    for d, kids in r.children.items():
        assert r.tot_alloc[d] == r.own_alloc[d] + sum(r.tot_alloc[k] for k in kids)

    assert [os.path.basename(f.path) for f in r.large_files] == ["big.bin"]
    assert r.ext_stats[".dat"][0] == 3
    assert sorted(len(v) for v in r.dup_candidates.values()) == [1, 3]


def test_duplicates(tmp_path):
    root = make_tree(tmp_path)
    r = scan(root, dup_min=100_000)
    groups = duplicates.find_duplicates(r.dup_candidates)
    assert len(groups) == 1
    assert sorted(os.path.basename(p) for p in groups[0].paths) == ["copy1.dat", "copy2.dat"]
    assert groups[0].wasted == 200_000


def test_history_compare(tmp_path):
    root = make_tree(tmp_path)
    before = history.dir_summary(scan(root), min_bytes=0)
    write(os.path.join(root, "sub", "new.bin"), 400_000)
    after = history.dir_summary(scan(root), min_bytes=0)
    rows = history.compare(after, before, "size", depth=1)
    assert rows[0]["folder"] == "sub" and rows[0]["change"] == 400_000


def test_snapshots_are_per_machine(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "SNAP_DIR", tmp_path / "snaps")
    saved = history.save_snapshot(scan(make_tree(tmp_path)), [])
    data = json.loads(saved.read_text())
    assert data["machine"] == history.MACHINE
    # The app folder was copied from another computer that had saved a snapshot of its own.
    data.update(machine="another-computer", host="other-pc")
    (tmp_path / "snaps" / "20200101-000000_other.json").write_text(json.dumps(data))
    # A snapshot saved before machine tagging counts as this computer's.
    del data["machine"]
    (tmp_path / "snaps" / "20200101-000001_legacy.json").write_text(json.dumps(data))
    assert len(history.list_snapshots()) == 2
    everything = history.list_snapshots(other_machines=True)
    assert len(everything) == 3
    assert [s["host"] for s in everything if not s["this_machine"]] == ["other-pc"]


def test_helpers():
    assert human_size(5_700_000_000) == "5.7 GB"
    assert human_size(512) == "512 B"
    assert file_extension("backup.tar.gz") == ".tar.gz"
    assert file_extension("libfoo.so.1.2") == ".so"
    assert file_extension("syslog.1") == "(numbered)"
    assert file_extension(".bashrc") == "(none)"


def test_windows_disk_list_parsing():
    sample = json.dumps([
        {"type": "disk", "disk": 0, "size": 512110190592, "model": "NVMe SSD 512GB", "bus": "17", "media": "4"},
        {"type": "part", "disk": 0, "part": 1, "letter": "", "size": 314572800, "fs": "FAT32", "label": "",
         "vsize": 314568704, "free": 200000000},
        {"type": "part", "disk": 0, "part": 2, "letter": "", "size": 16777216, "fs": "", "label": "",
         "vsize": None, "free": None},
        {"type": "part", "disk": 0, "part": 3, "letter": "C", "size": 274877906944, "fs": "NTFS",
         "label": "Windows", "vsize": 274877902848, "free": 74877902848},
    ])
    rows = disks.parse_windows_disks(sample)
    assert [r["type"] for r in rows] == ["disk", "part", "part", "part"]
    assert rows[0]["kind"] == "SSD (NVMe)" and rows[0]["device"] == "Disk 0"
    c = rows[3]
    assert c["mountpoint"] == "C:\\" and c["device"] == "Disk 0, partition 3 (C:)"
    assert c["used"] == 200_000_000_000 and c["model"] == "NVMe SSD 512GB"
    assert rows[2]["used"] is None and rows[2]["fstype"] == ""
    # A single row comes back from ConvertTo-Json as an object, not a list.
    assert len(disks.parse_windows_disks(json.dumps({"type": "disk", "disk": 1, "size": 1}))) == 1
    assert disks.parse_windows_disks("") == []


def test_windows_paths_for_display_and_commands(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Ann\AppData\Local")
    monkeypatch.setenv("APPDATA", r"C:\Users\Ann\AppData\Roaming")
    monkeypatch.setenv("USERPROFILE", r"C:\Users\Ann")
    assert cleanup.win_short(r"c:\users\ann\appdata\local\pip\Cache") == r"%LOCALAPPDATA%\pip\Cache"
    assert cleanup.win_short(r"C:\Users\Ann\.nuget") == r"%USERPROFILE%\.nuget"
    assert cleanup.win_short(r"C:\Users\Annette\x") == r"C:\Users\Annette\x"  # prefix must end at a folder
    assert cleanup.ps_quote(r"C:\Users\Ann\AppData\Roaming\Code\Cache") == r'"$env:APPDATA\Code\Cache"'
    assert cleanup.ps_quote(r"D:\$Recycle.Bin") == r'"D:\`$Recycle.Bin"'


def test_scan_in_windows_mode(tmp_path, monkeypatch):
    # Simulate Windows: no st_blocks (estimate from 4 KiB clusters) and no st_dev from directory entries.
    monkeypatch.setattr(system, "IS_WINDOWS", True)
    monkeypatch.setattr(scanner, "IS_WINDOWS", True)
    r = scan(make_tree(tmp_path))

    def clusters(n):
        return -(-n // 4096) * 4096

    files = [1000, 300_000, 200_000, 200_000, 200_000]  # the hard link is counted once
    folders = [os.stat(d).st_size for d, _, _ in os.walk(r.root)]
    assert r.file_count == 5 and r.total("alloc") == sum(map(clusters, files + folders))
    assert not r.skipped_mounts


def test_localised_linux_user_folders(tmp_path, monkeypatch):
    desk = tmp_path / "Schreibtisch"
    desk.mkdir()
    (tmp_path / "user-dirs.dirs").write_text(f'# comment\nXDG_DESKTOP_DIR="{desk}"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert system.user_folder("desktop") == str(desk)
    assert system.user_folder("downloads") == os.path.join(system.HOME, "Downloads")  # not listed: default
