"""Settings, all from the repo's ONE shared .env (compose passes it in as env_file; lib/svcenv.py looks up <HOST>_KEY,
then KEY, so per-server values sit in the same file). Nothing host-specific is hard-coded.

The agent runs on every server: it reads that server's iDRAC, GPU, fan-control and power-scaler state and publishes
it to MQTT under <prefix>/<host>/..., and it runs the commands sent to <prefix>/<host>/cmd/<name>.
The backend subscribes to <prefix>/+/# and serves everything over HTTP.
"""
from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass, field

# svcenv sits next to this package in the repo (lib/) and is copied beside it in the agent image
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
try:
    from svcenv import HOST as _HOST, env
except ImportError:   # installed on its own: plain environment variables
    _HOST, env = os.uname().nodename.split(".")[0].upper() if hasattr(os, "uname") else "LOCALHOST", os.environ.get


@dataclass
class Settings:
    host: str = env("SJC_HOST", env("HOST_KEY", _HOST.lower()))   # this server's name (MQTT topic + UI label)
    mqtt_host: str = env("SJC_MQTT_HOST", "localhost")
    mqtt_port: int = int(env("SJC_MQTT_PORT", "1883"))
    prefix: str = env("SJC_MQTT_PREFIX", "sjc")
    remote: str = env("SJC_REMOTE", "")                         # dev only: run every command and file read over `ssh <remote>`

    # hardware access (agent)
    ipmitool: list[str] = field(default_factory=lambda: shlex.split(env("SJC_IPMITOOL", "ipmitool")))
    sdr_cache: str = env("SJC_SDR_CACHE", "/tmp/sjc-sdr.cache")
    nvidia_smi: str = env("SJC_NVIDIA_SMI", "")                 # empty = this server has no GPU
    sudo: list[str] = field(default_factory=lambda: shlex.split(env("SJC_SUDO", "")))   # "sudo -n" outside a container
    host_proc: str = env("SJC_HOST_PROC", "/proc")              # /host/proc in a container, for system metrics

    # controller state files (read-only)
    fan_control_state: str = env("SJC_FAN_CONTROL_STATE", "/run/fan-control.json")
    fan_control_unit: str = env("SJC_FAN_CONTROL_UNIT", "")     # systemd unit to ask; empty in a container (no systemd)
    run_profile_file: str = env("SJC_RUN_PROFILE_FILE", "/run/run-profile.json")
    limits_file: str = env("SJC_LIMITS_FILE", "/usr/local/etc/component-limits.json")
    throttle_file: str = env("SJC_THROTTLE_FILE", "/dev/shm/power-throttle.json")
    scaler_state: str = env("SJC_SCALER_STATE", "")   # set on the server that runs the power scaler
    scaler_unit: str = env("SJC_SCALER_UNIT", "")

    # publish intervals (agent)
    sensor_every_s: float = float(env("SJC_SENSOR_EVERY_S", "5"))
    power_every_s: float = float(env("SJC_POWER_EVERY_S", "1"))
    gpu_every_s: float = float(env("SJC_GPU_EVERY_S", "2"))
    safety_every_s: float = float(env("SJC_SAFETY_EVERY_S", "5"))
    system_every_s: float = float(env("SJC_SYSTEM_EVERY_S", "5"))

    # manual control safety (agent): only while fan-control is NOT running
    controller_stale_s: float = float(env("SJC_CONTROLLER_STALE_S", "30"))   # fan-control state older than this = not running
    manual_lease_s: float = float(env("SJC_MANUAL_LEASE_S", "600"))          # a manual fan % hands back to Dell auto after this
    manual_min_pct: int = int(env("SJC_MANUAL_MIN_PCT", "20"))

    # backend
    port: int = int(env("SJC_PORT", "8000"))
    legacy_api: str = env("SJC_LEGACY_API", "").rstrip("/")      # optional: forward other /api/* calls here; empty = none
    control_nets: tuple[str, ...] = tuple(env("SJC_CONTROL_NETS", "127.,10.,172.,192.168.").split(","))   # POSTs only from these


settings = Settings()
