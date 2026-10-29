"""sjc_hw readings and the sjc-agent's command safety, against the mock ipmitool / nvidia-smi (no hardware)."""
import importlib.util
import json
import os
import sys
import time
import types

import pytest

from conftest import ROOT, mock_cmd

from sjc_hw import gpu, idrac, safety  # noqa: E402
from sjc_hw.config import settings as S  # noqa: E402


@pytest.fixture
def agent(tmp_path, monkeypatch):
    """The agent module with paho stubbed (commands don't touch MQTT), mocks for both tools, a temp fan-control state."""
    fake = types.ModuleType("paho"); fake.mqtt = types.ModuleType("paho.mqtt")
    fake.mqtt.client = types.ModuleType("paho.mqtt.client"); fake.mqtt.client.Client = object
    for name, mod in (("paho", fake), ("paho.mqtt", fake.mqtt), ("paho.mqtt.client", fake.mqtt.client)):
        monkeypatch.setitem(sys.modules, name, mod)
    log = tmp_path / "ipmi.log"
    monkeypatch.setenv("MOCK_IPMI_LOG", str(log))
    monkeypatch.setattr(S, "nvidia_smi", mock_cmd("nvidia-smi.py"))
    monkeypatch.setattr(S, "fan_control_state", str(tmp_path / "fan-control.json"))
    monkeypatch.setattr(S, "limits_file", str(tmp_path / "missing-limits.json"))
    spec = importlib.util.spec_from_file_location("sjc_agent", os.path.join(ROOT, "services", "sjc-agent", "sjc_agent.py"))
    a = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(a)
    a.latest["idrac"] = idrac.sensors()
    a.latest["gpu"] = None
    return a, log, tmp_path


def fan_control_state(tmp_path, age_s):
    (tmp_path / "fan-control.json").write_text(json.dumps({"ts": time.time() - age_s, "mode": "run"}))


def test_idrac_sensors_parse_the_bmc_output():
    v = idrac.sensors()
    assert "error" not in v
    assert [f["rpm"] for f in v["fans"]] == [4080, 4200, 4080] and v["redundancy"] == "Fully Redundant"
    assert {t["name"] for t in v["temps"]} >= {"CPU1", "CPU2", "Inlet", "Exhaust"}
    assert v["psu"]["ps1"] == {"v": 230.0, "a": 0.8, "status": "Presence detected"}
    assert v["power_w"] == 252
    assert idrac.dcmi()["w"] == 252


def test_gpu_readings(monkeypatch):
    monkeypatch.setattr(S, "nvidia_smi", mock_cmd("nvidia-smi.py"))
    (g,) = gpu.gpus()
    assert g["name"] == "Mock GPU 24GB" and g["power_limit_w"] == 400 and g["mem_total"] == 24564 * 1024 * 1024


def test_no_gpu_tooling_means_none(monkeypatch):
    monkeypatch.setattr(S, "nvidia_smi", "")
    assert gpu.gpus() is None


def test_emergency_limits_from_the_limits_file(tmp_path, monkeypatch):
    lim = {"kinds": {"cpu": {"red": 99}, "gpu": {"emergency": [92, 90]}, "inlet": {"fans": False, "red": 30}},
           "hosts": {"test-host": {"cpu": {"red": 86}}}}
    (tmp_path / "limits.json").write_text(json.dumps(lim))
    monkeypatch.setattr(S, "limits_file", str(tmp_path / "limits.json"))
    e = safety.emergency_limits()
    assert e["cpu"] == 86 and e["gpu"] == 92 and "inlet" not in e
    hot = safety.over_limit([{"name": "CPU1", "c": 87}], [{"index": 0, "temp": 80}])
    assert hot == ["CPU1 87C >= 86C"]


def test_fan_command_refused_while_fan_control_runs(agent):
    a, log, tmp = agent
    fan_control_state(tmp, age_s=2)
    r = a.command("fans", {"pct": 40})
    assert r["ok"] is False and "fan-control is running" in r["error"]
    assert not log.exists()                            # nothing sent to the BMC


def test_manual_fans_with_a_lease_then_back_to_dell_auto(agent):
    a, log, tmp = agent
    fan_control_state(tmp, age_s=3600)                 # stale: fan-control is not running
    r = a.command("fans", {"pct": 40, "by": "test"})
    assert r["ok"] and r["pct"] == 40
    assert "raw 0x30 0x30 0x02 0xff 0x28" in log.read_text()
    a.manual["until"] = time.time() - 1                # lease ran out
    a.guard_tick()
    assert a.manual["pct"] is None and a.manual["released"]["why"] == "lease ran out"
    assert log.read_text().strip().endswith("raw 0x30 0x30 0x01 0x01")   # Dell auto


def test_fan_percent_range_and_emergency_refusal(agent, monkeypatch):
    a, log, tmp = agent
    fan_control_state(tmp, age_s=3600)
    assert a.command("fans", {"pct": 5})["ok"] is False
    a.latest["idrac"] = {"temps": [{"name": "CPU1", "c": 95.0, "ok": True}]}   # past the built-in CPU Emergency (90)
    r = a.command("fans", {"pct": 50})
    assert r["ok"] is False and "Emergency" in r["error"]


def test_gpu_limit_range(agent):
    a, log, tmp = agent
    fan_control_state(tmp, age_s=3600)
    a.latest["gpu"] = gpu.gpus()
    assert a.command("gpu_limit", {"w": 999})["ok"] is False
    assert a.command("gpu_limit", {"w": 300}) == {"ok": True, "index": 0, "w": 300}
