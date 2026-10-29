"""Task runner: wall-clock schedules and power-based sizing (pure functions; no database, no processes)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from test_tools import load

tr = load("services/taskrunner/taskrunner.py", "taskrunner")
UTC = timezone.utc
NY = ZoneInfo("America/New_York")


def ts(y, mo, d, h, mi, tz=UTC):
    return datetime(y, mo, d, h, mi, tzinfo=tz).timestamp()


def sched(**kw) -> dict:
    t = {"kind": "schedule", "every_secs": 3600, "at_times": None, "updated": 0}
    t.update(kw)
    return t


def test_every_secs_counts_from_the_recorded_last_start():
    now = ts(2026, 10, 29, 12, 0)
    assert tr.schedule_due(sched(), None, now, UTC)[0]  # never ran: run now (as before)
    assert not tr.schedule_due(sched(), now - 600, now, UTC)[0]
    assert tr.schedule_due(sched(), now - 3600, now, UTC)[0]


def test_at_times_run_once_per_slot():
    t = sched(at_times=["03:00", "15:30"])
    now = ts(2026, 10, 29, 12, 0)
    assert not tr.schedule_due(t, ts(2026, 10, 29, 3, 0), now, UTC)[0]  # 03:00 done, 15:30 not yet
    assert tr.schedule_due(t, ts(2026, 10, 28, 15, 30), now, UTC)[0]  # today's 03:00 was missed: catch up once
    due, why = tr.schedule_due(t, ts(2026, 10, 29, 3, 0), now, UTC)
    assert why == "next Thu 15:30"


def test_at_times_new_task_waits_for_its_first_slot():
    now = ts(2026, 10, 29, 14, 0)
    t = sched(at_times=["03:00"], updated=now - 60)  # added a minute ago, never ran
    due, why = tr.schedule_due(t, None, now, UTC)
    assert not due and why == "next Fri 03:00"
    assert tr.schedule_due(t, None, ts(2026, 10, 30, 3, 0), UTC)[0]


def test_at_times_follow_the_wall_clock_across_dst():
    # 2026-11-01: New York falls back; 03:00 local is 08:00 UTC that day, 07:00 UTC the day before
    prev, nxt = tr.at_slots(["03:00"], ts(2026, 11, 1, 2, 0, NY), NY)
    assert datetime.fromtimestamp(prev, UTC).hour == 7 and datetime.fromtimestamp(nxt, UTC).hour == 8
    assert nxt - prev == timedelta(hours=25).total_seconds()


def cont(**kw) -> dict:
    t = {"kind": "continuous", "instances": 2, "instances_by_power": None}
    t.update(kw)
    return t


def test_sizing_by_power_target():
    steps = {"1000": 1, "1500": 3}
    assert tr.wanted_instances(cont(instances_by_power=steps), {"combined_w": 1500}) == 3
    assert tr.wanted_instances(cont(instances_by_power=steps), {"combined_w": 1250}) == 1
    assert tr.wanted_instances(cont(instances_by_power=steps), {"combined_w": 800}) == 1  # below the lowest step
    assert tr.wanted_instances(cont(instances_by_power={"1000": 0, "1500": 2}), {"combined_w": 1000}) == 0


def test_sizing_falls_back_to_the_row_count():
    assert tr.wanted_instances(cont(instances_by_power={"1000": 5}), None) == 2  # power API unreachable
    assert tr.wanted_instances(cont(instances_by_power={"1000": 5}), {"scale": "up"}) == 2  # no combined_w
    assert tr.wanted_instances(cont(), {"combined_w": 1500}) == 2  # no steps
    assert tr.wanted_instances({"kind": "schedule", "instances": 4}, {"combined_w": 1500}) == 1


def test_settings_come_from_the_shared_env_file(tmp_path):
    """Regression: the runner must read HUB_API_URL from the shared .env (the unit sets SERVICES_ENV_FILE), or the
    GPU server / GPU desktop would ask localhost for the power target and never see Low Power."""
    import os
    import subprocess
    import sys

    from conftest import ROOT

    envf = tmp_path / "x.env"
    envf.write_text("HUB_API_URL=http://hub.example:8090\nTASKRUNNER_KEEP_DAYS=7\n")
    code = "import runpy; g = runpy.run_path('services/taskrunner/taskrunner.py'); print(g['POWER_URL'], g['KEEP_DAYS'])"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TASKRUNNER_", "HUB_"))}
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=dict(env, SERVICES_ENV_FILE=str(envf)), capture_output=True, text=True
    )
    assert out.stdout.split() == ["http://hub.example:8090/api/powertarget", "7"], out.stderr
