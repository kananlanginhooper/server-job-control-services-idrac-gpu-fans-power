#!/usr/bin/env python3
# Per-minute GPU server GPU performance table (recovery.gpu_perf): temperature, watts used, watt cap, clock, utilization,
# card fan, case fan RPM and mode/profile from the GPU server agent (polled every 5 s), plus that minute's processing
# throughput from copilot_history.calls. Throughput is recomputed for the last 15 minutes each minute, because a
# call is recorded when it finishes.   usage: gpu-perf.py            (service)
#                                             gpu-perf.py --backfill <gpu-run-trace.csv>
from __future__ import annotations

import collections
import json
import os
import sys
import time
import urllib.request

import pymysql

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "lib"))
from svcenv import env  # noqa: E402  (shared repo .env; see .env.example)

AGENT = env("GPU_PERF_AGENT_URL", "http://localhost:8091").rstrip("/") + "/"  # the GPU server's dash-agent
DB, EVERY = env("RECOVERY_DB_NAME", "recovery"), 5
CALLS_TABLE = env("GPU_PERF_CALLS_TABLE", "")  # optional per-call LLM log (created_at, prompt/output tokens + ms, error)
SCHEMA = """create table if not exists gpu_perf(
  ts datetime primary key, mode varchar(24), samples int,
  temp_avg double, temp_max int, draw_avg double, draw_max double, limit_w double, util_avg double,
  sm_mhz double, card_fan double, case_rpm double, emergency tinyint, mem_used_mb double, mem_total_mb double, mem_bw_pct double, pcie_rx_mbs double, pcie_tx_mbs double, ram_read_mbs double, ram_write_mbs double,
  calls int, prompt_tokens bigint, output_tokens bigint, prompt_tps double, output_tps double)"""
INSERT = (
    "insert into gpu_perf(ts, mode, samples, temp_avg, temp_max, draw_avg, draw_max, limit_w, util_avg, sm_mhz, card_fan, case_rpm, emergency,"
    " mem_used_mb, mem_total_mb, mem_bw_pct, pcie_rx_mbs, pcie_tx_mbs, ram_read_mbs, ram_write_mbs) values(from_unixtime(%s), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) as n on duplicate key update "
    "mode=n.mode, samples=n.samples, temp_avg=n.temp_avg, temp_max=n.temp_max, draw_avg=n.draw_avg, draw_max=n.draw_max, "
    "limit_w=n.limit_w, util_avg=n.util_avg, sm_mhz=n.sm_mhz, card_fan=n.card_fan, case_rpm=n.case_rpm, emergency=n.emergency, "
    "mem_used_mb=n.mem_used_mb, mem_total_mb=n.mem_total_mb, mem_bw_pct=n.mem_bw_pct, pcie_rx_mbs=n.pcie_rx_mbs, pcie_tx_mbs=n.pcie_tx_mbs, ram_read_mbs=n.ram_read_mbs, ram_write_mbs=n.ram_write_mbs"
)
THROUGHPUT = """update gpu_perf p join (
    select from_unixtime(floor(unix_timestamp(created_at) / 60) * 60) m, count(*) calls, sum(prompt_tokens) pt, sum(prompt_ms) pm,
           sum(output_tokens) ot, sum(output_ms) om
    from {calls} where created_at >= now() - interval %s minute and error is null group by m) c on c.m = p.ts
  set p.calls = c.calls, p.prompt_tokens = c.pt, p.output_tokens = c.ot,
      p.prompt_tps = if(c.pm > 0, c.pt / (c.pm / 1000), null), p.output_tps = if(c.om > 0, c.ot / (c.om / 1000), null)""".replace(
    "{calls}", CALLS_TABLE
)


def db() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=env("RECOVERY_DB_HOST", "127.0.0.1"),
        port=int(env("RECOVERY_DB_PORT", 3306)),
        user=env("RECOVERY_DB_USER"),
        password=env("RECOVERY_DB_PASSWORD"),
        database=DB,
        charset="utf8mb4",
        autocommit=True,
    )


def row(minute: int, samples: list[dict]) -> tuple:
    modes = collections.Counter(s["mode"] for s in samples)
    n = len(samples)
    avg = lambda k: sum(s[k] for s in samples if s[k] is not None) / max(1, sum(1 for s in samples if s[k] is not None))
    return (
        minute,
        modes.most_common(1)[0][0],
        n,
        avg("temp"),
        max(s["temp"] for s in samples),
        avg("draw"),
        max(s["draw"] for s in samples),
        samples[-1]["limit"],
        avg("util"),
        avg("sm"),
        avg("card"),
        avg("rpm") or None,
        int(any(s["emerg"] for s in samples)),
        avg("mem") or None,
        max((s.get("memt") or 0) for s in samples) or None,
        *(avg(k) if any(s.get(k) is not None for s in samples) else None for k in ("bw", "rx", "tx", "rr", "rw")),
    )


def backfill(path: str) -> None:
    per = collections.defaultdict(list)
    for line in open(path):
        f = line.strip().split(",")
        if f[0] == "ts" or len(f) < 10:
            continue
        ts = int(f[0])
        per[ts // 60 * 60].append(
            {
                "mode": f[1],
                "temp": int(f[2]),
                "draw": float(f[3]),
                "limit": float(f[4]),
                "util": float(f[5]),
                "sm": float(f[6]),
                "card": float(f[9]),
                "rpm": float(f[8]) if f[8] else None,
                "emerg": f[1] == "emergency",
                "mem": None,
                "memt": None,
                "bw": None,
                "rx": None,
                "tx": None,
                "rr": None,
                "rw": None,
            }
        )
    c = db().cursor()
    c.execute(SCHEMA)
    c.executemany(INSERT, [row(m, s) for m, s in sorted(per.items())])
    CALLS_TABLE and c.execute(THROUGHPUT, (24 * 60,))
    print(f"backfilled {len(per)} minutes")


def main() -> None:
    c = db().cursor()
    c.execute(SCHEMA)
    for col in ("mem_used_mb", "mem_total_mb", "mem_bw_pct", "pcie_rx_mbs", "pcie_tx_mbs", "ram_read_mbs", "ram_write_mbs"):
        c.execute(
            "select count(*) from information_schema.columns where table_schema=%s and table_name='gpu_perf' and column_name=%s",
            (DB, col),
        )
        if not c.fetchone()[0]:
            c.execute(f"alter table gpu_perf add column {col} double")
    samples, minute = [], None
    while True:
        t = time.time()
        try:
            f = json.load(urllib.request.urlopen(AGENT, timeout=4))
            f = f.get("fast", f)
            fc = f.get("fan_control") or {}
            g = fc.get("gpu")
            if g and t - fc.get("ts", 0) < 30:
                fans = [x["rpm"] for x in (f.get("ipmi") or {}).get("fans") or []]
                ag = f.get("gpu") or {}
                mem = ag["mem_used"] / 1e6 if ag.get("mem_used") is not None else None
                memt = ag["mem_total"] / 1e6 if ag.get("mem_total") is not None else None
                rb = f.get("ram_bw") or {}
                rb_ok = rb.get("ts") and t - rb["ts"] < 30
                m = int(t // 60 * 60)
                if minute is not None and m != minute and samples:
                    try:
                        c.execute(INSERT, row(minute, samples))
                        CALLS_TABLE and c.execute(THROUGHPUT, (15,))
                    except pymysql.MySQLError:
                        c = db().cursor()
                        c.execute(INSERT, row(minute, samples))
                        CALLS_TABLE and c.execute(THROUGHPUT, (15,))
                    samples = []
                minute = m
                samples.append(
                    {
                        "mode": fc.get("mode"),
                        "temp": g["temp"],
                        "draw": g["draw"],
                        "limit": g.get("limit_set") or g["limit"],
                        "util": g["util"],
                        "sm": g["sm_mhz"],
                        "card": g["card_fan"],
                        "rpm": sum(fans) / len(fans) if fans else None,
                        "emerg": bool(g.get("emergency")),
                        "mem": mem,
                        "memt": memt,
                        "bw": ag.get("mem_util"),
                        "rx": ag["pcie_rx"] / 1e6 if ag.get("pcie_rx") is not None else None,
                        "tx": ag["pcie_tx"] / 1e6 if ag.get("pcie_tx") is not None else None,
                        "rr": rb["read_bps"] / 1e6 if rb_ok and rb.get("read_bps") is not None else None,
                        "rw": rb["write_bps"] / 1e6 if rb_ok and rb.get("write_bps") is not None else None,
                    }
                )
        except Exception as e:
            print("gpu-perf:", e, flush=True)
        time.sleep(max(0.5, EVERY - (time.time() - t)))


if __name__ == "__main__":
    backfill(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[1] == "--backfill" else main()
