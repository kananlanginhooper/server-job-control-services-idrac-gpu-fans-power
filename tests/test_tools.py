"""gpu-perf aggregation, task-report against the mock dashboard, and the shell settings loader."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys

import pytest
from conftest import ROOT
from dashboard_api import MockDashboard


def load(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def sample(**kw) -> dict:
    s = {
        "mode": "adaptive",
        "temp": 70,
        "draw": 200.0,
        "limit": 210.0,
        "util": 90.0,
        "sm": 1800.0,
        "card": 80.0,
        "rpm": 5000.0,
        "emerg": False,
        "mem": 9000.0,
        "memt": 11000.0,
        "bw": 40.0,
        "rx": None,
        "tx": None,
        "rr": None,
        "rw": None,
    }
    s.update(kw)
    return s


def test_gpu_perf_minute_row():
    gp = load("services/gpu-perf/gpu-perf.py", "gpu_perf")
    r = gp.row(1000, [sample(temp=70, draw=200.0), sample(temp=74, draw=220.0, emerg=True, mode="lowpower"), sample(temp=72)])
    minute, mode, n, t_avg, t_max, d_avg, d_max, limit, util, sm, card, rpm, emerg, mem, memt, bw, rx, tx, rr, rw = r
    assert (minute, n, t_max, d_max, limit, emerg) == (1000, 3, 74, 220.0, 210.0, 1)
    assert t_avg == pytest.approx(72.0) and d_avg == pytest.approx(620 / 3)
    assert mode == "adaptive"  # the most common mode of the minute
    assert bw == pytest.approx(40.0) and rx is None  # a counter no sample had stays NULL


def test_task_report_posts_to_the_dashboard():
    with MockDashboard() as d:
        env = dict(os.environ, HUB_API_URL=d.url)
        r = subprocess.run(
            [
                sys.executable,
                os.path.join(ROOT, "services", "tools", "task-report.py"),
                "test/job",
                "--title",
                "Test job",
                "--done",
                "1.5K",
                "--total",
                "3K",
                "--state",
                "running",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode == 0, r.stderr
        ((path, body),) = d.posts
        t = body[0] if isinstance(body, list) else body
        assert path == "/api/tasks" and t["id"] == "test/job" and t["done"] == 1500 and t["total"] == 3000


BASH = shutil.which("bash")


@pytest.mark.skipif(not BASH or "system32" in BASH.lower(), reason="needs a POSIX bash (not the WSL launcher)")
def test_shell_loader_matches_the_python_one(tmp_path):
    envf = tmp_path / "x.env"
    envf.write_text('ROLE=hub\nMYHOST_ROLE=gpu-server\nPAIRS="a=1 b=2"\n')
    lib = os.path.join(ROOT, "lib", "svcenv.sh").replace("\\", "/")
    script = f'. "{lib}"; SVC_HOST=MYHOST; svc_env ROLE; SVC_HOST=OTHER; svc_env ROLE; svc_env PAIRS; svc_env NOPE dflt'
    out = subprocess.run(
        [BASH, "-c", script],
        env=dict(os.environ, SERVICES_ENV_FILE=str(envf).replace("\\", "/")),
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.split("\n")
    assert out[:4] == ["gpu-server", "hub", "a=1 b=2", "dflt"]
