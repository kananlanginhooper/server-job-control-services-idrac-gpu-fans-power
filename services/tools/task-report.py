#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "lib"))
from svcenv import env  # noqa: E402  (shared repo .env; see .env.example)

URL = env("HUB_API_URL", "http://localhost:8090") + "/api/tasks"
UNITS = {"": 1, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}


def number(v: str) -> float:
    m = re.fullmatch(r"([\d.,]+)\s*([KMGT]?)i?B?(?:/s)?", str(v).strip(), re.I)
    if not m:
        raise argparse.ArgumentTypeError(f"not a number: {v}")
    return float(m.group(1).replace(",", "")) * UNITS[m.group(2).upper()]


ap = argparse.ArgumentParser(description="Report a task to the dashboard's Tasks table (POST /api/tasks).")
ap.add_argument("id", nargs="?")
for f in ("title", "group", "host", "owner", "phase", "message", "error", "parent", "log"):
    ap.add_argument("--" + f)
ap.add_argument("--state", choices=["queued", "running", "paused", "blocked", "done", "failed", "cancelled"])
ap.add_argument("--unit", choices=["bytes", "files", "items", "percent", "seconds"])
for f in ("done", "total", "rate"):
    ap.add_argument("--" + f, type=number)
ap.add_argument("--eta", type=number, dest="eta_s")
ap.add_argument("--heartbeat", type=int, dest="heartbeat_s")
ap.add_argument("--delete", action="store_true")
ap.add_argument("--json", help="post task objects from a JSON file instead")
ap.add_argument("--url", default=URL)
a = ap.parse_args()

if a.json:
    with open(a.json, encoding="utf-8") as f:
        body = json.load(f)
elif a.id:
    body = {k: v for k, v in vars(a).items() if k not in ("json", "url", "delete") and v is not None}
    if a.delete:
        body = {"id": a.id, "delete": True}
else:
    ap.error("give a task id or --json")

req = urllib.request.Request(a.url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=5) as r:
        res = json.loads(r.read() or b"{}")
except urllib.error.HTTPError as e:
    try:
        res = json.loads(e.read() or b"{}")
    except ValueError:
        res = {"ok": False, "errors": [f"HTTP {e.code}"]}
except (urllib.error.URLError, OSError) as e:
    print(f"dashboard unreachable: {e}", file=sys.stderr)
    sys.exit(2)
if not res.get("ok"):
    print("; ".join(res.get("errors") or ["rejected"]), file=sys.stderr)
    sys.exit(1)
