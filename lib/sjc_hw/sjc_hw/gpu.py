"""NVIDIA GPU readings and power limit over nvidia-smi (ported from the original per-server dashboard agent)."""

from __future__ import annotations

import shlex

from .config import settings
from .shell import run

FIELDS = [
    "index",
    "name",
    "temperature.gpu",
    "utilization.gpu",
    "memory.used",
    "memory.total",
    "fan.speed",
    "power.draw",
    "power.limit",
    "power.min_limit",
    "power.max_limit",
    "clocks.sm",
    "utilization.memory",
    "pcie.link.gen.current",
    "pcie.link.width.current",
]
KEYS = [
    "index",
    "name",
    "temp",
    "util",
    "mem_used",
    "mem_total",
    "fan_pct",
    "power_w",
    "power_limit_w",
    "power_min_w",
    "power_max_w",
    "sm_mhz",
    "mem_util",
    "pcie_gen",
    "pcie_width",
]


def _num(v: str) -> float | None:
    try:
        return float(v)
    except ValueError:
        return None  # "[N/A]", "[Not Supported]"


def parse(csv: str) -> list[dict]:
    out = []
    for line in csv.splitlines():
        vals = [x.strip() for x in line.split(",")]
        if len(vals) != len(FIELDS):
            continue
        d = {k: (v if k == "name" else _num(v)) for k, v in zip(KEYS, vals)}
        for k in ("mem_used", "mem_total"):  # MiB -> bytes, like the agent
            if d[k] is not None:
                d[k] *= 1024 * 1024
        out.append(d)
    return out


def gpus() -> list[dict] | None:
    """None = no GPU tooling on this server; [] = tooling but no readable card."""
    if not settings.nvidia_smi:
        return None
    try:
        r = run(
            shlex.split(settings.nvidia_smi) + ["--query-gpu=" + ",".join(FIELDS), "--format=csv,noheader,nounits"], timeout=10
        )
    except Exception:
        return None
    return parse(r.stdout) if r.returncode == 0 else None


def compute_apps() -> list[dict]:
    r = run(
        shlex.split(settings.nvidia_smi) + ["--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader,nounits"],
        timeout=10,
    )
    out = []
    for line in r.stdout.splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) == 3:
            out.append({"pid": int(f[0]) if f[0].isdigit() else None, "name": f[1], "mem_mib": _num(f[2])})
    return out


def set_power_limit(watts: int, index: int = 0) -> None:
    r = run(settings.sudo + shlex.split(settings.nvidia_smi) + ["-i", str(index), "-pl", str(watts)], timeout=20)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[:200] or "nvidia-smi -pl failed")
