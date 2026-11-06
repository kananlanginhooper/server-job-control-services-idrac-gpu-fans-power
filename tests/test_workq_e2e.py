"""Work queues end to end: the broker in-process, a real GPU worker process and a fake Ollama, against a throwaway Postgres.

Runs only when TASKRUNNER_TEST_DSN points at a disposable database (its tables are dropped), on Linux.
scripts/taskrunner-e2e.sh starts one in Docker. It never touches a server or a GPU.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import ROOT
from test_taskrunner_e2e import free_port, wait

DSN = os.environ.get("TASKRUNNER_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not DSN or sys.platform == "win32", reason="needs TASKRUNNER_TEST_DSN (Linux; see scripts/taskrunner-e2e.sh)"
)
BIG, SMALL = "big:27b", "small:14b"


class FakeOllama:
    """/api/ps and a streamed /api/chat. A prompt 'slow' streams for 30 s; loading another model reports load_duration 2 s."""

    def __init__(self) -> None:
        self.loaded, self.calls, self.lock = None, [], threading.Lock()
        me = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                with me.lock:
                    ps = [{"name": me.loaded[0], "context_length": me.loaded[1]}] if me.loaded else []
                b = json.dumps({"models": ps}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                key = (body["model"], (body.get("options") or {}).get("num_ctx") or 2048)
                with me.lock:
                    load = 0 if me.loaded == key else 2_000_000_000
                    me.loaded = key
                    me.calls.append((body["model"], body["messages"][0]["content"]))
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                slow = body["messages"][0]["content"] == "slow"
                try:
                    for i in range(60 if slow else 2):
                        self.chunk({"message": {"role": "assistant", "content": f"w{i} "}, "done": False})
                        if slow:
                            time.sleep(0.5)
                    self.chunk({"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
                                "load_duration": load, "eval_count": 2})  # fmt: skip
                    self.wfile.write(b"0\r\n\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def chunk(self, obj):
                b = json.dumps(obj).encode() + b"\n"
                self.wfile.write(f"{len(b):x}\r\n".encode() + b + b"\r\n")
                self.wfile.flush()

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def test_work_queues(tmp_path):
    import psycopg
    from psycopg.rows import dict_row
    from test_tools import load

    db = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    db.execute("drop schema if exists broker cascade")
    db.execute("drop schema if exists work cascade")
    db.execute("drop table if exists task_requests cascade")
    db.execute("create table task_requests(id bigserial primary key)")  # unused here: placement is off
    sys.path.insert(0, os.path.join(ROOT, "services", "run-broker"))
    rb = load("services/run-broker/run_broker.py", "run_broker_wq")
    broker = rb.Broker(DSN, hosts={"testhost": {}}, held=set(), place_requests=False)
    bport = free_port()
    srv = rb.serve(broker, bport, tokens={"testhost": "tok"})
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    stop = threading.Event()

    def ticks():
        while not stop.is_set():
            broker.tick()
            stop.wait(1)

    threading.Thread(target=ticks, daemon=True).start()
    ollama = FakeOllama()

    def add(q, prompt, model=BIG, priority=0, study="s1", parent=None, ctx=8192, state="ready"):
        payload = {"api": "chat", "body": {"messages": [{"role": "user", "content": prompt}], "options": {"num_ctx": ctx}}}
        return db.execute(
            f"insert into work.{q}_items(study, model, priority, payload, parent_id, state) values(%s, %s, %s, %s, %s, %s) returning id",
            (study, model, priority, json.dumps(payload), parent, state),
        ).fetchone()["id"]

    def item(q, i):
        return db.execute(f"select * from work.{q}_items where id = %s", (i,)).fetchone()

    def order():  # prompts in the order the fake Ollama got them
        with ollama.lock:
            return [p for _, p in ollama.calls]

    # queued before the worker starts, all paused, so the order is decided in one go once resumed
    reg = add("standard", "reg-big", BIG, state="paused")
    pri_small = add("priority", "pri-small", SMALL, priority=1, state="paused")
    pri_a1 = add("priority", "pri-a1", BIG, study="a", state="paused")
    pri_a2 = add("priority", "pri-a2", BIG, study="a", state="paused")
    pri_b1 = add("priority", "pri-b1", BIG, study="b", state="paused")

    env = dict(
        os.environ,
        GPU_WORKER_BROKER_URL=f"http://127.0.0.1:{bport}/api/v1/agent",
        GPU_WORKER_HOST="testhost",
        GPU_WORKER_GPU="testhost-gpu0",
        GPU_WORKER_MODELS=f"{BIG},{SMALL}",
        GPU_WORKER_OLLAMA=ollama.url,
        TASKRUNNER_POWER_URL="http://127.0.0.1:9/none",
        AGENT_TOKEN="tok",
    )
    worker = subprocess.Popen([sys.executable, os.path.join(ROOT, "services", "gpu-worker", "gpu_worker.py")], env=env)
    try:
        assert db.execute("select work.resume('standard', study => 's1')").fetchone()["resume"] == 1
        db.execute("select work.resume('priority', study => 'a')")
        db.execute("select work.resume('priority', study => 'b')")
        db.execute("select work.resume('priority', study => 's1')")
        wait(lambda: item("standard", reg)["state"] == "done", secs=60)
        # level 1 first (a load), then level 0 with studies taking turns, then the standard queue last
        assert order() == ["pri-small", "pri-a1", "pri-b1", "pri-a2", "reg-big"]
        r = db.execute("select result from work.results where queue = 'priority' and item_id = %s", (pri_a1,)).fetchone()
        assert r["result"]["message"]["content"] == "w0 w1 " and item("priority", pri_a1)["stats"]["ctx"] == 8192
        loads = db.execute("select model, n, avg_s from work.model_loads order by model").fetchall()
        assert [(x["model"], x["n"], x["avg_s"]) for x in loads] == [(BIG, 1, 2.0), (SMALL, 1, 2.0)]
        assert all(item("priority", i)["state"] == "done" for i in (pri_small, pri_a2, pri_b1))
        st = {(r["queue"], r["study"]): r for r in db.execute("select * from work.studies").fetchall()}
        assert st[("priority", "a")]["done"] == 2 and st[("priority", "a")]["ready"] == 0 and st[("standard", "s1")]["done"] == 1
        d = db.execute("select result from work.done where queue = 'priority' and id = %s", (pri_b1,)).fetchone()
        assert d["result"]["message"]["content"] == "w0 w1 "

        # cancel a running item: the call is aborted and the next item runs; its follow-up tree is cancelled with it
        slow = add("priority", "slow", study="c")
        child = add("priority", "child", study="c", parent=slow, state="paused")
        wait(lambda: item("priority", slow)["state"] == "claimed")
        wait(lambda: (item("priority", slow)["progress"] or "").endswith("tokens"), secs=30)
        assert db.execute("select work.cancel('priority', root => %s)", (slow,)).fetchone()["cancel"] == 2
        nxt = add("priority", "after-cancel", study="c")
        wait(lambda: item("priority", nxt)["state"] == "done", secs=40)
        assert item("priority", slow)["state"] == "cancelled" and item("priority", child)["state"] == "cancelled"
        assert not db.execute("select 1 from work.results where queue = 'priority' and item_id = %s", (slow,)).fetchall()

        # the fallback model when the own model has nothing; the depth view
        fb = add("standard", "fallback", SMALL)
        wait(lambda: item("standard", fb)["state"] == "done", secs=40)

        # pin_gpu: an item pinned to another GPU is never claimed here; one pinned to this GPU is
        other = add("standard", "pinned-other", SMALL, study="pin", state="paused")
        mine = add("standard", "pinned-mine", SMALL, study="pin", state="paused")
        db.execute("update work.standard_items set pin_gpu = 'othergpu' where id = %s", (other,))
        db.execute("update work.standard_items set pin_gpu = 'testhost-gpu0' where id = %s", (mine,))
        db.execute("select work.resume('standard', study => 'pin')")
        wait(lambda: item("standard", mine)["state"] == "done", secs=40)
        time.sleep(3)
        assert item("standard", other)["state"] == "ready" and "pinned-other" not in order()
        db.execute("select work.cancel('standard', study => 'pin')")

        # batches sized in time: at 25 s per item, a 60-s queue takes 3; holding that, only outranking work comes
        for k in range(5):
            add("standard", f"probe-{k}", "probe:1b", study="probe")
        broker.item_s[("probe-gpu", "probe:1b", 8192)] = 25.0
        pb = {"gpu": "probe-gpu", "models": ["probe:1b"], "queue_s": 60}
        got = broker.wait_claim_many("testhost", pb, 20, 0)
        assert len(got) == 3
        assert broker.wait_claim_many("testhost", pb, 20, 0) == []
        hi = add("standard", "probe-hi", "probe:1b", priority=5, study="probe")
        assert [x["id"] for x in broker.wait_claim_many("testhost", pb, 20, 0)] == [hi]
        # a level raised after the claim comes back on the next sync, so the GPU's local queue can reorder
        db.execute("update work.standard_items set priority = 9 where id = %s", (got[2]["id"],))
        held = [{"queue": "standard", "id": x["id"], "progress": None} for x in got]
        assert broker.work_sync("probe-gpu", {"held": held})["levels"][f"standard/{got[2]['id']}"] == 9
        db.execute("select work.cancel('standard', study => 'probe')")
        assert db.execute("select count(*) n from work.depth").fetchone()["n"] == 0

        # backup: taken only when nothing else waits, and a running backup item gives way to new standard work
        bk = add("backup", "slow", study="fragments")
        wait(lambda: item("backup", bk)["state"] == "claimed")
        wait(lambda: (item("backup", bk)["progress"] or "").endswith("tokens"), secs=30)
        std = add("standard", "std-after-backup")
        wait(lambda: item("standard", std)["state"] == "done", secs=40)
        b = item("backup", bk)
        assert b["state"] in ("ready", "claimed") and b["attempt"] <= 1  # handed back uncounted, then taken again
        db.execute("select work.cancel('backup', study => 'fragments')")
        wait(lambda: item("backup", bk)["state"] == "cancelled")

        # a GPU that serves only the backup queue never gets standard work
        worker.send_signal(signal.SIGSTOP)  # keep the real worker out of it for a moment
        try:
            only_std = add("standard", "for-others", SMALL)
            req = urllib.request.Request(
                f"http://127.0.0.1:{bport}/api/v1/agent/servers/testhost/work/claim",
                data=json.dumps({"gpu": "testhost-gpu9", "models": [SMALL], "queues": ["backup"], "wait": 0}).encode(),
                headers={"Authorization": "Bearer tok", "Content-Type": "application/json"},
            )
            assert urllib.request.urlopen(req, timeout=10).status == 204
            assert item("standard", only_std)["state"] == "ready"
        finally:
            worker.send_signal(signal.SIGCONT)
        wait(lambda: item("standard", only_std)["state"] == "done", secs=40)

        # every HTTP request closes its own database connection (a thread-local connection leaked one per request)
        n = db.execute("select count(*) n from pg_stat_activity where application_name = 'run-broker'").fetchone()["n"]
        assert n <= 12, f"{n} broker connections open"  # one per open (kept-alive) worker connection, never growing

        # stopping the worker hands its running item straight back
        slow2 = add("standard", "slow")
        wait(lambda: item("standard", slow2)["state"] == "claimed")
        worker.send_signal(signal.SIGTERM)
        worker.wait(30)
        it = item("standard", slow2)
        assert it["state"] == "ready" and it["attempt"] == 0
    finally:
        stop.set()
        if worker.poll() is None:
            worker.kill()
        srv.shutdown()
        ollama.srv.shutdown()
