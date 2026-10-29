"""Host system metrics (CPU, memory, load, disk and network I/O, hwmon temperatures) via psutil.

In a container, the host's /proc is mounted at SJC_HOST_PROC (/host/proc) so the numbers are the server's, not the container's.
"""
from __future__ import annotations

import time

import psutil

from .config import settings

psutil.PROCFS_PATH = settings.host_proc
_last: dict = {}


def _rates(key: str, now: float, counters: dict[str, tuple]) -> dict[str, list[float]]:
    """Per-second rates since the previous call, per device: {dev: [a/s, b/s, ...]}."""
    prev, _last[key] = _last.get(key), (now, counters)
    if not prev:
        return {}
    dt = now - prev[0]
    return {d: [round((a - b) / dt, 1) for a, b in zip(v, prev[1][d])] for d, v in counters.items() if d in prev[1] and dt > 0}


def _whole_disk(d: str) -> bool:
    """sda yes, sda1 no; nvme0n1 yes, nvme0n1p1 no; loop/ram/dm/md no."""
    if d.startswith("nvme"):
        return "p" not in d[4:]
    return d.startswith(("sd", "vd", "hd")) and not d[-1].isdigit()


def snapshot() -> dict:
    now = time.time()
    vm, sw = psutil.virtual_memory(), psutil.swap_memory()
    disks = {d: (c.read_bytes, c.write_bytes, getattr(c, "busy_time", 0))
             for d, c in (psutil.disk_io_counters(perdisk=True) or {}).items() if _whole_disk(d)}
    nics = {n: (c.bytes_recv, c.bytes_sent) for n, c in psutil.net_io_counters(pernic=True).items()
            if not n.startswith(("lo", "veth", "docker", "br-"))}
    temps = {}
    try:
        for chip, rows in psutil.sensors_temperatures().items():
            vals = [r.current for r in rows if r.current]
            if vals:
                temps[chip] = max(vals)
    except (AttributeError, OSError):
        pass
    disk_rates = _rates("disk", now, disks)
    return {
        "at": now,
        "cpu_pct": psutil.cpu_percent(interval=None),
        "cpu_count": psutil.cpu_count(),
        "load": list(psutil.getloadavg()),
        "mem": {"total": vm.total, "used": vm.total - vm.available, "pct": vm.percent},
        "swap": {"total": sw.total, "used": sw.used, "pct": sw.percent},
        "disk_io": {d: {"read_bps": r[0], "write_bps": r[1], "busy_pct": round(min(100.0, r[2] / 10), 1)} for d, r in disk_rates.items()},
        "net_io": {n: {"rx_bps": r[0], "tx_bps": r[1]} for n, r in _rates("net", now, nics).items()},
        "hwmon_max_c": temps,
        "uptime_s": round(now - psutil.boot_time()),
    }
