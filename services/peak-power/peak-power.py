#!/usr/bin/env python3
# True power peaks from the iDRAC's own high-rate "System Peak Power" counter (ipmitool delloem powermonitor), which catches
# sub-second spikes that the dashboard's 1-per-second DCMI polling misses (2026-09-28: "peak usage is larger than the
# graph shows"). Every WINDOW s: read the peak since the last reset, then reset it. Writes {ts, peak_w, peak_at, window_s}.
# Run as root: peak-power.py <out.json> [ipmitool command...]
import json, os, re, subprocess, sys, time

OUT = sys.argv[1] if len(sys.argv) > 1 else "/dev/shm/peak-power.json"
IPMI = sys.argv[2:] or ["ipmitool"]
WINDOW = 30


def run(*args):
    return subprocess.run(IPMI + list(args), capture_output=True, text=True, timeout=30).stdout


def peak():
    out = run("delloem", "powermonitor")
    m = re.search(r"System Peak Power.*?Peak Time\s*:\s*(.+?)\s*Peak Reading\s*:\s*(\d+)\s*W", out, re.S)
    return (int(m.group(2)), m.group(1).strip()) if m else (None, None)


run("delloem", "powermonitor", "clear", "peakpower")
start = time.time()
while True:
    time.sleep(max(1, start + WINDOW - time.time()))
    try:
        w, at = peak()
        run("delloem", "powermonitor", "clear", "peakpower")
        now = time.time()
        if w is not None:
            tmp = OUT + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"ts": now, "peak_w": w, "peak_at": at, "window_s": round(now - start)}, f)
            os.replace(tmp, OUT)
        start = now
    except Exception as e:
        print("peak-power:", e, flush=True)
        start = time.time()
