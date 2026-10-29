"""power-guard against the mock dashboard API: what it reads, when it triggers, and exactly what it POSTs."""

import importlib.util
import os

import pytest
from dashboard_api import MockDashboard

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = {
    "interval_s": 10,
    "peak_margin_w": 50,
    "cooldown_s": 20,
    "cooldown_avg_s": 60,
    "stale_s": 30,
    "hub_floor_w": 280,
    "hold_s": 900,
    "hold_repost_s": 300,
    "step_w": 40,
    "srv_floor_pkg_w": 55,
    "gpu_step_w": 8,
    "gpu_floor_w": 120,
    "gpu_max_w": 210,
    "up_margin_w": 60,
    "up_hold_s": 180,
    "hub_max_w": 550,
    "srv_max_pkg_w": 95,
    "up_step_frac": 0.5,
    "gpu_after_cpu_frac": 0.5,
}


@pytest.fixture
def guard(tmp_path, monkeypatch):
    with MockDashboard(hub="alpha", srv="beta") as dash:
        monkeypatch.setenv("HUB_API_URL", dash.url)
        monkeypatch.setenv("HUB_KEY", "alpha")
        monkeypatch.setenv("GPU_SERVER_KEY", "beta")
        monkeypatch.setenv("GPU_THROTTLE_KEY", "betagpu")
        spec = importlib.util.spec_from_file_location(
            "power_guard", os.path.join(ROOT, "services", "power-guard", "power-guard.py")
        )
        pg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pg)
        pg.LOG, pg.STATE = str(tmp_path / "guard.log"), str(tmp_path / "state.json")
        pg.srv_pkg_w = lambda: (_ for _ in ()).throw(RuntimeError("no ssh in tests"))
        yield pg, dash


def throttles(dash):
    return {b["host"]: b for p, b in dash.posts if p == "/api/throttle" and not b.get("clear")}


def test_readings_use_the_configured_host_keys(guard):
    pg, dash = guard
    dash.set_power(hub_peak=600, hub_avg=580, srv_peak=700, srv_avg=650, pkg_w=90, gpu_draw=200)
    x = pg.readings()
    assert x["combined_w"] == 1250 and x["srv_w"] == 1000
    assert x["comb_peak"] == 1300 and x["hub_peak"] == 600 and x["srv_pkg_w"] == 90 and x["gpu_draw"] == 200


def test_no_trigger_under_the_line(guard):
    pg, dash = guard
    dash.set_power(hub_peak=500, hub_avg=480, srv_peak=600, srv_avg=580)
    assert pg.triggers(pg.readings(), 50) == []


def test_combined_over_steps_both_servers_once_and_spares_an_idle_gpu(guard):
    pg, dash = guard
    dash.set_power(hub_peak=700, hub_avg=690, srv_peak=650, srv_avg=640, pkg_w=90, gpu_draw=200)
    x = pg.readings()
    trig = pg.triggers(x, 50)
    assert {s for s, _ in trig} == {"combined"}
    st = {"cap": {}, "posted": {}, "pkg_hist": [90]}
    pg.tighten(CFG, st, x, "combined", "test", dry=False)
    t = throttles(dash)
    assert t["beta"]["rapl_package_w"] == 70  # actual 90 W/pkg - step 40/2
    assert t["alpha"]["dcmi_w"] == 660  # hub 5-s peak 700 - step 40
    assert "betagpu" not in t  # CPUs barely throttled yet: the GPU is the last lever


def test_gpu_is_stepped_once_the_cpus_are_well_throttled(guard):
    pg, dash = guard
    dash.set_power(hub_peak=700, hub_avg=690, srv_peak=650, srv_avg=640, pkg_w=90, gpu_draw=200)
    x = pg.readings()
    st = {"cap": {"alpha": 320, "beta": 60}, "posted": {}, "pkg_hist": [62]}
    pg.tighten(CFG, st, x, "combined", "test", dry=False)
    assert throttles(dash)["betagpu"]["gpu_w"] == 192  # draw 200 - gpu_step 8


def test_floors_hold(guard):
    pg, dash = guard
    dash.set_power(hub_peak=300, srv_peak=900, pkg_w=56, gpu_draw=100)
    x = pg.readings()
    st = {"cap": {"alpha": 280, "beta": 55}, "posted": {}, "pkg_hist": [56]}
    assert pg.tighten(CFG, st, x, "combined", "test", dry=False) is None  # at every floor: nothing posted
    assert throttles(dash) == {}


def test_loosen_brings_the_gpu_back_first_then_clears(guard):
    pg, dash = guard
    dash.set_power(hub_peak=300, srv_peak=300)
    x = pg.readings()
    st = {"cap": {"alpha": 500, "beta": 80, "betagpu": 205}, "posted": {}}
    pg.loosen(CFG, st, x, dry=False)
    assert "betagpu" not in st["cap"]  # 205 + 8 >= max 210: released
    assert ("/api/throttle", {"host": "betagpu", "clear": True, "by": pg.BY}) in dash.posts
    assert st["cap"]["alpha"] == 500  # CPUs wait for the next up-step
    pg.loosen(CFG, st, x, dry=False)
    assert st["cap"]["alpha"] == 520 and st["cap"]["beta"] == 90  # half steps up (up_step_frac 0.5)


def test_dry_run_posts_nothing(guard):
    pg, dash = guard
    dash.set_power(hub_peak=700, srv_peak=650, pkg_w=90)
    st = {"cap": {}, "posted": {}, "pkg_hist": [90]}
    pg.tighten(CFG, st, pg.readings(), "combined", "test", dry=True)
    assert throttles(dash) == {} and st["cap"]


def test_config_with_host_named_keys_still_loads(guard):
    pg, _ = guard
    old = {k: v for k, v in CFG.items() if k not in ("hub_floor_w", "hub_max_w", "srv_floor_pkg_w", "srv_max_pkg_w")}
    old.update({"alpha_floor_w": 280, "alpha_max_w": 550, "beta_floor_pkg_w": 55, "beta_max_pkg_w": 95})
    c = pg.cfg_keys(old)
    assert (c["hub_floor_w"], c["hub_max_w"], c["srv_floor_pkg_w"], c["srv_max_pkg_w"]) == (280, 550, 55, 95)


def test_power_guard_dir_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("POWER_GUARD_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("pg2", os.path.join(ROOT, "services", "power-guard", "power-guard.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.LOG == os.path.join(str(tmp_path), "guard.log") and m.CFG == os.path.join(str(tmp_path), "power-guard.json")
