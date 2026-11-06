"""Metrics recorder: rules applied to agent payloads, bucket combining, tier and view choice (pure; no MQTT, no database)."""

from __future__ import annotations

from test_tools import load

mr = load("services/metrics-recorder/metrics_recorder.py", "metrics_recorder")
RULES = mr.load_rules()


def rule(metric: str) -> dict:
    return next(r for r in RULES["metrics"] if r["metric"] == metric)


def test_every_rule_names_a_known_topic_and_aggregate():
    topics = {"power", "system", "sensors", "idrac", "safety", "gpu", "disks", "health", "runlevel"}
    for r in RULES["metrics"]:
        assert r["topic"] in topics and r["agg"] in ("avg", "max", "min"), r


def test_simple_and_summed_paths():
    system = {"cpu_pct": 12.5, "mem": {"pct": 40.0}, "net_io": {"eno1": {"rx_bps": 100, "tx_bps": 5}, "eno2": {"rx_bps": 50, "tx_bps": 1}},
              "cpu_pkg_w": {"package-0": 44.3, "package-1": 51.9}, "dstate": {"count": 3}}  # fmt: skip
    assert mr.extract(system, rule("cpu_pct")) == [("cpu_pct", 12.5)]
    assert mr.extract(system, rule("net_rx_bps")) == [("net_rx_bps", 150.0)]
    [(name, w)] = mr.extract(system, rule("cpu_pkg_w"))
    assert name == "cpu_pkg_w" and abs(w - 96.2) < 1e-9
    assert mr.extract(system, rule("dstate")) == [("dstate", 3.0)]
    assert mr.extract({"cpu_pkg_w": {}}, rule("cpu_pkg_w")) == []  # first sample after a restart: nothing yet


def test_list_reduce_and_min():
    sensors = {"temps": [{"c": 54.8}, {"c": 89.4}, {"c": 61}], "fans": [{"rpm": 1198}, {"rpm": 0}]}
    assert mr.extract(sensors, rule("temp_max_c")) == [("temp_max_c", 89.4)]
    assert mr.extract(sensors, rule("fan_max_rpm")) == [("fan_max_rpm", 1198.0)]
    idrac = {"psu": {"ps1": {"v": 230.0}, "ps2": {"v": 226.0}}}
    assert mr.extract(idrac, rule("psu_volt_min")) == [("psu_volt_min", 226.0)]


def test_per_item_metrics_are_named_from_the_item():
    gpu = {"cards": [{"index": 0.0, "temp": 63.0, "fan_rpm": 1102.0}, {"index": 1.0, "temp": 70.0, "fan_rpm": None}]}
    assert mr.extract(gpu, rule("gpu{index}_temp_c")) == [("gpu0_temp_c", 63.0), ("gpu1_temp_c", 70.0)]
    assert mr.extract(gpu, rule("gpu{index}_fan_rpm")) == [("gpu0_fan_rpm", 1102.0)]  # a null reading is skipped
    assert mr.extract({"cards": None}, rule("gpu{index}_temp_c")) == []
    disks = {"filesystems": [{"mount": "/", "pct": 12.9}, {"mount": "/c", "pct": 78.0, "remote": True},
                             {"mount": "/var/lib/mysql", "pct": 44.8, "same_as": "/n4"}, {"mount": "/n4", "pct": 44.8}]}  # fmt: skip
    assert mr.extract(disks, rule("disk_pct:{mount}")) == [("disk_pct:/", 12.9), ("disk_pct:/n4", 44.8)]


def test_combine_and_choices():
    assert mr.combine("max", [1, 5, 2]) == 5 and mr.combine("min", [3, 1]) == 1 and mr.combine("avg", [1, 2, 3]) == 2
    assert mr.tier_for(RULES, 3600) == "1m" and mr.tier_for(RULES, 86400) == "1m" and mr.tier_for(RULES, 7 * 86400) == "1h"
    assert [mr.view_bucket(RULES, r) for r in (600, 3600, 21600, 86400, 30 * 86400)] == [60, 60, 120, 300, 3600]


def test_ingest_buckets_by_minute_and_combines_on_flush(monkeypatch):
    rec = mr.Recorder.__new__(mr.Recorder)  # no database: exercise ingest + the combining step only
    rec.rules, rec.base = RULES, RULES["tiers"][0]
    rec.by_topic = {}
    for m in RULES["metrics"]:
        rec.by_topic.setdefault(m["topic"], []).append(m)
    rec.acc, rec.aggs, rec.lock = {}, {}, __import__("threading").Lock()
    rec.latest, rec.hold, rec.sampled = {}, {}, [d for d in RULES["derived"] if d.get("per_sample")]
    rec.seen = {"messages": 0, "points": 0, "last_flush": 0.0, "errors": 0}
    rec.ingest("sjc/srv2/power", b'{"w": 400, "peak_5s": 450}', now=120.0)
    rec.ingest("sjc/srv2/power", b'{"w": 520, "peak_5s": 600}', now=150.0)
    rec.ingest("sjc/srv2/power", b'{"w": 300}', now=185.0)  # next minute
    rec.ingest("sjc/srv2/status", b"online", now=150.0)  # not a rule topic: ignored
    rec.ingest("other/srv2/power", b'{"w": 1}', now=150.0)
    assert rec.acc[(120.0, "srv2", "power_w")] == [400.0, 520.0] and rec.acc[(180.0, "srv2", "power_w")] == [300.0]
    assert mr.combine(rec.aggs["power_w"], rec.acc[(120.0, "srv2", "power_w")]) == 520.0  # power keeps the minute's peak


def test_hottest_follows_the_hubs_rule():
    idrac = {"temps": [{"name": "Inlet", "c": 30}, {"name": "Exhaust", "c": 41}, {"name": "CPU1", "c": 55}]}
    sensors = {"temps": [{"chip": "coretemp", "label": "Package id 0", "c": 61}, {"chip": "coretemp", "label": "Core 3", "c": 70},
                         {"chip": "nvme", "label": "Composite", "c": 49}, {"chip": "enp5s0", "label": "MAC", "c": 77}]}  # fmt: skip
    assert mr.extract(idrac, rule("_hot_bmc")) == [("_hot_bmc", 55.0)]  # Inlet excluded
    assert mr.extract(sensors, rule("_hot_cpu")) == [("_hot_cpu", 61.0)]  # package only, not single cores
    assert mr.extract(sensors, rule("_hot_nvme")) == [("_hot_nvme", 49.0)]  # NIC sensor left out
    vals = {(60.0, "srv1", "_hot_bmc"): 55.0, (60.0, "srv1", "_hot_cpu"): 61.0, (60.0, "srv1", "drive_temp_max_c"): 39.0}
    assert mr.derive(RULES, vals) == {(60.0, "srv1", "hottest_c"): 61.0}


def test_psu_volt_per_supply():
    idrac = {"psu": {"ps1": {"v": 230.0, "a": 1.2}, "ps2": {"v": 226.0}}}
    assert sorted(mr.extract(idrac, rule("psu_volt:{key}"))) == [("psu_volt:ps1", 230.0), ("psu_volt:ps2", 226.0)]


def test_laptop_power_per_reading_prefers_battery_draw():
    d = next(x for x in RULES["derived"] if x["metric"] == "power_w")
    latest = {("laptop", "cpu_pkg_w"): (55.0, 100.0), ("laptop", "gpu0_power_w"): (74.0, 98.0)}
    assert mr.per_sample(d, latest, "laptop", 101.0) == 129.0
    latest[("laptop", "_battery_w")] = (40.0, 100.0)
    assert mr.per_sample(d, latest, "laptop", 101.0) == 40.0  # on battery: the battery draw
    assert mr.per_sample(d, latest, "laptop", 130.0) is None  # nothing fresh


def test_backfill_maps_the_hub_series_the_charts_read(monkeypatch):
    monkeypatch.setenv("METRICS_BACKFILL_BMC_HOSTS", "srv1,srv2")
    monkeypatch.setenv("METRICS_BACKFILL_GPU_HOSTS", "srv2,gpu1")
    bf = load("services/metrics-recorder/backfill_hub_stats.py", "backfill_hub_stats")
    assert bf.map_row("power", "srv1") == ("srv1", "power_peak_w", 1.0)
    assert bf.map_row("power", "srv2_peak") == ("srv2", "power_true_peak_w", 1.0)
    assert bf.map_row("power", "gpu1_gpu") == ("gpu1", "gpu0_power_w", 1.0)
    assert bf.map_row("power", "laptop") == ("laptop", "power_w", 1.0)
    assert bf.map_row("temp", "srv2") == ("srv2", "hottest_c", 1.0) and bf.map_row("temp", "srv2_gpu") == (
        "srv2",
        "gpu0_temp_c",
        1.0,
    )
    assert bf.map_row("fan", "srv1") == ("srv1", "bmc_fan_max_rpm", 1.0) and bf.map_row("fan", "srv2_gpu") == (
        "srv2",
        "gpu0_fan_rpm",
        1.0,
    )
    assert bf.map_row("srv2", "cpu") == ("srv2", "cpu_pct", 100.0)
    assert bf.map_row("copy", "laptop") is None and bf.map_row("remaining", "srv2_move") is None
    rows = bf.convert([("1h", "T", "volt", "srv2_ps1", 115.0), ("1h", "T", "volt", "srv2_ps2", 113.0)])
    assert ("1h", "T", "srv2", "psu_volt:ps2", 113.0) in rows and ("1h", "T", "srv2", "psu_volt_min", 113.0) in rows
