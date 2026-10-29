"""Temperature safety, power scaling and run-mode monitoring - read-only views of the controllers in controllers/.

fan-control.py owns the fans, Emergency (a sensor past its limit) and the GPU power limit on each server.
The power scaler scales power caps down (and back up) to hold the combined power target.
This module reads their state files and never writes them. The agent's own manual-control guard lives in
agent/sjc_agent.py and uses emergency_limits() / over_limit() from here.
"""
from __future__ import annotations

import time

from .config import settings
from .shell import read_json, unit_active

FAN_CONTROL_KEYS = ("ts", "mode", "profile", "fan_pct", "driven_by", "quick_cool", "emergency", "emergency_detail",
                    "adaptive", "gpu_paused", "gpu_case_floor", "cpus", "nvme", "drives", "raid", "exhaust", "gpus")

# fan-control.py's built-in Emergency table, (enter C, clear C): the fallback when the limits file has no value
EMERGENCY = {"ssd": (70, 63), "nvme": (80, 70), "toshiba": (48, 44), "hdd": (58, 52), "cpu": (90, 80),
             "exhaust": (50, 45), "gpu": (90, 87), "raid": (97, 95)}
IDRAC_KIND = {"CPU1": "cpu", "CPU2": "cpu", "Exhaust": "exhaust"}   # iDRAC temperature name -> limits kind (Inlet is info only)


def fan_control(events: int = 30) -> dict:
    """fan-control's own state: mode, what drives the fans, Emergency, adaptive phase, and its recent events."""
    s = read_json(settings.fan_control_state) or {}
    out = {k: s.get(k) for k in FAN_CONTROL_KEYS if k in s}
    out["events"] = (s.get("events") or [])[-events:]
    out["unit"] = unit_active(settings.fan_control_unit) if settings.fan_control_unit else None
    out["age_s"] = round(time.time() - s["ts"], 1) if s.get("ts") else None
    out["running"] = out["age_s"] is not None and out["age_s"] <= settings.controller_stale_s
    out["in_emergency"] = bool(s.get("emergency"))
    return out


def controller_running() -> bool:
    """fan-control counts as running while its state file is fresh (works the same inside a container)."""
    s = read_json(settings.fan_control_state) or {}
    return bool(s.get("ts")) and time.time() - s["ts"] <= settings.controller_stale_s


def limits() -> dict | None:
    """Component limits (yellow/red/aim/emergency per sensor kind), shared by fan-control and the dashboard."""
    return read_json(settings.limits_file)


def emergency_limits() -> dict[str, float]:
    """kind -> guard temperature (C) for this host, from the limits file (hosts.<host> overrides kinds.<kind>):
    the Emergency enter value ("emergency": [enter, clear]); kinds without an Emergency (the CPU throttles itself) use red.
    Kinds the file doesn't list keep fan-control's built-in table. Display-only kinds ("fans": false) are skipped."""
    out = {k: v[0] for k, v in EMERGENCY.items()}
    j = limits() or {}
    over = (j.get("hosts") or {}).get(settings.host) or {}
    for k, v in (j.get("kinds") or {}).items():
        v = dict(v, **(over.get(k) or {}))
        em = v.get("emergency")
        if v.get("fans") is False:
            out.pop(k, None)
        elif isinstance(em, (list, tuple)) and em:
            out[k] = float(em[0])
        elif isinstance(em, (int, float)):
            out[k] = float(em)
        elif v.get("red") is not None:
            out[k] = float(v["red"])
    return out


def over_limit(idrac_temps: list[dict], gpus: list[dict] | None) -> list[str]:
    """Sensors at or past their Emergency temperature, e.g. ["CPU1 91C >= 90C"]."""
    lim, hot = emergency_limits(), []
    for t in idrac_temps or []:
        k = IDRAC_KIND.get(t["name"])
        if k in lim and t["c"] >= lim[k]:
            hot.append(f"{t['name']} {t['c']:.0f}C >= {lim[k]:.0f}C")
    for g in gpus or []:
        if g.get("temp") is not None and "gpu" in lim and g["temp"] >= lim["gpu"]:
            hot.append(f"GPU{int(g['index'] or 0)} {g['temp']:.0f}C >= {lim['gpu']:.0f}C")
    return hot


def scaler() -> dict | None:
    if not settings.scaler_state:
        return None
    st = read_json(settings.scaler_state) or {}
    return {"unit": unit_active(settings.scaler_unit) if settings.scaler_unit else None, "state": st}


def runlevel() -> dict:
    """Run mode as fan-control sees it (run-profile file + the profile it reports) and any power throttle in force."""
    fc = read_json(settings.fan_control_state) or {}
    rp = read_json(settings.run_profile_file) or {}
    return {"profile_file": rp, "profile_active": fc.get("profile"), "mode": fc.get("mode"),
            "matches": (rp.get("profile") or "daytime") == fc.get("profile") if fc else None,
            "throttle": read_json(settings.throttle_file), "scaler": scaler()}
