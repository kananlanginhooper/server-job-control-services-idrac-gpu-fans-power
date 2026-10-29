"""Shared settings for every service in this repo: ONE .env at the repo root (copy .env.example), read by all of them.

Lookup order for env("NAME"):
  1. <HOST>_NAME from the process environment, then from .env   (HOST = this machine's short hostname, upper-cased,
     non-alphanumerics -> "_"; e.g. R720_ROLE, TUG_GPU_POWER_W), so one shared file can hold per-server values
  2. NAME from the process environment, then from .env
  3. the default given by the caller
Adding a server is a config change: give it its own <HOST>_ keys where it differs from the shared ones.
"""

from __future__ import annotations

import os
import re
import socket
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
ENV_FILE = os.environ.get("SERVICES_ENV_FILE", os.path.join(ROOT, ".env"))
HOST = re.sub(r"[^A-Z0-9]", "_", socket.gethostname().split(".")[0].upper())


def _read(path: str) -> dict[str, str]:
    vals = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                vals[k.strip()] = v
    except OSError:
        pass
    return vals


FILE = _read(ENV_FILE)


def env(name: str, default: Any = None) -> Any:
    for key in (f"{HOST}_{name}", name):
        for src in (os.environ, FILE):
            v = src.get(key)
            if v not in (None, ""):
                return v
    return default


def env_int(name: str, default: int | str) -> int:
    return int(env(name, default))
