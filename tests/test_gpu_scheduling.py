"""GPU scheduling (docs/gpu-scheduling.md): job definitions, claim files, finish-or-give-way, and gpuq (pure; no GPU, no broker)."""

from __future__ import annotations

import json
import os
import time

from test_tools import load

gw = load("services/gpu-worker/gpu_worker.py", "gpu_worker")


def write(path: str, obj: object) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(obj if isinstance(obj, str) else json.dumps(obj))


def test_job_definitions_need_a_command_list(tmp_path):
    write(tmp_path / "image-signals.json", {"cmd": ["/opt/run.sh"], "args": True})
    write(tmp_path / "no-cmd.json", {"workdir": "/x"})
    write(tmp_path / "shell-string.json", {"cmd": "rm -rf /"})
    write(tmp_path / "broken.json", "{not json")
    write(tmp_path / "notes.txt", "ignored")
    assert list(gw.job_defs(str(tmp_path))) == ["image-signals"]
    assert gw.job_defs("") == {} and gw.job_defs(str(tmp_path / "missing")) == {}


def test_job_args_are_appended_only_when_the_definition_allows():
    d = {"cmd": ["/opt/run.sh", "--fixed"], "args": True}
    assert gw.job_argv(d, {"payload": {"args": ["--batch", 2019]}}) == ["/opt/run.sh", "--fixed", "--batch", "2019"]
    assert gw.job_argv({"cmd": ["/opt/run.sh"]}, {"payload": {}}) == ["/opt/run.sh"]
    for bad in ({"cmd": ["/opt/run.sh"]}, d):
        try:
            gw.job_argv(bad, {"payload": {"args": "--batch 1; rm -rf /" if bad is d else ["x"]}})
        except ValueError:
            continue
        raise AssertionError("bad args accepted")


def test_claims_live_while_their_pid_runs_and_until_their_end(tmp_path):
    now = time.time()
    write(tmp_path / "a.json", {"for": "training", "pid": 111, "at": now - 10})
    write(tmp_path / "b.json", {"for": "server", "pid": 222, "at": now - 20})
    write(tmp_path / "c.json", {"for": "maint", "until": now + 60, "at": now - 5})
    write(tmp_path / "d.json", {"for": "old", "until": "2020-01-01T00:00:00-05:00"})
    write(tmp_path / ".state", {"held_by": None})
    live = gw.live_claims(str(tmp_path), now, alive=lambda pid: pid == 111)
    assert [c["id"] for c in live] == ["a", "c"]  # b's process is gone, d has ended; oldest first
    assert gw.live_claims(str(tmp_path / "none"), now) == []


def test_long_fill_work_gives_way_and_short_work_finishes(tmp_path):
    now = time.time()
    ollama = {"yields": True, "finish_s": 300}  # an Ollama call: no estimate
    assert gw.gives_way(ollama, now) is False
    job = {"yields": True, "finish_s": 300, "est_s": 3600, "started": now - 60}
    assert gw.gives_way(job, now) is True
    assert gw.gives_way(dict(job, started=now - 3400), now) is False  # 200 s left: finish
    assert gw.gives_way(dict(job, yields=False), now) is False  # priority work never gives way
    st = tmp_path / "status.json"
    write(st, {"remaining_s": 120, "note": "batch 39 of 40"})
    assert gw.remaining(dict(job, status_file=str(st)), now) == 120  # the job's own estimate wins
    assert gw.gives_way(dict(job, status_file=str(st)), now) is False


def test_gpuq_hold_writes_a_claim_and_status_reads_it(tmp_path, monkeypatch, capsys):
    gq = load("scripts/gpuq.py", "gpuq")
    monkeypatch.setattr(gq, "CLAIMS_DIR", str(tmp_path))
    assert gq.main(["hold", "--gpu", "g0", "--for", "server", "--pid", str(os.getpid()), "--id", "c1", "--no-wait"]) == 0
    claim = json.load(open(tmp_path / "g0" / "c1.json", encoding="utf-8"))
    assert claim["for"] == "server" and claim["pid"] == os.getpid()
    assert [c["id"] for c in gw.live_claims(str(tmp_path / "g0"), time.time())] == ["c1"]
    assert gq.main(["release", "--gpu", "g0", "--id", "c1"]) == 0
    assert gw.live_claims(str(tmp_path / "g0"), time.time()) == []
