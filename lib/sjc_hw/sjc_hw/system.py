"""Host system metrics via psutil and sysfs: CPU, memory, load, disk and network I/O, every temperature and fan sensor,
filesystem space per mount, software RAID, NIC link state, ECC error counts, RAM bandwidth.

In a container, the host's /proc is mounted at SJC_HOST_PROC (/host/proc) so the numbers are the server's, not the container's.
"""

from __future__ import annotations

import json
import os
import posixpath
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
    disks = {
        d: (c.read_bytes, c.write_bytes, getattr(c, "busy_time", 0))
        for d, c in (psutil.disk_io_counters(perdisk=True) or {}).items()
        if _whole_disk(d)
    }
    nics = {
        n: (c.bytes_recv, c.bytes_sent)
        for n, c in psutil.net_io_counters(pernic=True).items()
        if not n.startswith(("lo", "veth", "docker", "br-"))
    }
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
        "mem": {"total": vm.total, "used": vm.total - vm.available, "avail": vm.available, "pct": vm.percent},
        "swap": {"total": sw.total, "used": sw.used, "pct": sw.percent},
        "disk_io": {
            d: {"read_bps": r[0], "write_bps": r[1], "busy_pct": round(min(100.0, r[2] / 10), 1)} for d, r in disk_rates.items()
        },
        "net_io": {n: {"rx_bps": r[0], "tx_bps": r[1]} for n, r in _rates("net", now, nics).items()},
        "hwmon_max_c": temps,
        "nics": nic_links(nics),
        "cpu_pkg_w": rapl_watts(now),
        "dstate": dstate(),
        "edac": edac(),
        "ram_bw": _shm_json("imc-bw.json"),
        "peak_power": _shm_json("peak-power.json"),
        "uptime_s": round(now - psutil.boot_time()),
    }


def rapl_watts(now: float, base: str = "/sys/class/powercap") -> dict | None:
    """CPU package power per socket from RAPL energy counters (Intel and AMD): {package-0: W, ...}; None without RAPL."""
    zones = {}
    for z in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        if z.count(":") != 1:
            continue  # intel-rapl:0 is a package; intel-rapl:0:0 is a sub-zone (core, dram)
        name, e = _read(f"{base}/{z}/name"), _read(f"{base}/{z}/energy_uj")
        if name.startswith("package") and e.isdigit():
            zones[name] = (int(e), int(_read(f"{base}/{z}/max_energy_range_uj", "0") or 0))
    if not zones:
        return None
    prev, _last["rapl"] = _last.get("rapl"), (now, zones)
    if not prev or now <= prev[0]:
        return {}
    out = {}
    for name, (e, wrap) in zones.items():
        if name in prev[1]:
            d = e - prev[1][name][0]
            if d < 0 and wrap:
                d += wrap  # the counter wrapped
            out[name] = round(d / 1e6 / (now - prev[0]), 1) if d >= 0 else None
    return out


def dstate() -> dict:
    """Processes in uninterruptible sleep (D state, usually stuck on disk or NFS I/O): {count, names: [up to 10]}."""
    names = []
    try:
        for p in psutil.process_iter(["name", "status"]):
            if p.info.get("status") == psutil.STATUS_DISK_SLEEP:
                names.append(p.info.get("name") or "?")
    except (psutil.Error, OSError):
        pass
    return {"count": len(names), "names": sorted(set(names))[:10]}


def _read(p: str, default: str = "") -> str:
    try:
        with open(p) as f:
            return f.read().strip()
    except OSError:
        return default


def _shm_json(name: str) -> dict | None:
    """A JSON state file another host service writes to /dev/shm (RAM bandwidth, true power peak); None if absent."""
    try:
        return json.loads(_read(os.path.join(settings.host_shm, name)) or "null")
    except ValueError:
        return None


def nic_links(names) -> dict:
    """{nic: {up, speed_mbit}} from sysfs (the container shares the host's network namespace)."""
    out = {}
    for n in names:
        sp = _read(f"/sys/class/net/{n}/speed", "0")
        out[n] = {
            "up": _read(f"/sys/class/net/{n}/operstate") == "up",
            "speed_mbit": int(sp) if sp.isdigit() else None,
        }  # None: no link / not reported
    return out


def edac() -> dict:
    """Corrected / uncorrected ECC memory errors since boot, summed over memory controllers."""
    ce = ue = 0
    for mc in sorted(os.listdir("/sys/devices/system/edac/mc")) if os.path.isdir("/sys/devices/system/edac/mc") else []:
        ce += int(_read(f"/sys/devices/system/edac/mc/{mc}/ce_count", "0") or 0)
        ue += int(_read(f"/sys/devices/system/edac/mc/{mc}/ue_count", "0") or 0)
    return {"ce": ce, "ue": ue}


def _limit(c: float | None) -> float | None:
    """A sensor's high/critical limit, or None when the driver reports a placeholder (e.g. 65261.85 C on some NVMe sensors)."""
    return c if c and 0 < c < 200 else None


def sensors() -> dict:
    """Every hwmon temperature and fan reading (the max-per-chip summary stays in snapshot()['hwmon_max_c'])."""
    temps, fans = [], []
    try:
        for chip, rows in psutil.sensors_temperatures().items():
            for r in rows:
                if r.current:
                    temps.append(
                        {
                            "chip": chip,
                            "label": r.label or chip,
                            "c": r.current,
                            "high": _limit(r.high),
                            "crit": _limit(r.critical),
                        }
                    )
    except (AttributeError, OSError):
        pass
    try:
        for chip, rows in (psutil.sensors_fans() or {}).items():
            for i, r in enumerate(rows, 1):
                fans.append({"chip": chip, "label": r.label or f"fan{i}", "rpm": r.current})
    except (AttributeError, OSError):
        pass
    return {"at": time.time(), "temps": temps, "fans": fans}


REAL_FS = ("ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "vfat", "exfat", "ntfs", "ntfs3", "fuseblk", "f2fs", "nfs", "nfs4")


def filesystems(mounts_text: str, statvfs=None, root: str = "") -> list[dict]:
    """Space per real host mount: [{mount, dev, fstype, total, used, free, pct, ro, shutdown}] (pure apart from statvfs)."""
    statvfs = statvfs or os.statvfs
    out, seen = [], set()
    for line in mounts_text.splitlines():
        f = line.split()
        if len(f) < 4 or f[2] not in REAL_FS or f[1] in seen:
            continue
        dev, mnt, fstype, opts = f[0], f[1].replace("\\040", " "), f[2], f[3].split(",")
        if mnt.startswith(("/snap/", "/var/lib/docker", "/run/")):
            continue
        seen.add(f[1])
        try:
            st = statvfs(posixpath.join(root, mnt.lstrip("/")) if root else mnt)
            total, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
            used = total - st.f_bfree * st.f_frsize
            u = {"total": total, "used": used, "free": free, "pct": round(100 * used / total, 1) if total else None}
        except OSError:
            u = {"total": None, "used": None, "free": None, "pct": None}
        first = next((o["mount"] for o in out if o["dev"] == dev), None)
        out.append(
            {
                "mount": mnt,
                "dev": dev,
                "fstype": fstype,
                **u,
                "ro": "ro" in opts,
                "shutdown": "shutdown" in opts or "emergency_ro" in opts,
                "remote": fstype.startswith("nfs"),  # the space belongs to another server's filesystem
                "same_as": first,  # a bind mount or another mount of the same device: don't add its space twice
            }
        )
    return out


def mdraid(mdstat: str) -> list[dict]:
    """Software RAID arrays from /proc/mdstat: [{name, level, state, members, active, degraded, sync}]."""
    out, cur = [], None
    for line in mdstat.splitlines():
        f = line.split()
        if f and f[0].startswith("md") and len(f) > 3 and f[1] == ":":
            cur = {"name": f[0], "state": f[2], "level": f[3] if f[3].startswith("raid") else None, "members": len([x for x in f if "[" in x and x[0] != "["]),
                   "active": None, "degraded": False, "sync": None}  # fmt: skip
            out.append(cur)
        elif cur and ("recovery" in line or "resync" in line or "check" in line or "reshape" in line):
            cur["sync"] = " ".join(f[:4])
        elif cur and "[" in line and "/" in line and "]" in line:
            for tok in f:
                if tok.startswith("[") and "/" in tok:
                    n, a = tok.strip("[]").split("/")
                    cur["active"], cur["degraded"] = int(a), int(a) < int(n)
    return out


def disks() -> dict:
    proc = settings.host_proc
    return {
        "at": time.time(),
        "filesystems": filesystems(_read(f"{proc}/1/mounts") or _read(f"{proc}/mounts"), root=settings.host_root),
        "mdraid": mdraid(_read(f"{proc}/mdstat")),
    }
