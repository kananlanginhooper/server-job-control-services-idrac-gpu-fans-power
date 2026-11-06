"""NVIDIA GPU readings and power limit over nvidia-smi (ported from the original per-server dashboard agent)."""

from __future__ import annotations

import ctypes
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
    "pcie.link.gen.max",
    "pcie.link.width.max",
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
    "pcie_gen_max",
    "pcie_width_max",
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
    cards = parse(r.stdout) if r.returncode == 0 else None
    for c in cards or []:
        c["fan_rpm"] = fan_rpm(int(c["index"] or 0))  # nvidia-smi has no RPM; NVML does (driver 520+)
        if c.get("mem_used") is not None and c["mem_used"] <= 2 * 1024 * 1024:
            # a 1 MiB reading was seen once on a busy card (2026-11-01): confirm it with NVML before publishing it
            used = mem_used(int(c["index"] or 0))
            if used is not None:
                c["mem_used"] = used
    return cards


_nvml: dict = {"lib": None, "failed": False}


class _NvFan(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint), ("fan", ctypes.c_uint), ("speed", ctypes.c_uint)]


class _NvMem(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


def mem_used(index: int = 0) -> float | None:
    """Used VRAM in bytes straight from NVML; None if NVML isn't available."""
    if fan_rpm(index) is None and _nvml["lib"] is None:
        return None
    try:
        h = ctypes.c_void_p()
        if _nvml["lib"].nvmlDeviceGetHandleByIndex_v2(index, ctypes.byref(h)):
            return None
        m = _NvMem()
        return float(m.used) if _nvml["lib"].nvmlDeviceGetMemoryInfo(h, ctypes.byref(m)) == 0 else None
    except (OSError, AttributeError):
        return None


def fan_rpm(index: int = 0) -> float | None:
    """The card's first fan in RPM via NVML (libnvidia-ml, mounted by the NVIDIA runtime); None if unsupported."""
    if _nvml["failed"]:
        return None
    try:
        if _nvml["lib"] is None:
            lib = ctypes.CDLL("libnvidia-ml.so.1")
            if lib.nvmlInit_v2():
                _nvml["failed"] = True
                return None
            _nvml["lib"] = lib
        h = ctypes.c_void_p()
        if _nvml["lib"].nvmlDeviceGetHandleByIndex_v2(index, ctypes.byref(h)):
            return None
        s = _NvFan()
        s.version = ctypes.sizeof(_NvFan) | (1 << 24)
        return float(s.speed) if _nvml["lib"].nvmlDeviceGetFanSpeedRPM(h, ctypes.byref(s)) == 0 else None
    except (OSError, AttributeError):
        _nvml["failed"] = True
        return None


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
