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
    fake = types.ModuleType("paho")
    fake.mqtt = types.ModuleType("paho.mqtt")
    fake.mqtt.client = types.ModuleType("paho.mqtt.client")
    fake.mqtt.client.Client = object
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
    lim = {
        "kinds": {"cpu": {"red": 99}, "gpu": {"emergency": [92, 90]}, "inlet": {"fans": False, "red": 30}},
        "hosts": {"test-host": {"cpu": {"red": 86}}},
    }
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
    assert not log.exists()  # nothing sent to the BMC


def test_manual_fans_with_a_lease_then_back_to_dell_auto(agent):
    a, log, tmp = agent
    fan_control_state(tmp, age_s=3600)  # stale: fan-control is not running
    r = a.command("fans", {"pct": 40, "by": "test"})
    assert r["ok"] and r["pct"] == 40
    assert "raw 0x30 0x30 0x02 0xff 0x28" in log.read_text()
    a.manual["until"] = time.time() - 1  # lease ran out
    a.guard_tick()
    assert a.manual["pct"] is None and a.manual["released"]["why"] == "lease ran out"
    assert log.read_text().strip().endswith("raw 0x30 0x30 0x01 0x01")  # Dell auto


def test_fan_percent_range_and_emergency_refusal(agent, monkeypatch):
    a, log, tmp = agent
    fan_control_state(tmp, age_s=3600)
    assert a.command("fans", {"pct": 5})["ok"] is False
    a.latest["idrac"] = {"temps": [{"name": "CPU1", "c": 95.0, "ok": True}]}  # past the built-in CPU Emergency (90)
    r = a.command("fans", {"pct": 50})
    assert r["ok"] is False and "Emergency" in r["error"]


def test_gpu_limit_range(agent):
    a, log, tmp = agent
    fan_control_state(tmp, age_s=3600)
    a.latest["gpu"] = gpu.gpus()
    assert a.command("gpu_limit", {"w": 999})["ok"] is False
    assert a.command("gpu_limit", {"w": 300}) == {"ok": True, "index": 0, "w": 300}


def test_gpu_limit_refused_when_gpu_is_read_only(agent, monkeypatch):
    a, log, tmp = agent
    fan_control_state(tmp, age_s=3600)
    a.latest["gpu"] = gpu.gpus()
    monkeypatch.setattr(S, "gpu_readonly", True)
    r = a.command("gpu_limit", {"w": 300})
    assert r["ok"] is False and "read-only" in r["error"]


def test_filesystems_lists_real_host_mounts_with_space():
    from types import SimpleNamespace

    from sjc_hw import system

    mounts = "\n".join(
        [
            "/dev/sda2 / ext4 rw,relatime 0 0",
            "proc /proc proc rw 0 0",
            "/dev/md0 /raid6 xfs ro,shutdown 0 0",
            "overlay /var/lib/docker/overlay2/x/merged overlay rw 0 0",
            "/dev/loop3 /snap/core/1 squashfs ro 0 0",
            "/dev/sdb1 /b ext4 rw 0 0",
            "/dev/sda2 /var/lib/mysql ext4 rw 0 0",
            "192.0.2.2:/export/c /c nfs4 rw 0 0",
        ]
    )
    seen = []

    def fake(p):
        seen.append(p)
        return SimpleNamespace(f_blocks=1000, f_frsize=4096, f_bavail=250, f_bfree=300)

    fs = system.filesystems(mounts, statvfs=fake, root="/host/root")
    assert [f["mount"] for f in fs] == ["/", "/raid6", "/b", "/var/lib/mysql", "/c"]
    assert seen[:3] == ["/host/root/", "/host/root/raid6", "/host/root/b"]
    assert fs[3]["same_as"] == "/" and fs[0]["same_as"] is None and fs[4]["remote"] and not fs[0]["remote"]
    assert fs[0]["total"] == 4096000 and fs[0]["free"] == 1024000 and fs[0]["used"] == 2867200 and fs[0]["pct"] == 70.0
    assert fs[1]["ro"] and fs[1]["shutdown"] and not fs[2]["ro"]


def test_mdraid_reads_state_and_degraded_arrays():
    from sjc_hw import system

    mdstat = """Personalities : [raid6] [raid5] [raid4]
md0 : active raid6 sdc[2] sdb[1] sda[0] sdd[3]
      3906764800 blocks super 1.2 level 6, 512k chunk, algorithm 2 [4/3] [UUU_]
      [==>..................]  recovery = 12.6% (245760/1953382400) finish=120.0min

unused devices: <none>
"""
    md = system.mdraid(mdstat)
    assert len(md) == 1 and md[0]["name"] == "md0" and md[0]["level"] == "raid6" and md[0]["members"] == 4
    assert md[0]["active"] == 3 and md[0]["degraded"] is True and md[0]["sync"].startswith("[==>")


def test_health_scan_skips_usb_and_reads_megaraid_bays():
    from sjc_hw import health

    scan = "\n".join(
        [
            "/dev/sda -d scsi # /dev/sda, SCSI device",
            "/dev/sdb -d sat # /dev/sdb [USB] ...",
            "/dev/bus/0 -d megaraid,4 # /dev/bus/0 [megaraid_disk_04], SCSI device",
            "/dev/bus/0 -d sat+megaraid,0 # /dev/bus/0 [megaraid_disk_00] [SAT], ATA device",
            "/dev/nvme0 -d nvme # /dev/nvme0, NVMe device",
        ]
    )
    assert health.scan_targets(scan, usb={"sdb"}) == [
        ("/dev/sda", "scsi", "sda"),
        ("/dev/bus/0", "megaraid,4", "bay 4"),
        ("/dev/bus/0", "sat+megaraid,0", "bay 0"),
        ("/dev/nvme0", "nvme", "nvme0"),
    ]


def test_health_kernel_errors_counts_recent_lines_per_device():
    from datetime import datetime

    from sjc_hw import health

    dmesg = "\n".join(
        [
            "2026-11-01T09:00:00,000000-04:00 sd 0:0:0:0: [sdc] I/O error, dev sdc, sector 12",
            "2026-11-01T10:30:00,000000-04:00 blk_update_request: I/O error, dev sdc, sector 99",
            "2026-11-01T10:31:00,000000-04:00 nvme nvme0: I/O 12 QID 3 timeout, reset controller",
            "2026-11-01T10:32:00,000000-04:00 usb 1-1: new device",
        ]
    )
    since = datetime.fromisoformat("2026-11-01T10:00:00-04:00")
    assert health.kernel_errors(dmesg, since) == {"sdc": 1, "nvme0": 1}


def test_health_smart_parse_nvme_and_ata():
    from sjc_hw import health

    nv = health.smart_parse(
        {"model_name": "X", "smart_status": {"passed": True}, "temperature": {"current": 41},
         "nvme_smart_health_information_log": {"percentage_used": 3, "available_spare": 100, "media_errors": 0, "critical_warning": 0}},
        "nvme0", 0,
    )  # fmt: skip
    assert nv["proto"] == "NVMe" and nv["wear"] == 3 and nv["temp"] == 41 and nv["passed"] is True
    ata = health.smart_parse(
        {"ata_smart_attributes": {"table": [{"id": 5, "raw": {"value": 2}}, {"id": 199, "raw": {"value": 7}}]}}, "sda", 4
    )
    assert ata["realloc"] == 2 and ata["crc"] == 7 and ata["proto"] == "ATA"


@pytest.mark.skipif(sys.platform == "win32", reason="RAPL zone names contain a colon (not a valid Windows file name)")
def test_rapl_watts_per_package_handles_wrap(tmp_path):
    from sjc_hw import system

    z = tmp_path / "intel-rapl:0"
    z.mkdir()
    (z / "name").write_text("package-0")
    (z / "max_energy_range_uj").write_text("1000000000")
    sub = tmp_path / "intel-rapl:0:0"
    sub.mkdir()
    (sub / "name").write_text("core")
    (sub / "energy_uj").write_text("5")
    system._last.pop("rapl", None)
    (z / "energy_uj").write_text("999000000")
    assert system.rapl_watts(100.0, str(tmp_path)) == {}  # first call: no delta yet
    (z / "energy_uj").write_text("99000000")  # wrapped: +100 J in 2 s
    assert system.rapl_watts(102.0, str(tmp_path)) == {"package-0": 50.0}
    assert system.rapl_watts(103.0, str(tmp_path / "none")) is None
