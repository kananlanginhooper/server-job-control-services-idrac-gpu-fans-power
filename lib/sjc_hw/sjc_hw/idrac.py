"""iDRAC sensors and fan control over ipmitool (ported from the dashboard's app.py and fan-control.py).

Readings parse `ipmitool sdr type <X>` lines:  name | id | status | entity | value
"""
from __future__ import annotations

import re
import time

from .config import settings
from .shell import run

CPU_ENTITY = {"3.1": "CPU1", "3.2": "CPU2"}   # Dell names both CPU sensors "Temp"; the entity id tells them apart


def ipmi(*args: str, timeout: float = 20):
    return run(settings.ipmitool + list(args), timeout=timeout)


def sdr(typ: str) -> str:
    """One SDR type, read through a cached SDR dump (much faster than a live scan)."""
    r = ipmi("-S", settings.sdr_cache, "sdr", "type", typ, timeout=15)
    if r.returncode != 0:   # no cache yet (or it went stale after a firmware update): rebuild it once
        ipmi("sdr", "dump", settings.sdr_cache, timeout=60)
        r = ipmi("-S", settings.sdr_cache, "sdr", "type", typ, timeout=15)
    return r.stdout


def _rows(txt: str):
    for line in txt.splitlines():
        f = [x.strip() for x in line.split("|")]
        if len(f) >= 5:
            yield f


def parse_fans(txt: str) -> tuple[list[dict], str | None]:
    fans, redundancy = [], None
    for name, _, st, _, val in (f[:5] for f in _rows(txt)):
        m = re.match(r"([\d.]+)\s*RPM", val)
        if m:
            fans.append({"name": name.replace(" RPM", ""), "rpm": float(m.group(1)), "ok": st == "ok"})
        elif "Redundan" in name:
            redundancy = val
    return fans, redundancy


def parse_temps(txt: str) -> list[dict]:
    out = []
    for name, _, st, ent, val in (f[:5] for f in _rows(txt)):
        m = re.match(r"([\d.]+)\s*degrees C", val)
        if m:
            label = CPU_ENTITY.get(ent, name) if name == "Temp" else name.replace(" Temp", "")
            out.append({"name": label, "c": float(m.group(1)), "ok": st == "ok"})
    return out


def parse_psu(volt: str, curr: str, supply: str) -> dict:
    out: dict[int, dict] = {1: {}, 2: {}}
    for txt in (volt, curr):
        for f in _rows(txt):
            m = re.match(r"(Voltage|Current) ([12])$", f[0])
            v = re.match(r"([\d.]+)\s*(Volts|Amps)", f[4])
            if m:
                out[int(m.group(2))]["v" if m.group(1) == "Voltage" else "a"] = float(v.group(1)) if v else None
    n = 0
    for f in _rows(supply):
        if f[0] == "Status":
            n += 1
            if n in out:
                out[n]["status"] = f[4]
    return {f"ps{k}": d for k, d in out.items()}


def parse_dcmi(txt: str) -> dict | None:
    grab = lambda k: (lambda m: float(m.group(1)) if m else None)(re.search(k + r"[^:]*:\s*([\d.]+)\s*Watts", txt))
    w = grab("Instantaneous power reading")
    if w is None:
        return None
    return {"t": time.time(), "w": w, "avg": grab("Average power reading"), "min": grab("Minimum during"), "max": grab("Maximum during")}


def sensors() -> dict:
    """Fans, temperatures, PSUs and the server's power draw, as one snapshot."""
    out: dict = {"at": time.time()}
    try:
        out["fans"], out["redundancy"] = parse_fans(sdr("Fan"))
        out["temps"] = parse_temps(sdr("Temperature"))
        out["psu"] = parse_psu(sdr("Voltage"), sdr("Current"), sdr("Power Supply"))
        out["power_w"] = next((float(m.group(1)) for f in _rows(sdr("Current"))
                               if (m := re.match(r"([\d.]+)\s*Watts", f[4]))), None)
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


def dcmi() -> dict | None:
    try:
        return parse_dcmi(ipmi("dcmi", "power", "reading", timeout=5).stdout)
    except Exception:
        return None


def sel_info() -> dict:
    r = ipmi("sel", "info", timeout=30).stdout
    grab = lambda k: (lambda m: m.group(1).strip() if m else None)(re.search(rf"^{k}\s*:\s*(.+)$", r, re.M))
    pct = grab("Percent Used")
    return {"entries": grab("Entries"), "percent_used": int(pct.rstrip("%")) if pct and pct.rstrip("%").isdigit() else None,
            "last_add": grab("Last Add Time")}


# --- control: only when fan-control isn't running (see main.py) -------------------------------------------------

def set_fan_pct(pct: int) -> None:
    """Manual fan speed for every fan. Dell raw commands: 0x30 0x30 0x01 0x00 = manual, 0x30 0x30 0x02 0xff <pct>."""
    if ipmi("raw", "0x30", "0x30", "0x01", "0x00").returncode or ipmi("raw", "0x30", "0x30", "0x02", "0xff", f"0x{pct:02x}").returncode:
        raise RuntimeError("ipmitool fan command failed")


def dell_auto() -> None:
    """Hand the fans back to the iDRAC's own curve (what fan-control does when it stops)."""
    if ipmi("raw", "0x30", "0x30", "0x01", "0x01").returncode:
        raise RuntimeError("ipmitool dell-auto command failed")
