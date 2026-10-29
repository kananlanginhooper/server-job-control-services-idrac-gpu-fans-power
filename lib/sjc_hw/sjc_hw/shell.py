"""Run a command or read a file on this server, or over ssh when SJC_REMOTE is set (local development)."""
from __future__ import annotations

import json
import shlex
import subprocess
from typing import Any

from .config import settings


def run(cmd: list[str], timeout: float = 20, input: str | None = None) -> subprocess.CompletedProcess:
    if settings.remote:
        cmd = ["ssh", "-o", "BatchMode=yes", settings.remote, " ".join(shlex.quote(c) for c in cmd)]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input)


def read_text(path: str) -> str | None:
    if not path:
        return None
    if settings.remote:
        try:
            r = run(["cat", path], timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return r.stdout if r.returncode == 0 else None
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def read_json(path: str, default: Any = None) -> Any:
    t = read_text(path)
    try:
        return json.loads(t) if t else default
    except ValueError:
        return default


def unit_active(unit: str) -> str:
    """systemctl is-active: "active", "inactive", "failed", ... or "unknown" when systemd can't be asked."""
    try:
        return run(["systemctl", "is-active", unit], timeout=10).stdout.strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
