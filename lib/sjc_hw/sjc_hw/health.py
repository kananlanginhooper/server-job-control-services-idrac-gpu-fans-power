"""Drive and kernel health: SMART for every drive (direct disks, NVMe, and drives behind a MegaRAID/PERC controller),
kernel I/O errors in the last hour, ECC counts. Needs raw device access: the agent runs it only with SJC_HEALTH=1, which
the privileged compose add-on (services/sjc-agent/compose.health.yaml) sets. USB drives are skipped (intake drives are
never touched). Same fields as the dashboard's own health view (the hub and dash-agent), so it can switch over as is.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone

from .config import settings

KERR = re.compile(
    r"I/O error|critical medium error|medium error|unrecovered read|exception Emask|failed command|"
    r"nvme.*(timeout|reset)|resetting controller|megaraid.*(error|fault)|sense key",
    re.I,
)


def smart_parse(j: dict, dev: str, rc: int) -> dict:
    """One drive's smartctl -j -a output -> the dashboard's drive record."""
    temp = j.get("temperature") or {}
    d = {
        "dev": dev,
        "serial": j.get("serial_number"),
        "rc": rc,
        "model": j.get("model_name") or " ".join(x for x in (j.get("scsi_vendor"), j.get("scsi_product")) if x),
        "proto": "NVMe" if "nvme_smart_health_information_log" in j else "SAS" if "scsi_error_counter_log" in j else "ATA",
        "passed": (j.get("smart_status") or {}).get("passed"),
        "temp": temp.get("current"),
        "trip": temp.get("drive_trip"),
        "hours": (j.get("power_on_time") or {}).get("hours"),
        "size": (j.get("user_capacity") or {}).get("bytes"),
    }
    attrs = {a["id"]: a["raw"]["value"] for a in (j.get("ata_smart_attributes") or {}).get("table", [])}
    if attrs:
        d.update(
            realloc=attrs.get(5),
            pending=attrs.get(197),
            crc=attrs.get(199),
            uncorrect=(attrs.get(187) or 0) + (attrs.get(198) or 0),
        )
    n = j.get("nvme_smart_health_information_log")
    if n:
        d.update(
            wear=n.get("percentage_used"),
            spare=n.get("available_spare"),
            media=n.get("media_errors"),
            crit=n.get("critical_warning"),
            errlog=n.get("num_err_log_entries"),
        )
    s = j.get("scsi_error_counter_log")
    if s:
        d.update(
            grown=j.get("scsi_grown_defect_list"),
            unc=sum((s.get(k) or {}).get("total_uncorrected_errors", 0) for k in ("read", "write", "verify")),
        )
    return d


def scan_targets(scan: str, usb: set[str]) -> list[tuple[str, str, str]]:
    """smartctl --scan-open output -> [(device, -d type, label)], skipping USB disks and the controller's own entry."""
    out = []
    for line in scan.splitlines():
        m = re.match(r"^(\S+)\s+-d\s+(\S+)", line)
        if not m:
            continue
        dev, typ = m.groups()
        name = dev.rsplit("/", 1)[-1]
        if name in usb or typ.startswith("usb") or typ == "megaraid" or line.lstrip().startswith("#"):
            continue
        bay = re.search(r"megaraid,(\d+)", typ)  # megaraid,N or sat+megaraid,N: a drive behind the RAID controller
        label = f"bay {bay.group(1)}" if bay else name
        out.append((dev, typ, label))
    return out


def _usb_disks() -> set[str]:
    try:
        devs = json.loads(
            subprocess.run(["lsblk", "-J", "-dn", "-o", "NAME,TRAN"], capture_output=True, text=True, timeout=20).stdout
        )
        return {d["name"] for d in devs.get("blockdevices", []) if (d.get("tran") or "").lower() == "usb"}
    except Exception:
        return set()


def drives() -> list[dict]:
    try:
        scan = subprocess.run(["smartctl", "--scan-open"], capture_output=True, text=True, timeout=60).stdout
    except Exception as e:
        return [{"error": f"smartctl --scan-open: {e}"[:160]}]
    out = []
    for dev, typ, label in scan_targets(scan, _usb_disks()):
        try:
            r = subprocess.run(["smartctl", "-j", "-a", "-d", typ, dev], capture_output=True, text=True, timeout=60)
            out.append(smart_parse(json.loads(r.stdout or "{}"), label, r.returncode))
        except Exception as e:
            out.append({"dev": label, "error": str(e)[:160]})
    return out


def kernel_errors(dmesg: str, since: datetime) -> dict:
    """Kernel log lines matching I/O / medium / controller errors since `since`, counted per device (sdX, nvmeN, ataN)."""
    out = {}
    for line in dmesg.splitlines():
        ts, _, msg = line.partition(" ")
        try:
            if datetime.fromisoformat(ts.replace(",", ".")) < since:
                continue
        except ValueError:
            continue
        if KERR.search(msg):
            m = re.search(r"\b(sd[a-z]+|nvme\d+(?:n\d+)?|ata\d+(?:\.\d+)?)\b", msg)
            k = m.group(1) if m else "other"
            out[k] = out.get(k, 0) + 1
    return out


def snapshot() -> dict:
    try:
        dm = subprocess.run(["dmesg", "--time-format=iso", "-k"], capture_output=True, text=True, timeout=30).stdout
        kern = kernel_errors(dm, datetime.now(timezone.utc).astimezone() - timedelta(hours=1))
    except Exception:
        kern = None
    from .system import edac  # ECC counts sit with the other memory stats; repeated here so health is complete on its own

    return {"at": time.time(), "drives": drives(), "kernel": kern, "edac": edac(), "every_s": settings.health_every_s}
