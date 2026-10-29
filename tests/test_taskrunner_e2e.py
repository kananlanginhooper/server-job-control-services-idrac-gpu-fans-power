"""Task runner end to end: a real runner process against a throwaway Postgres and the mock dashboard.

Runs only when TASKRUNNER_TEST_DSN points at a disposable database (its tables are dropped), on Linux.
scripts/taskrunner-e2e.sh starts one in Docker. It never touches a server's task database.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import pytest
from conftest import ROOT
from dashboard_api import MockDashboard

DSN = os.environ.get("TASKRUNNER_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not DSN or sys.platform == "win32", reason="needs TASKRUNNER_TEST_DSN (Linux; see scripts/taskrunner-e2e.sh)"
)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait(cond, secs: float = 60, every: float = 1):
    end = time.time() + secs
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(every)
    raise AssertionError(f"timed out after {secs} s")


def test_runner(tmp_path):
    import psycopg

    db = psycopg.connect(DSN, autocommit=True)
    db.execute("drop table if exists tasks, task_runs cascade")
    sys.path.insert(0, os.path.join(ROOT, "services", "taskrunner"))
    from test_tools import load

    db.execute(load("services/taskrunner/taskrunner.py", "taskrunner_e2e").SCHEMA)

    def task(name, command, **kw):
        cols = dict(name=name, command=command, **kw)
        for k in ("instances_by_power",):
            if k in cols:
                cols[k] = json.dumps(cols[k])
        db.execute(f"insert into tasks({', '.join(cols)}) values({', '.join(['%s'] * len(cols))})", list(cols.values()))

    def runs(name):
        return db.execute("select count(*) from task_runs where task=%s", (name,)).fetchone()[0]

    task("sched", "true", kind="schedule", every_secs=3600)
    task("sized", "exec sleep 600", instances=1, instances_by_power={"1000": 1, "1500": 3}, stop_secs=5)
    task("one", "true", kind="oneshot")
    hhmm = time.strftime("%H:%M", time.localtime(time.time() - 3600))  # an hour ago: a new task must NOT catch up
    task("atjob", "true", kind="schedule", at_times=[hhmm])
    for days in (60, 50, 40):  # history of a removed task: pruning keeps only the newest row
        db.execute(
            "insert into task_runs(task, instance, started_at) values('gone', 0, now() - make_interval(days => %s))", (days,)
        )

    port = free_port()
    with MockDashboard() as dash:
        dash.state["powertarget"]["combined_w"] = 1500
        env = dict(
            os.environ,
            TASKRUNNER_DSN=DSN,
            TASKRUNNER_PORT=str(port),
            TASKRUNNER_LOGS=str(tmp_path / "logs"),
            TASKRUNNER_POWER_URL=dash.url + "/api/powertarget",
        )
        script = os.path.join(ROOT, "services", "taskrunner", "taskrunner.py")

        def status():
            try:
                st = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2))
                return {t["name"]: t for t in st["tasks"]}
            except Exception:
                return None

        runner = subprocess.Popen([sys.executable, script], env=env)
        try:
            # 1500 W: three copies; the schedule and the oneshot run once; the at-task waits; old history is pruned
            wait(lambda: (s := status()) and s["sized"]["running"] == 3 and s["sized"]["wanted"] == 3)
            wait(lambda: runs("sched") == 1 and runs("one") == 1)
            wait(lambda: db.execute("select enabled from tasks where name='one'").fetchone()[0] is False)
            assert status()["atjob"]["state"]["0"].startswith("next ") and runs("atjob") == 0
            ages = db.execute("select (now() - started_at) > interval '39 days' from task_runs where task='gone'").fetchall()
            assert ages == [(True,)]

            # 1000 W: scale down to one copy
            with dash.lock:
                dash.state["powertarget"]["combined_w"] = 1000
            wait(lambda: (s := status()) and s["sized"]["running"] == 1, secs=60)

            # restart the runner: the schedule must NOT run again (its last start comes from task_runs)
            runner.send_signal(signal.SIGTERM)
            runner.wait(60)
            runner = subprocess.Popen([sys.executable, script], env=env)
            wait(lambda: (s := status()) and s["sized"]["running"] == 1)
            time.sleep(12)  # a few ticks
            assert runs("sched") == 1 and runs("atjob") == 0
            assert status()["sched"]["state"]["0"] == "scheduled"
        finally:
            runner.send_signal(signal.SIGTERM)
            runner.wait(60)
