"""Run broker: placement, runner rows, request status from runs, auth (pure functions; no database, no processes)."""

from __future__ import annotations

from test_tools import load

rb = load("services/run-broker/run_broker.py", "run_broker")

HOSTS = {
    "hub": {"cpu_slots": 4, "gpu_slots": 0},
    "small": {"cpu_slots": 2, "gpu_slots": 1, "gpu_vram_gb": 11},
    "big": {"cpu_slots": 2, "gpu_slots": 1, "gpu_vram_gb": 24},
}


def req(**kw) -> dict:
    r = {"id": 7, "title": "Re-embed thumbnails", "command": "true", "kind": "oneshot", "needs": {}, "status": "queued"}
    r.update(kw)
    return r


def load_(**kw) -> dict:
    out = {h: {"cpu": 0, "gpu": 0} for h in HOSTS}
    for h, v in kw.items():
        out[h].update(v)
    return out


def test_cpu_work_goes_to_the_host_with_most_free_slots_and_the_first_host_wins_a_tie():
    assert rb.choose_host(req(), HOSTS, load_(), set())[:2] == ("hub", "cpu")
    assert rb.choose_host(req(), HOSTS, load_(hub={"cpu": 3}), set())[:2] == ("small", "cpu")
    full = load_(hub={"cpu": 4}, small={"cpu": 2}, big={"cpu": 2})
    assert rb.choose_host(req(), HOSTS, full, set()) == (None, "cpu", "waiting for a free CPU slot")


def test_gpu_work_takes_the_smallest_gpu_that_fits():
    assert rb.choose_host(req(needs={"gpu": "required", "gpu_vram_gb": 8}), HOSTS, load_(), set())[:2] == ("small", "gpu")
    assert rb.choose_host(req(needs={"gpu": "required", "gpu_vram_gb": 16}), HOSTS, load_(), set())[:2] == ("big", "gpu")
    busy = load_(small={"gpu": 1})
    assert rb.choose_host(req(needs={"gpu": "required", "gpu_vram_gb": 8}), HOSTS, busy, set())[:2] == ("big", "gpu")
    host, _, why = rb.choose_host(req(needs={"gpu": "required", "gpu_vram_gb": 40}), HOSTS, load_(), set())
    assert host is None and why == "no GPU with 40 GB"


def test_lent_out_gpus_hold_gpu_work_and_preferred_falls_back_to_cpu():
    held = {"small", "big"}
    host, lane, why = rb.choose_host(req(needs={"gpu": "required"}), HOSTS, load_(), held)
    assert (host, lane, why) == (None, "gpu", "the GPUs are lent out; waits for them")
    assert rb.choose_host(req(needs={"gpu": "preferred"}), HOSTS, load_(), held)[:2] == ("hub", "cpu")


def test_a_hand_set_placed_host_wins():
    assert rb.choose_host(req(placed_host="big"), HOSTS, load_(), set())[:2] == ("big", "cpu")
    assert rb.choose_host(req(placed_host="nowhere"), HOSTS, load_(), set())[0] is None


def test_line_order_matches_the_queue_view():
    a = req(id=1, priority="low", requested_at=1)
    b = req(id=2, priority="high", requested_at=5)
    c = req(id=3, priority="normal", requested_at=2, wanted_by=100)
    d = req(id=4, priority="normal", requested_at=1)
    assert [r["id"] for r in sorted([a, b, c, d], key=rb.line_key)] == [2, 3, 4, 1]


def test_order_spec_is_a_runner_row():
    r = req(repo="media-tool", workdir="api", env={"A": "1"}, needs={"gpu": "required", "reads": ["/b", "mysql:x"]})
    s = rb.order_spec(r, "gpu", "~/server-projects")
    assert s["name"] == "req-7-re-embed-thumbnails"
    assert s["workdir"] == "~/server-projects/media-tool/api"
    assert s["env"] == {"A": "1", "TASK_REQUEST_ID": "7"}
    assert s["needs"] == {"paths": ["/b"], "gpu": True}
    assert s["enabled"] and s["instances"] == 1 and s["kind"] == "oneshot"
    assert rb.order_spec(req(), "cpu")["workdir"] is None and rb.order_spec(req(), "cpu")["needs"] == {}
    assert rb.order_spec(req(status="paused"), "cpu")["enabled"] is False
    assert rb.order_spec(req(repo="r"), "cpu", "/srv/code")["workdir"] == "/srv/code/r"
    assert set(s) == set(rb.RUNNER_FIELDS)


def run(start, end=None, rc=None) -> dict:
    return {"started_at": start, "ended_at": end, "exit_code": rc}


def test_request_status_follows_the_runs():
    assert rb.request_update("oneshot", []) is None
    u = rb.request_update("oneshot", [run(1)])
    assert u["status"] == "running" and u["started_at"] == 1 and not u["withdraw"]
    u = rb.request_update("oneshot", [run(1, 2, 0)])
    assert (u["status"], u["exit_code"], u["finished_at"], u["withdraw"]) == ("done", 0, 2, True)


def test_failed_runs_retry_then_fail_the_request():
    u = rb.request_update("oneshot", [run(1, 2, 3)], max_fails=3)
    assert u["status"] == "running" and "retries (1 of 3)" in u["status_note"] and not u["withdraw"]
    u = rb.request_update("oneshot", [run(1, 2, 3), run(3, 4, 3), run(5, 6, 1)], max_fails=3)
    assert (u["status"], u["exit_code"], u["withdraw"]) == ("failed", 1, True)
    # a success resets the count for long-lived kinds
    u = rb.request_update("continuous", [run(1, 2, 3), run(3, 4, 3), run(5, 6, 0), run(7, 8, 1)], max_fails=3)
    assert u["status"] == "running" and not u["withdraw"]
    u = rb.request_update("schedule", [run(1, 2, 0)])
    assert u["status"] == "running" and not u["withdraw"]


def test_auth_one_token_per_server():
    tokens = {"hub": "aaa", "big": "bbb"}
    assert rb.auth("Bearer aaa", "hub", tokens) == 200
    assert rb.auth("Bearer bbb", "hub", tokens) == 403
    assert rb.auth("Bearer zzz", "hub", tokens) == 401
    assert rb.auth(None, "hub", tokens) == 401


def test_slug():
    assert rb.slug("  SigLIP 2: re-embed / ALL thumbnails!! ") == "siglip-2-re-embed-all-th"
    assert rb.slug("") == "job"


def test_reservation_is_active_only_until_its_end():
    until = "2026-11-01T14:45:00-04:00"
    end = 1793558700.0  # that instant
    assert rb.reservation(end - 60, until, "glyphlang", "exclusive") == {
        "until": until, "for": "glyphlang", "mode": "exclusive", "priority_only": True, "fill_idle_s": rb.FILL_IDLE_S,
    }  # fmt: skip
    assert rb.reservation(end - 60, until, "glyphlang", "fill")["priority_only"] is False
    assert rb.reservation(end, until, "glyphlang") is None
    assert rb.reservation(end - 60, "", "x") is None
    assert rb.reservation(end - 60, "not a date", "x") is None


def test_a_fill_reservation_lets_other_work_onto_a_gpu_idle_for_the_fill_time():
    """Owner 2026-11-18: a reservation holds the priority slot; other work fills a GPU idle for 30 s (docs/gpu-scheduling.md)."""
    qs = ["priority", "standard", "backup"]
    fill = {"priority_only": False, "fill_idle_s": 30.0}
    excl = {"priority_only": True, "fill_idle_s": 30.0}
    assert rb.claim_queues(qs, None, 0) == qs  # no reservation: every queue, as before
    assert rb.claim_queues(qs, fill, 5) == ["priority"]  # priority work a moment ago
    assert rb.claim_queues(qs, fill, 30) == qs
    assert rb.claim_queues(qs, excl, 9999) == ["priority"]
    assert rb.yielding(None) == ("backup",)
    assert rb.yielding(fill) == ("backup", "standard")  # fill work gives way to the holder's priority work
    assert rb.yielding(excl) == ("backup",)


def test_work_with_little_time_left_finishes_instead_of_giving_way():
    """Owner 2026-11-18: under 300 s of estimated run time left, finish; a changeover may take minutes."""
    assert rb.finishing({}) is True  # an Ollama call: no estimate, always finishes
    assert rb.finishing({"remaining_s": rb.YIELD_FINISH_S}) is True
    assert rb.finishing({"remaining_s": rb.YIELD_FINISH_S + 1}) is False
