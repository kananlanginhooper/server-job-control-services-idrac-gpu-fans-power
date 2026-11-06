#!/usr/bin/python3
"""One-time backfill (owner, 2026-11-01): copy the classic hub's MySQL `stats` history into the recorder's `metrics`,
so long-range graphs don't start empty. Both tiers: `1m` (the hub's last 24 h) and `1h` (back to 2026-09-23).

The hub's rows are (tier, ts, group, sub, value), e.g. ("power", "<host>_peak"); map() names each one in the recorder's
terms. Points the recorder already has are never overwritten (ON CONFLICT DO NOTHING). Transfer metrics (copy speeds,
bytes remaining) aren't host statistics and stay in the hub's table.

    python3 backfill_hub_stats.py [--dry-run]      (on the hub; MySQL credentials from METRICS_BACKFILL_MYSQL_CNF)
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict

def hosts(key: str) -> tuple[str, ...]:
    return tuple(h.strip() for h in os.environ.get(key, "").split(",") if h.strip())


BMC_HOSTS = hosts("METRICS_BACKFILL_BMC_HOSTS")  # the servers whose power, temperature and fans came from a BMC
SERVERS = BMC_HOSTS + ("laptop",)
RATES = {"cpu": ("cpu_pct", 100.0), "net_rx": ("net_rx_bps", 1.0), "net_tx": ("net_tx_bps", 1.0),
         "disk_read": ("disk_read_bps", 1.0), "disk_write": ("disk_write_bps", 1.0)}  # fmt: skip
GPU_HOST = {f"{h}_gpu": h for h in hosts("METRICS_BACKFILL_GPU_HOSTS") + ("laptop",)}
AGG = {"cpu_pct": "avg", "net_rx_bps": "avg", "net_tx_bps": "avg", "disk_read_bps": "avg", "disk_write_bps": "avg",
       "power_peak_w": "max", "power_true_peak_w": "max", "gpu0_power_w": "max", "power_w": "max", "hottest_c": "max",
       "gpu0_temp_c": "max", "bmc_fan_max_rpm": "max", "gpu0_fan_rpm": "max", "fan_max_rpm": "max",
       "psu_volt:ps1": "min", "psu_volt:ps2": "min", "psu_volt_min": "min"}  # fmt: skip


def map_row(group: str, sub: str) -> tuple[str, str, float] | None:
    """(group, sub) of a hub stats row -> (host, metric, scale), or None when it has no recorder equivalent."""
    if group in SERVERS and sub in RATES:
        return (group, RATES[sub][0], RATES[sub][1])  # the hub stored CPU as a 0-1 fraction
    if group == "power":
        if sub in BMC_HOSTS:
            return (sub, "power_peak_w", 1.0)  # the hub charted the BMC 5-s peak per minute
        if sub.endswith("_peak") and sub[:-5] in BMC_HOSTS:
            return (sub[:-5], "power_true_peak_w", 1.0)
        if sub in GPU_HOST:
            return (GPU_HOST[sub], "gpu0_power_w", 1.0)
        if sub == "laptop":
            return ("laptop", "power_w", 1.0)
    if group == "temp":
        if sub in SERVERS:
            return (sub, "hottest_c", 1.0)
        if sub in GPU_HOST:
            return (GPU_HOST[sub], "gpu0_temp_c", 1.0)
    if group == "fan":
        if sub in BMC_HOSTS:
            return (sub, "bmc_fan_max_rpm", 1.0)
        if sub == "laptop":
            return ("laptop", "fan_max_rpm", 1.0)
        if sub in GPU_HOST:
            return (GPU_HOST[sub], "gpu0_fan_rpm", 1.0)
    if group == "volt" and "_ps" in sub:
        h, ps = sub.split("_", 1)
        return (h, f"psu_volt:{ps}", 1.0)
    return None


def convert(rows) -> list[tuple]:
    """Hub rows -> recorder rows (tier, ts, host, metric, value), plus psu_volt_min = the lower PSU per minute/hour."""
    out, volts = [], defaultdict(list)
    for tier, ts, group, sub, value in rows:
        m = map_row(group, sub)
        if m is None or value is None:
            continue
        host, metric, scale = m
        out.append((tier, ts, host, metric, float(value) * scale))
        if metric.startswith("psu_volt:"):
            volts[(tier, ts, host)].append(float(value))
    out += [(t, ts, h, "psu_volt_min", min(v)) for (t, ts, h), v in volts.items()]
    return out


def main() -> None:
    import psycopg
    import pymysql

    dry = "--dry-run" in sys.argv
    src = pymysql.connect(read_default_file=os.path.expanduser(os.environ.get("METRICS_BACKFILL_MYSQL_CNF", "~/.my.cnf")), charset="utf8mb4")
    with src.cursor() as c:
        c.execute("select tier, ts, host, metric, value from stats where tier in ('1m', '1h')")
        rows = convert(c.fetchall())
    src.close()
    span = defaultdict(lambda: [None, None, 0])
    for tier, ts, *_ in rows:
        s = span[tier]
        s[0], s[1], s[2] = min(s[0] or ts, ts), max(s[1] or ts, ts), s[2] + 1
    print({t: (str(a), str(b), n) for t, (a, b, n) in span.items()}, len({(r[2], r[3]) for r in rows}), "host/metric series")
    if dry:
        return
    with psycopg.connect(os.environ.get("METRICS_DSN", "dbname=server_metrics"), autocommit=True) as db:
        with db.cursor() as cur:
            cur.executemany(
                "insert into metric_defs(metric, agg) values(%s, %s) on conflict (metric) do nothing", sorted(AGG.items())
            )
            n0 = db.execute("select count(*) from metrics").fetchone()[0]
            cur.executemany(
                "insert into metrics(tier, ts, host, metric, value) values(%s, %s at time zone 'America/New_York', %s, %s, %s) "
                "on conflict (tier, host, metric, ts) do nothing",
                rows,
            )
            n1 = db.execute("select count(*) from metrics").fetchone()[0]
    print(f"inserted {n1 - n0} of {len(rows)} rows (the rest were already recorded)")


if __name__ == "__main__":
    main()
