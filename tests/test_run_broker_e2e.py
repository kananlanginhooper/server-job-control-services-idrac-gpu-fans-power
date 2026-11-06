"""Run broker end to end: the broker in-process and a real runner process, against a throwaway Postgres.

Runs only when TASKRUNNER_TEST_DSN points at a disposable database (its tables are dropped), on Linux.
scripts/taskrunner-e2e.sh starts one in Docker. It never touches a server's database.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest
from conftest import ROOT
from test_taskrunner_e2e import free_port, wait

DSN = os.environ.get("TASKRUNNER_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not DSN or sys.platform == "win32", reason="needs TASKRUNNER_TEST_DSN (Linux; see scripts/taskrunner-e2e.sh)"
)

# the columns of the hub's task_requests that the broker reads and writes (the real table has more)
REQUESTS = """
create table task_requests(
  id bigserial primary key,
  requested_by text not null default 'test', requested_at timestamptz not null default now(),
  title text not null, repo text, workdir text, command text not null, env jsonb not null default '{}',
  kind text not null default 'oneshot', every_secs int, needs jsonb not null default '{}',
  priority text not null default 'normal', wanted_by timestamptz,
  status text not null default 'requested', status_note text,
  placed_host text, placed_task text, placed_at timestamptz,
  started_at timestamptz, finished_at timestamptz, exit_code int
);
"""


def test_broker_places_runs_and_closes_requests(tmp_path):
    import psycopg
    from psycopg.rows import dict_row
    from test_tools import load

    db = psycopg.connect(DSN, autocommit=True)
    db.execute("drop schema if exists broker cascade")
    db.execute("drop table if exists tasks, task_runs, agent_state, task_requests cascade")
    db.execute(load("services/taskrunner/taskrunner.py", "taskrunner_be2e").SCHEMA)
    db.execute(REQUESTS)
    rb = load("services/run-broker/run_broker.py", "run_broker_e2e")

    hosts = {"testhost": {"cpu_slots": 2, "gpu_slots": 1, "gpu_vram_gb": 11}}
    broker = rb.Broker(DSN, hosts=hosts, held={"testhost"}, auto_accept=False, code_root=str(tmp_path), place_requests=True)
    bport = free_port()
    srv = rb.serve(broker, bport, tokens={"testhost": "test_token"})
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    stop = threading.Event()

    def ticks():
        while not stop.is_set():
            broker.tick()
            stop.wait(1)

    threading.Thread(target=ticks, daemon=True).start()
    base = f"http://127.0.0.1:{bport}/api/v1/agent"

    def http(path, token):
        r = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {token}"})
        try:
            return urllib.request.urlopen(r, timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code

    assert http("/servers/testhost/orders?version=0", "wrong") == 401
    assert http("/servers/testhost/orders?version=99&wait=0", "test_token") == 304

    def add(title, command, **kw):
        cols = dict(title=title, command=command, **kw)
        for k in ("needs", "env"):
            if k in cols:
                cols[k] = json.dumps(cols[k])
        sql = f"insert into task_requests({', '.join(cols)}) values({', '.join(['%s'] * len(cols))}) returning id"
        return db.execute(sql, list(cols.values())).fetchone()[0]

    def req(i):
        cur = db.cursor(row_factory=dict_row)
        return cur.execute("select * from task_requests where id = %s", (i,)).fetchone()

    (tmp_path / "repo").mkdir()
    ok = add("Say hello", 'echo "hello $TASK_REQUEST_ID" > out.txt', repo="repo", status="queued")
    waiting = add("Not accepted yet", "true")  # 'requested': the broker leaves it alone without auto-accept
    gpu = add("Needs the GPU", "true", needs={"gpu": "required"}, status="queued")
    longrun = add("Long one", "exec sleep 600", kind="continuous", status="queued")

    port = free_port()
    env = dict(
        os.environ,
        TASKRUNNER_DSN=DSN,
        TASKRUNNER_PORT=str(port),
        TASKRUNNER_LOGS=str(tmp_path / "logs"),
        TASKRUNNER_POWER_URL="http://127.0.0.1:9/none",
        TASKRUNNER_BROKER_URL=base,
        TASKRUNNER_BROKER_HOST="testhost",
        AGENT_TOKEN="test_token",
    )
    runner = subprocess.Popen([sys.executable, os.path.join(ROOT, "services", "taskrunner", "taskrunner.py")], env=env)
    try:
        # the oneshot is placed, runs in its repo directory with its request id, and the request closes as done
        wait(lambda: req(ok)["status"] == "done", secs=90)
        r = req(ok)
        assert r["placed_host"] == "testhost" and r["placed_task"] == f"req-{ok}-say-hello" and r["exit_code"] == 0
        assert r["started_at"] and r["finished_at"]
        assert (tmp_path / "repo" / "out.txt").read_text().strip() == f"hello {ok}"
        # its order is withdrawn and the runner removes the row
        wait(lambda: not db.execute("select 1 from tasks where name = %s", (r["placed_task"],)).fetchall())
        assert db.execute("select count(*) from broker.runs where task = %s", (r["placed_task"],)).fetchone()[0] == 1

        # not accepted: untouched; GPU lent out: queued with the reason
        assert req(waiting)["status"] == "requested" and req(waiting)["placed_task"] is None
        assert req(gpu)["placed_task"] is None and req(gpu)["status_note"] == "the GPUs are lent out; waits for them"

        # continuous: running; paused disables it (the copy stops); cancelled withdraws it
        wait(lambda: req(longrun)["status"] == "running")
        name = req(longrun)["placed_task"]
        open_runs = "select count(*) from task_runs where task = %s and ended_at is null"
        db.execute("update task_requests set status = 'paused' where id = %s", (longrun,))
        wait(lambda: db.execute(open_runs, (name,)).fetchone()[0] == 0)
        assert db.execute("select enabled, origin from tasks where name = %s", (name,)).fetchone() == (False, "broker")
        db.execute("update task_requests set status = 'cancelled' where id = %s", (longrun,))
        wait(lambda: not db.execute("select 1 from tasks where name = %s", (name,)).fetchall())

        # a local row is never touched by a sync
        db.execute("insert into tasks(name, command, enabled) values('mine', 'true', false)")
        time.sleep(3)
        assert db.execute("select origin from tasks where name = 'mine'").fetchone() == ("local",)
    finally:
        stop.set()
        runner.send_signal(signal.SIGTERM)
        runner.wait(60)
        srv.shutdown()
