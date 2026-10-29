"""link-pusher: target parsing, what counts as published, the network-mount guard, marker ordering, rsync flags."""

from __future__ import annotations

import importlib.util
import os

import pytest
from conftest import ROOT


@pytest.fixture()
def lp(tmp_path, monkeypatch):
    monkeypatch.setenv("LINK_ROOT", str(tmp_path))
    spec = importlib.util.spec_from_file_location("link_pusher", os.path.join(ROOT, "services", "link-pusher", "link_pusher.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def touch(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def test_parse_targets(lp):
    assert lp.parse_targets("desk:1000 GPUSRV:0") == [("desk", 1000), ("gpusrv", 0)]
    assert lp.parse_targets("hub, gpusrv:5") == [("hub", 0), ("gpusrv", 5)]
    assert lp.parse_targets("") == []


def test_publishable(lp):
    assert lp.publishable("a.bin") and lp.publishable("set1/a.bin") and lp.publishable("set1.done")
    for p in (".incoming/a.bin", ".rsync-partial/a", "set/.hidden", "a.bin.part", ".a"):
        assert not lp.publishable(p)


def test_walk_skips_unpublished(lp, tmp_path):
    for rel in ("a", "d/b", "d/c.part", ".incoming/x", "d/.rsync-partial/y", ".dot"):
        touch(str(tmp_path / rel))
    assert sorted(r for r, _, _ in lp.walk(str(tmp_path))) == ["a", "d/b"]


def test_fs_type_longest_prefix(lp):
    mounts = (
        "/dev/sda2 / ext4 rw 0 0\n"
        "nas:/export/link/gpusrv-hub /link/gpusrv-hub nfs4 rw 0 0\n"
        "/dev/sda1 /link/gpusrv-gpusrv0 xfs rw 0 0\n"
    )
    assert lp.fs_type("/link/gpusrv-hub", mounts) == "nfs4"
    assert lp.fs_type("/link/gpusrv-hub/sub/f", mounts) == "nfs4"
    assert lp.fs_type("/link/hub-desk", mounts) == "ext4"
    assert lp.fs_type("/link/gpusrv-gpusrv0", mounts) == "xfs"


def test_refuses_network_mount(lp, monkeypatch):
    monkeypatch.setattr(lp, "fs_type", lambda path, mounts_text=None: "nfs4")
    ob = lp.Outbox("hub", 0)
    with pytest.raises(RuntimeError, match="refusing"):
        ob.push()


def test_rsync_cmd(lp):
    cmd = lp.rsync_cmd("/link/hub-desk", "desk-link", 1000, ["--exclude=.*"])
    assert cmd[-2:] == ["/link/hub-desk/", "desk-link:"]  # rrsync roots the key at the inbox: target is the root
    assert "--bwlimit=1000" in cmd and "--remove-source-files" in cmd and "--partial-dir=.rsync-partial" in cmd
    assert not any(c.startswith("--bwlimit") for c in lp.rsync_cmd("/link/hub-gpusrv", "gpusrv-link", 0, []))


def test_markers_go_last_and_only_preexisting(lp, tmp_path, monkeypatch):
    ob = lp.Outbox("desk", 1000)
    touch(os.path.join(ob.path, "set1", "a.bin"))
    touch(os.path.join(ob.path, "set1.done"))
    calls = []

    def fake_run(cmd, stdin=None):
        calls.append((cmd, stdin))
        if len(calls) == 1:  # a new marker published while pass 1 runs must wait for the next round
            touch(os.path.join(ob.path, "set2.done"))
        return 1, 10

    for rel in ("set1/a.bin", "set1.done"):
        age(os.path.join(ob.path, rel), 600)
    monkeypatch.setattr(lp, "fs_type", lambda path, mounts_text=None: "ext4")
    monkeypatch.setattr(ob, "_run", fake_run)
    ob.push()
    assert calls[0][1] == "set1/a.bin\n"
    assert "--files-from=-" in calls[1][0] and calls[1][1] == "set1.done\n"
    assert ob.st["last_files"] == 2 and ob.st["last_bytes"] == 20


def age(path, secs):
    t = os.path.getmtime(path) - secs
    os.utime(path, (t, t))


def test_unsettled_files_are_held(lp, monkeypatch):
    """A file changed in the last SETTLE_S, or still open for writing, is never pushed (nor removed); while anything is
    held, markers wait too."""
    ob = lp.Outbox("desk", 1000)
    for rel in ("old.bin", "young.bin", "open.bin", "set.done"):
        touch(os.path.join(ob.path, rel))
    for rel in ("old.bin", "open.bin", "set.done"):
        age(os.path.join(ob.path, rel), 600)
    open_path = os.path.join(os.path.realpath(ob.path), "open.bin")
    monkeypatch.setattr(lp, "open_for_write", lambda top: {open_path})
    monkeypatch.setattr(lp, "fs_type", lambda path, mounts_text=None: "ext4")
    calls = []
    monkeypatch.setattr(ob, "_run", lambda cmd, stdin=None: calls.append(stdin) or (1, 1))
    ob.push()
    assert calls == ["old.bin\n"] and ob.held == 2
    # once settled and closed, the rest go, marker last
    age(os.path.join(ob.path, "young.bin"), 600)
    monkeypatch.setattr(lp, "open_for_write", lambda top: set())
    calls.clear()
    ob.push()
    assert calls == ["old.bin\nopen.bin\nyoung.bin\n", "set.done\n"] and ob.held == 0


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs Linux /proc")
def test_open_for_write_sees_own_writer(lp, tmp_path):
    p = tmp_path / "w.bin"
    with open(p, "w") as f:
        f.write("x")
        f.flush()
        assert os.path.realpath(p) in lp.open_for_write(str(tmp_path))
    assert os.path.realpath(p) not in lp.open_for_write(str(tmp_path))
    with open(p) as f:  # read-only opens don't count
        assert os.path.realpath(p) not in lp.open_for_write(str(tmp_path))


def test_rsync_stats(lp):
    out = "Number of regular files transferred: 1,204\nTotal transferred file size: 52,428,800 bytes\n"
    assert lp.rsync_stats(out) == (1204, 52428800)
    assert lp.rsync_stats("") == (0, 0)


def test_status_file(lp, tmp_path, monkeypatch):
    monkeypatch.setattr(lp, "STATUS_FILE", str(tmp_path / "st" / "status.json"))
    ob = lp.Outbox("desk", 1000)
    touch(os.path.join(ob.path, "a"), b"12345")
    os.makedirs(tmp_path / f"desk-{lp.SELF}")
    lp.write_status([ob])
    import json

    st = json.load(open(lp.STATUS_FILE))
    assert st["outboxes"][ob.name]["files"] == 1 and st["outboxes"][ob.name]["bytes"] == 5
    assert f"desk-{lp.SELF}" in st["inboxes"]
