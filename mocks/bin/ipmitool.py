#!/usr/bin/env python3
"""Mock ipmitool: canned answers in the formats a Dell BMC gives, and every write command logged instead of run.

Use it wherever a service takes an ipmitool command line, e.g.  SJC_IPMITOOL="python mocks/bin/ipmitool.py"
or IPMITOOL_CMD="python mocks/bin/ipmitool.py". Writes (raw fan commands, peak reset) go to MOCK_IPMI_LOG if set.
MOCK_IPMI_TEMP_CPU raises the CPU temperatures (to test Emergency handling).
"""

from __future__ import annotations

import os
import sys

CPU = float(os.environ.get("MOCK_IPMI_TEMP_CPU", "62"))
SDR = {
    "fan": """Fan1 RPM         | 30h | ok  |  7.1 | 4080 RPM
Fan2 RPM         | 31h | ok  |  7.1 | 4200 RPM
Fan3 RPM         | 32h | ok  |  7.1 | 4080 RPM
Fan Redundancy   | 75h | ok  |  7.1 | Fully Redundant
""",
    "temperature": f"""Inlet Temp       | 04h | ok  |  7.1 | 24 degrees C
Exhaust Temp     | 01h | ok  |  7.1 | 38 degrees C
Temp             | 0Eh | ok  |  3.1 | {CPU:.0f} degrees C
Temp             | 0Fh | ok  |  3.2 | {CPU - 3:.0f} degrees C
""",
    "voltage": """Voltage 1        | 6Ch | ok  | 10.1 | 230 Volts
Voltage 2        | 6Dh | ok  | 10.2 | 230 Volts
""",
    "current": """Current 1        | 6Ah | ok  | 10.1 | 0.80 Amps
Current 2        | 6Bh | ok  | 10.2 | 0.20 Amps
Pwr Consumption  | 77h | ok  |  7.1 | 252 Watts
""",
    "power supply": """Status           | 60h | ok  | 10.1 | Presence detected
Status           | 61h | ok  | 10.2 | Presence detected
""",
}
DCMI = """
    Instantaneous power reading:                   252 Watts
    Minimum during sampling period:                180 Watts
    Maximum during sampling period:                410 Watts
    Average power reading over sample period:      260 Watts
    IPMI timestamp:                           Wed Oct 28 21:00:00 2026
    Sampling period:                          00000001 Seconds.
    Power reading state is:                   activated
"""
POWERMONITOR = """Power Tracking Statistics
Statistic      : Cumulative Energy Consumption
Start Time     : Wed Oct 28 00:00:00 2026
Finish Time    : Wed Oct 28 21:00:00 2026
Reading        : 5.2 kWh

Statistic      : System Peak Power
Start Time     : Wed Oct 28 20:59:30 2026
Peak Time      : Wed Oct 28 20:59:47 2026
Peak Reading   : 431 W
"""


def log_write(args: list[str]) -> None:
    p = os.environ.get("MOCK_IPMI_LOG")
    if p:
        with open(p, "a") as f:
            f.write(" ".join(args) + "\n")


def main(argv: list[str]) -> int:
    a = list(argv)
    while a and a[0] in ("-I", "-H", "-U", "-P", "-S"):  # connection options and the SDR cache: ignored
        a = a[2:]
    if a[:2] == ["sdr", "type"] and len(a) > 2:
        sys.stdout.write(SDR.get(" ".join(a[2:]).lower(), ""))
    elif a[:2] == ["sdr", "dump"]:
        open(a[2], "w").close() if len(a) > 2 else None
    elif a[:3] == ["dcmi", "power", "reading"]:
        sys.stdout.write(DCMI)
    elif a[:2] == ["delloem", "powermonitor"] and len(a) == 2:
        sys.stdout.write(POWERMONITOR)
    elif a[:1] == ["raw"] or a[:3] == ["delloem", "powermonitor", "clear"] or a[:3] == ["dcmi", "power", "set_limit"]:
        log_write(a)
    elif a[:2] == ["sel", "info"]:
        sys.stdout.write("Entries          : 42\nPercent Used     : 7%\nLast Add Time    : 10/28/2026 20:00:00\n")
    else:
        sys.stderr.write(f"mock ipmitool: unsupported {' '.join(a)}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
