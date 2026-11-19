"""Work queues: which item a GPU gets next (pure; no database) and the GPU worker's request/answer handling."""

from __future__ import annotations

import os
import sys

from conftest import ROOT
from test_tools import load

sys.path.insert(0, os.path.join(ROOT, "services", "run-broker"))
wq = load("services/run-broker/workq.py", "workq")
gw = load("services/gpu-worker/gpu_worker.py", "gpu_worker")

BIG, SMALL = "big:27b", "small:14b"
NOW = 100_000.0


def c(i, model=BIG, priority=0, study="a", created=NOW - 10, study_last=0, ctx=8192) -> dict:
    return {
        "id": i,
        "model": model,
        "priority": priority,
        "study": study,
        "created": created,
        "study_last": study_last,
        "ctx": ctx,
    }


def pick(cands, loaded=(BIG, 8192), since=NOW - 5, load_s=None, models=(BIG, SMALL)):
    r = wq.pick(cands, loaded, since, load_s or {BIG: 5.0, SMALL: 3.0}, NOW, list(models))
    return r and r["id"]


def test_the_highest_level_wins_even_when_it_means_loading():
    assert pick([c(1, BIG, 0), c(2, SMALL, 5)]) == 2


def test_the_loaded_model_stays_while_it_has_work_at_the_level():
    assert pick([c(1, SMALL, created=NOW - 30), c(2, BIG, created=NOW - 1)]) == 2


def test_a_context_change_counts_as_a_reload():
    assert pick([c(1, BIG, ctx=32768, created=NOW - 30), c(2, BIG, ctx=8192, created=NOW - 1)]) == 2


def test_a_same_level_switch_waits_until_the_load_is_amortized():
    # a context change of the loaded model: switch once it has run 10x the load time and the other has waited as long
    cands = [c(1, BIG, ctx=32768, created=NOW - 60), c(2, BIG)]
    assert pick(cands, since=NOW - 10) == 2
    assert pick(cands, since=NOW - 51) == 1  # same model, another context: BIG loads in 5 s here
    assert pick([c(1, BIG, ctx=32768, created=NOW - 5), c(2, BIG)], since=NOW - 51) == 2  # not waited long enough yet
    # never loaded here: the default cost
    assert pick(cands, since=NOW - 51, load_s={SMALL: 3.0}) == 2
    assert pick([c(1, BIG, ctx=32768, created=NOW - 400), c(2, BIG)], since=NOW - 301, load_s={SMALL: 3.0}) == 1


def test_nothing_loaded_or_nothing_for_it_falls_back_by_preference():
    assert pick([c(1, SMALL, created=NOW - 50), c(2, BIG)], loaded=None) == 2  # own model first
    assert pick([c(1, SMALL)], loaded=(BIG, 8192)) == 1  # fallback when own has nothing


def test_a_fallback_never_takes_the_gpu_from_its_own_model():
    # own model loaded with work left: the fallback waits however long, even past the amortized dwell
    assert pick([c(1, SMALL, created=NOW - 900), c(2, BIG)], since=NOW - 900) == 2
    # the fallback loaded and own-model work waiting: back to the own model once the load is amortized
    cands = [c(1, BIG, created=NOW - 60), c(2, SMALL)]
    assert pick(cands, loaded=(SMALL, 8192), since=NOW - 10) == 2
    assert pick(cands, loaded=(SMALL, 8192), since=NOW - 51) == 1  # BIG loads in 5 s here: 10x = 50 s


def test_studies_take_turns_within_a_level():
    cands = [c(1, study="big-study", study_last=NOW - 1, created=NOW - 60), c(2, study="small-study", study_last=NOW - 30)]
    assert pick(cands) == 2
    assert pick([c(1, study="x", study_last=0, created=NOW - 5), c(2, study="y", study_last=0, created=NOW - 9)]) == 2


def test_nothing_ready():
    assert pick([]) is None


def test_request_body_fills_model_stream_and_keep_alive():
    item = {"model": BIG, "payload": {"api": "chat", "body": {"messages": [], "think": True, "stream": False}}}
    path, body = gw.request_body(item, "30m")
    assert path == "/api/chat" and body["model"] == BIG and body["stream"] is True and body["keep_alive"] == "30m"
    item["payload"]["body"]["keep_alive"] = 0
    assert gw.request_body(item)[1]["keep_alive"] == 0  # the submitter's own keep_alive wins
    assert gw.request_body({"model": BIG, "payload": {"api": "generate", "body": {"prompt": "x"}}})[0] == "/api/generate"


def test_assemble_matches_ollamas_non_streamed_answer():
    chunks = [
        {"message": {"role": "assistant", "content": "", "thinking": "hm"}, "done": False},
        {"message": {"role": "assistant", "content": "Hel"}, "done": False},
        {"message": {"role": "assistant", "content": "lo"}, "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True, "eval_count": 3, "load_duration": 4_500_000_000},
    ]
    r = gw.assemble("chat", chunks)
    assert r["message"] == {"role": "assistant", "content": "Hello", "thinking": "hm"} and r["eval_count"] == 3
    assert gw.stats(r, 8192)["load_s"] == 4.5
    g = gw.assemble("generate", [{"response": "a"}, {"response": "b", "done": True, "context": [1, 2]}])
    assert g["response"] == "ab" and "context" not in g and "logprobs" not in g
    # with "logprobs" asked for, every content chunk carries its tokens' logprobs and the done chunk none: all are kept
    lp = gw.assemble(
        "generate",
        [
            {"response": "Ye", "logprobs": [{"token": "Ye", "logprob": -0.1}]},
            {"response": "s", "logprobs": [{"token": "s", "logprob": -0.2}]},
            {"response": "", "done": True},
        ],
    )
    assert [x["token"] for x in lp["logprobs"]] == ["Ye", "s"]


def test_dashboard_tab_order_outranks_item_priority():
    order = {"recovery": 10, "copilot": 30, "media": 40, "sensitivity": 80}
    cands = [
        dict(c(1, priority=99), dashboard="sensitivity"),
        dict(c(2, priority=0), dashboard="copilot"),
        dict(c(3, priority=5), dashboard="copilot"),
        dict(c(4, priority=50), dashboard=None),
    ]
    assert pick(wq.rank(cands, order)) == 3  # copilot is the earliest tab here; its priority 5 beats its priority 0
    assert pick(wq.rank([dict(c(1), dashboard="nowhere"), dict(c(2), dashboard="sensitivity")], order)) == 2
    assert pick(wq.rank([dict(c(1, priority=3)), dict(c(2, priority=1))], {})) == 1  # no tab list: item priority as before


def test_gpu_only_worker_asks_for_every_layer_and_spots_a_spill():
    item = {"model": BIG, "payload": {"api": "chat", "body": {"messages": [], "options": {"num_ctx": 6144}}}}
    assert gw.request_body(item, "30m", gpu_only=True)[1]["options"] == {"num_ctx": 6144, "num_gpu": 999}
    assert "num_gpu" not in gw.request_body(item, "30m", gpu_only=False)[1]["options"]
    assert gw.spill([{"name": BIG, "size": 4_000_000_000, "size_vram": 4_000_000_000}], BIG) is None
    assert gw.spill([{"name": BIG, "size": 7_200_000_000, "size_vram": 6_300_000_000}], BIG) == (6.3, 7.2)
    assert gw.spill([], BIG) is None


def test_local_queue_takes_levels_changed_after_the_claim():
    q = [{"queue": "priority", "id": i, "priority": p} for i, p in ((1, 28), (2, 26), (3, 20))]
    q.sort(key=gw.queue_order)
    assert gw.relevel(q, {"priority/3": 29, "priority/2": 26}) == 1
    q.sort(key=gw.queue_order)
    assert [it["id"] for it in q] == [3, 1, 2]
    assert gw.relevel(q, {}) == 0
    assert gw.queue_order({"queue": "backup", "priority": 99}) > gw.queue_order({"queue": "priority", "priority": 0})


def test_busy_gpu_still_takes_higher_level_work_for_another_model():
    busy = [{"queue": "priority", "model": SMALL, "ctx": 8192, "priority": 15, "dashboard": None, "id": 1}]
    cands = wq.rank([dict(c(10, BIG, priority=26)), dict(c(11, SMALL, priority=15)), dict(c(12, BIG, priority=10))], {})
    assert sorted(x["id"] for x in wq.busy_filter(cands, "priority", busy, {})) == [
        10,
        11,
    ]  # 27b at 26 outranks; 27b at 10 doesn't
    # holding its queue's worth of time already: only the outranking work, not more of the same
    assert [x["id"] for x in wq.busy_filter(cands, "priority", busy, {}, same_ok=False)] == [10]
    # holding priority-queue work: nothing from the standard table may preempt it, whatever its level
    std = wq.rank([dict(c(20, BIG, priority=99))], {})
    assert wq.busy_filter(std, "standard", busy, {}) == []
    # holding backup work: priority-queue work for another model always may
    held_backup = [dict(busy[0], queue="backup")]
    assert [x["id"] for x in wq.busy_filter(wq.rank([dict(c(30, BIG, priority=0))], {}), "priority", held_backup, {})] == [30]
