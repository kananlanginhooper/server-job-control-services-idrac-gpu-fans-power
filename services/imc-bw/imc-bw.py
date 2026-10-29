#!/usr/bin/env python3
# RAM bandwidth from the Intel memory-controller (uncore IMC) CAS counters; 1 CAS = 64 bytes.
# Run as root: sudo -n python3 imc-bw.py <out.json>. Writes {ts, read_bps, write_bps, sockets} every 5 s.
from __future__ import annotations

import ctypes
import glob
import json
import os
import re
import struct
import sys
import time

OUT = sys.argv[1] if len(sys.argv) > 1 else "/dev/shm/imc-bw.json"
EVERY = 5
libc = ctypes.CDLL(None, use_errno=True)
NR_perf_event_open = 298


def field(dev: str, name: str) -> str:
    with open(f"{dev}/{name}") as f:
        return f.read().strip()


def encode(dev: str, event: str) -> int:
    cfg = 0
    for part in field(dev, f"events/{event}").split(","):
        k, v = part.split("=")
        bits = field(dev, f"format/{k}")
        lo = int(re.search(r"config:(\d+)", bits).group(1))
        cfg |= int(v, 16) << lo
    return cfg


def open_counter(ptype: int, config: int, cpu: int) -> int:
    attr = struct.pack("IIQQQQQII", ptype, 64, config, 0, 0, 0, 0, 0, 0) + b"\0" * 8
    buf = ctypes.create_string_buffer(attr, 64)
    fd = libc.syscall(NR_perf_event_open, buf, -1, cpu, -1, 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "perf_event_open")
    return fd


def main() -> None:
    counters = []
    for dev in sorted(glob.glob("/sys/bus/event_source/devices/uncore_imc_*")):
        ptype = int(field(dev, "type"))
        cpus = [int(c) for c in re.split(r"[,-]", field(dev, "cpumask")) if c]
        for sock, cpu in enumerate(cpus):
            for kind in ("read", "write"):
                counters.append((sock, kind, open_counter(ptype, encode(dev, f"cas_count_{kind}"), cpu)))
    last, t_last = None, None
    while True:
        now = time.time()
        vals = [(s, k, struct.unpack("Q", os.read(fd, 8))[0]) for s, k, fd in counters]
        if last is not None:
            dt = now - t_last
            socks = {}
            for (s, k, v), (_, _, v0) in zip(vals, last):
                socks.setdefault(s, {"read_bps": 0.0, "write_bps": 0.0})[k + "_bps"] += (v - v0) * 64 / dt
            body = {
                "ts": now,
                "read_bps": sum(x["read_bps"] for x in socks.values()),
                "write_bps": sum(x["write_bps"] for x in socks.values()),
                "sockets": socks,
                "channels": len(counters) // 2,
            }
            with open(OUT + ".tmp", "w") as f:
                json.dump(body, f)
            os.chmod(OUT + ".tmp", 0o644)
            os.replace(OUT + ".tmp", OUT)
        last, t_last = vals, now
        time.sleep(EVERY)


if __name__ == "__main__":
    main()
