#!/usr/bin/env python3
# NVMe watch for the hub: every minute, per controller: state, temperature, SMART error counters and kernel
# timeout/reset messages. Logs changes to /var/log/nvme-watch.log and reports each drive to the dashboard Tasks table.
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "lib"))
from svcenv import env  # noqa: E402  (shared repo .env; see .env.example)

LOG, STATE = "/var/log/nvme-watch.log", "/run/nvme-watch.json"
DASH = env("HUB_API_URL", "http://localhost:8090") + "/api/tasks"
HOST_KEY = env("HOST_KEY", "hub")
TEMP_ALERT = 84  # composite; Samsung 990 PRO warning 82, critical 85 (2026-09-24)
KERNEL_BAD = re.compile(r"nvme\d*.*(timeout|reset|abort|not ready|dead|I/O error|Removing after probe failure)", re.I)


def log(msg):
    with open(LOG, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")


def post(task):
    try:
        req = urllib.request.Request(DASH, json.dumps(task).encode(), {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass


def controllers():
    out = {}
    for c in sorted(glob.glob("/sys/class/nvme/nvme[0-9]*")):
        name = os.path.basename(c)
        info = {
            "state": open(f"{c}/state").read().strip(),
            "model": open(f"{c}/model").read().strip(),
            "serial": open(f"{c}/serial").read().strip(),
        }
        try:
            s = json.loads(
                subprocess.run(
                    ["nvme", "smart-log", f"/dev/{name}", "-o", "json"], capture_output=True, text=True, timeout=20
                ).stdout
            )
            info.update(
                temp=round(s["temperature"] - 273.15) if s["temperature"] > 200 else s["temperature"],
                critical_warning=s["critical_warning"],
                media_errors=s["media_errors"],
                err_log=s["num_err_log_entries"],
                used_pct=s.get("percent_used", s.get("percentage_used")),
            )
        except Exception as e:
            info["smart_error"] = str(e)[:120]
        out[name] = info
    return out


def kernel_lines(since):
    r = subprocess.run(
        ["journalctl", "-k", "--since", since, "-o", "short-iso", "--no-pager"], capture_output=True, text=True, timeout=30
    )
    return [l for l in r.stdout.splitlines() if KERNEL_BAD.search(l)]


def main():
    prev, base = {}, {}
    log("started")
    while True:
        now = controllers()
        bad_kernel = kernel_lines("-70s")
        for line in bad_kernel:
            log(f"KERNEL {line}")
        for name, d in now.items():
            base.setdefault(name, {k: d.get(k) for k in ("media_errors", "err_log")})
            problems = []
            if d["state"] != "live":
                problems.append(f"controller state {d['state']}")
            if d.get("critical_warning"):
                problems.append(f"critical warning {d['critical_warning']}")
            if d.get("temp") is not None and d["temp"] >= TEMP_ALERT:
                problems.append(f"{d['temp']}°C")
            for k in ("media_errors", "err_log"):
                if d.get(k) is not None and base[name][k] is not None and d[k] > base[name][k]:
                    problems.append(f"{k} rose {base[name][k]} -> {d[k]}")
            mine = [l for l in bad_kernel if name in l]
            if mine:
                problems.append(f"kernel: {mine[-1][-120:]}")
            summary = f"{d.get('temp', '?')}°C, {d['state']}"
            if problems != prev.get(name):
                log(f"{name} {d['model']} {d['serial']}: {summary}; " + ("; ".join(problems) if problems else "ok"))
            prev[name] = problems
            post(
                {
                    "id": f"nvme-watch/{name}",
                    "title": f"NVMe watch {name} ({d['model']})",
                    "group": "Drive health",
                    "host": HOST_KEY,
                    "owner": "recovery",
                    "heartbeat_s": 120,
                    "state": "failed" if problems else "running",
                    "phase": "watching",
                    "message": summary,
                    "error": "; ".join(problems) if problems else None,
                }
            )
        with open(STATE + ".tmp", "w") as f:
            json.dump({"ts": time.time(), "drives": now}, f)
        os.replace(STATE + ".tmp", STATE)
        time.sleep(60)


if __name__ == "__main__":
    main()
