#!/usr/bin/env python3
# Thermal controller for the hub and GPU server, one process per server so nothing fights over the fans.
# Fixed modes, no fan curves (2026-09-24): Quick Cool > Emergency > Run Mode > Normal, each a fixed speed.
# Server fans via Dell IPMI; on the GPU server also the RTX 2080 Ti card fan and power limit via NVML.
# Falls back to Dell automatic fans, the card's automatic fan and the fixed power limit on any failure or on exit.
from __future__ import annotations

import csv, ctypes as C, glob, json, math, os, re, shutil, signal, socket, subprocess, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "lib"))
from svcenv import env  # noqa: E402  (shared repo .env; see .env.example)

TICK, DRIVE_EVERY, RPM_EVERY, ADJ_EVERY = 5, 300, 30, 20
NORMAL_RPM, RUN_RPM, EMERG_FAN = (
    4000,
    5500,
    80,
)  # server fan targets (the owner): Normal 4,000 RPM, Run Mode 5,500 RPM, Emergency 80% (was 100; 2026-09-26)
FAN_CAL = [
    (15, 3600),
    (22, 5190),
    (24, 5560),
    (27, 5940),
    (33, 6920),
    (40, 8000),
    (50, 9540),
]  # hub (pct, RPM): 22-50 measured, 15 estimated; the GPU server table is set below
EMERGENCY = {  # kind: (enter C, clear C); any sensor at enter puts the server in Emergency
    "ssd": (70, 63),  # WD Red SA500, rated 70C (2026-09-24)
    "nvme": (80, 70),  # hotter of composite and hotspot; Samsung 990 PRO warning 82 (composite)
    "toshiba": (48, 44),  # MG03SCA400 trip temperature is 50C
    "hdd": (58, 52),  # WD181KRYZ
    "cpu": (90, 80),  # (2026-09-24)
    "exhaust": (50, 45),
    "gpu": (90, 87),  # RTX 2080 Ti max operating 89, slowdown 91
    "raid": (97, 95),  # PERC ROC (LSI SAS3108 / SAS2208): designed to run < 105C, throttles at 110
}
# Site settings come from the environment (EnvironmentFile in the unit; see .env.example at the repo root).
ROLE = env("ROLE", "hub")  # "hub" (dashboard host, DCMI cap) or "gpu-server" (RAPL caps + GPU card)
HOST_KEY = env("HOST_KEY", ROLE)  # this server's key in the dashboard's per-host JSON
DASH_URL = env("HUB_API_URL", "http://localhost:8090")
GPU_THROTTLE_KEY = env("GPU_THROTTLE_KEY", "gpu-server-gpu")
LIMITS_FILE = env("LIMITS_FILE", "/usr/local/etc/component-limits.json")
_limits_mtime = None
GPU_AIM = [
    83.0
]  # GPU server GPU control aim (limits "aim"): below the card's 84C target, where its firmware throttles and spikes the fan (2026-09-27)
DISPLAY_ONLY = {"cpu", "inlet"}  # kinds shown on the dashboard but never used to set fan speed (limits "fans": false; 2026-09-28)
AIM = {}  # per-kind explicit fan aim from the limits file ("aim"); else yellow - EQ_EARLY
YELLOW = {}  # per-kind yellow (max run) for this host, from the limits file
NO_EMERG = (
    set()
)  # kinds with no Emergency (v2: the CPU throttles itself; 2026-09-27); their EMERGENCY entry is the Adaptive "tune" limit


def load_limits() -> (
    None
):  # shared with the dashboard (2026-09-26); the table above stays as the fallback if the file is missing or bad
    global _limits_mtime
    try:
        m = os.path.getmtime(LIMITS_FILE)
    except OSError:
        return
    if m == _limits_mtime:
        return
    _limits_mtime = m
    try:
        with open(LIMITS_FILE) as f:
            j = json.load(f)
        over = (j.get("hosts") or {}).get(HOST_KEY) or {}
        new, noem, yel = {}, set(), {}
        for k, v in j["kinds"].items():
            v = dict(v, **(over.get(k) or {}))
            if v.get("yellow") is not None:
                yel[k] = float(v["yellow"])
            e = v.get("emergency")
            if e:
                new[k] = (float(e[0]), float(e[1]))
            elif v.get("tune"):
                new[k] = (float(v["tune"][0]), float(v["tune"][1]))
                noem.add(k)
        if set(EMERGENCY) - set(new) or any(c >= e for e, c in new.values()):
            raise ValueError(f"missing kinds {sorted(set(EMERGENCY) - set(new))} or clear >= enter")
        if new != {k: EMERGENCY[k] for k in new} or noem != NO_EMERG:
            log(f"limits: {LIMITS_FILE} v{j.get('version')} table {new}; no Emergency (tune only): {sorted(noem)}")
        EMERGENCY.update(new)
        NO_EMERG.clear()
        NO_EMERG.update(noem)
        YELLOW.clear()
        YELLOW.update(yel)
        disp = {k for k, v in j["kinds"].items() if dict(v, **(over.get(k) or {})).get("fans") is False}
        if disp != DISPLAY_ONLY:
            log(f"limits: display only (never set fan speed): {sorted(disp)}")
        DISPLAY_ONLY.clear()
        DISPLAY_ONLY.update(disp)
        AIM.clear()
        AIM.update(
            {
                k: float(dict(v, **(over.get(k) or {}))["aim"])
                for k, v in j["kinds"].items()
                if k != "gpu" and dict(v, **(over.get(k) or {})).get("aim") is not None
            }
        )
        ga = dict(j["kinds"].get("gpu") or {}, **(over.get("gpu") or {})).get("aim")
        GPU_AIM[0] = float(ga) if ga is not None else 83.0
    except Exception as ex:
        log(f"limits: {LIMITS_FILE} rejected ({ex}); keeping the current table")


QUICK_COOL, QUICK_COOL_STALE = (
    "/dev/shm/quick-cool.json",
    60,
)  # written by the dashboard (app.py on the hub, agent on the GPU server)

# GPU server GPU (RTX 2080 Ti: target 84C, max operating 89, slowdown 91, shutdown 94). Settings are the owner's, 2026-09-24.
CARD_FAN, CARD_FAN_HOT, CARD_HOT_ON, CARD_HOT_OFF = 85, 100, 89, 87
CARD_FAN_WARM, CARD_WARM_ON, CARD_WARM_OFF = 90, 87, 85  # card fan 90% from 87C until back at 85C (2026-09-24)
CARD_FAN_IDLE = 75  # card fan when the GPU is idle; None = the card's automatic curve
PL_LOW = 100  # GPU W in Low Power/Meeting (2026-09-28: 150 -> 125 -> 100, the card minimum; combined 600 W target)
RAPL_LOW, RAPL_HIGH = 60, 135  # low/high mode markers for apply_rapl
DCMI_LOW, DCMI_HIGH = (
    280,
    550,
)  # hub whole-server cap (W): low modes / normal (2026-09-29: 550; briefly 650, before that 517; hub runs on one 750 W PSU). 2026-09-28: 165 was tried and reverted: a binding cap makes the Intel ME fire GPE 0x24 about once a second, and the BIOS ACPI handler errors (\_SB.PRAD not found)
DRAM_LOW_W = 20  # GPU server per-socket memory RAPL cap in low modes (drew ~31 W each); uncapped otherwise (2026-09-28)
PL_WARM, PL_WARM_INLET_ON, PL_WARM_INLET_OFF = (
    210,
    28,
    27,
)  # Adaptive GPU cap while the inlet is warm (2026-09-27: 200; 2026-09-28: "230 overrides the warm-room rule", so = the 230 W base)
PL_FIXED, PL_EMERG = (
    185,
    150,
)  # hand-back limit; GPU emergency drops straight to 150 W (the owner), back to the profile limit on clear
BUSY_W, BUSY_UTIL, IDLE_S = 90, 30, 120  # GPU busy: util % or watts; Normal after IDLE_S with no work
CPU_BUSY, DISK_BUSY = 20, 25  # Run Mode while any work: CPU % of all cores, or any disk/volume busy %

GPU_SERVER = ROLE == "gpu-server"
if GPU_SERVER:  # GPU server disks are recovery evidence: run from RAM, write nothing to disk
    IPMITOOL, SMARTCTL = "/dev/shm/tools/ipmitool", "/dev/shm/tools/smartctl"
    ENV = dict(os.environ, LD_LIBRARY_PATH="/dev/shm/tools/lib")
    LOG, STATE, SDR = "/dev/shm/fan-control.log", "/dev/shm/fan-control.json", "/dev/shm/fan-control-sdr.cache"
    TRACE = "/dev/shm/gpu-run-trace.csv"
    EMERGENCY["exhaust"] = (55, 50)  # the GPU server runs a warmer exhaust with the GPU
    PROFILE_FILE = "/dev/shm/run-profile.json"
    FAN_CAL = [(20, 3840), (29, 4980), (33, 5610), (36, 5940), (43, 6950), (50, 7920), (58, 8960), (65, 9900), (70, 10680)]
else:
    IPMITOOL, SMARTCTL, ENV = "ipmitool", "smartctl", None
    LOG, STATE, SDR, TRACE = "/var/log/fan-control.log", "/run/fan-control.json", "/run/fan-control-sdr.cache", None
    PROFILE_FILE = "/run/run-profile.json"
PROFILES = {  # Run Mode profiles (2026-09-24); pick one by writing {"profile": "<name>"} to PROFILE_FILE
    "daytime": {"label": "Daytime Run Mode", "run_rpm": 5800 if GPU_SERVER else 5000, "card_fan": 85, "pl": 188},
    "unattended": {
        "label": "Unattended Run Mode",
        "run_rpm": 9000,
        "card_fan": 100,
        "pl": 240,
    },  # 2026-09-26 09:55: reverted to 9000 both after the hub ran hot at 7000 (emergency cpu0 90C 09:43)
}
PROFILES["adaptive"] = {
    "label": "Adaptive Run Mode",
    "run_rpm": 7000 if GPU_SERVER else 6750,
    "floor_rpm": NORMAL_RPM,  # 4,000 on both (2026-09-27 18:45: the GPU no longer drives the case fans and is at 200 W in a warm room, so the GPU server 5,800 floor came off)
    "card_fan": 100,
    "card_floor": 75,
    "pl": 210,
}  # 210 W (2026-09-29: back from the 230 W trial, which ran the GPU at 88-89C with firmware fan spikes)   # 2026-09-26: target (center) GPU server 7000 / hub 6750, ceiling = Unattended (temperature only), floor = Daytime; card fan 100 while the GPU is busy, 75 floor idle only; 210 W ("there isn't much performance increase from 240 W")
# Adaptive (2026-09-26): start at 9k, step down 500 RPM at a time with stabilize + test periods, never below Daytime;
# small rise in a test -> back up one step and hold; large rise -> straight back to max. Timings/margins are constants below.
# Adaptive v2 (2026-09-26: "speed up the adaptive checks... not hunting but respond to rapid load changes"):
ADAPT_EWMA_TAU, ADAPT_SLOPE_WIN = 30, 60  # per-sensor EWMA time constant (s); slope over the last 60 s of the EWMA (C/min)
ADAPT_SETTLE_SLOPE, ADAPT_SETTLE_MIN, ADAPT_SETTLE_MAX, ADAPT_TEST = (
    0.3,
    60,
    240,
    120,
)  # settle until |slope| < 0.3 C/min (60-240 s), then test 120 s
ADAPT_BIG_HEADROOM, ADAPT_MID_HEADROOM, ADAPT_MARGIN = (
    15,
    6,
    2,
)  # headroom to enter: >=15C big, 6-15 small, 2-6 tiny, <2 none (the owner: margin 1-2C)
ADAPT_STEP, ADAPT_STEP_BIG, ADAPT_STEP_TINY = 500, 1000, 250  # case fans, RPM
ADAPT_CARD_STEP, ADAPT_CARD_STEP_BIG, ADAPT_CARD_STEP_TINY = 5, 10, 5  # GPU server card fan, %
ADAPT_DOWN_STEP_RPM = 1000  # 2026-09-26 10:00: back to 1000 RPM steps with the old timing (250 steps made the fans spike)
ADAPT_PACE = (1.0, 1.0, 1.0)  # old timing at every headroom
ADAPT_LOOKAHEAD = 180  # step down only if every sensor's EWMA + slope * 3 min stays < enter - 2 (GPU: <= 88)
ADAPT_UP_MARGIN, ADAPT_UP_FAST_SLOPE = (
    1,
    1.0,
)  # projection crossing enter - 1 -> +500 (+1000 if slope > 1 C/min); above the step-down gate
ADAPT_JUMP_RISE, ADAPT_JUMP_WINDOW, ADAPT_NEAR = (
    5,
    60,
    1,
)  # within 1C of enter -> max; the 60 s window feeds the intercept (ADAPT_JUMP_RISE unused since 2026-09-27)
ADAPT_INTERCEPT_MIN_SLOPE = 0.5  # C/min; slower rises are left to the normal settle/test steps
ADAPT_INTERCEPT_GAIN, ADAPT_INTERCEPT_FAST = (
    1.0,
    30,
)  # C/min of cooling per +1,000 RPM (initial guess; to be fitted from the corpus); limit < 30 s away -> ceiling
FAN_RAMP_RPM, FAN_RAMP_S = 500, 15  # 2026-09-26: adaptive case fans move 500 RPM per 15 s; instant within ADAPT_NEAR of emergency
ADAPT_LOAD_JUMP, ADAPT_LOAD_WIN, ADAPT_LOAD_DROP_HOLD, ADAPT_LOAD_KICK = 20, 30, 60, 1000  # load feed-forward
ADAPT_REVERT_WIN, ADAPT_BLOCK_BASE, ADAPT_BLOCK_MAX, ADAPT_BLOCK_RESET = 300, 900, 3600, 7200  # anti-hunt backoff
ADAPT_GPU_CASE_LIMIT = (
    90  # fallback only: the GPU yellow from component-limits.json is used (2026-09-27: 90C is correct for the GPU)
)
ADAPT_GPU_FLOOR_S, ADAPT_LOAD_CHANGE = (
    3600,
    0.20,
)  # learned GPU-limited case floor: 60 min, cleared early if GPU util/draw moves 20%+
ADAPT_GPU_BUSY_AVG, ADAPT_GPU_IDLE_S = 30, 30  # GPU busy judged on a 30 s average; 30 s not busy before the card may adapt
PROFILES["lowpower"] = dict(
    PROFILES["adaptive"], label="Low Power Mode", pl=PL_LOW, run_rpm=NORMAL_RPM, floor_rpm=3000, ceil_rpm=7000
)  # ceil 7,000 RPM (2026-09-30: "adjust fans for low power mode to not allow for fans over 7000 rpm"); Emergency stays exempt   # 2026-09-28: same algorithm as Adaptive, target 4,000 instead of 7,000, floor 3,000   # 2026-09-28: Adaptive fans + low power caps (GPU 125, GPU server CPU 55/pkg, hub DCMI 280)
PROFILES["normal"] = {
    "label": "Normal",
    "run_rpm": NORMAL_RPM,
    "card_fan": CARD_FAN_IDLE,
    "pl": PROFILES["daytime"]["pl"],
}  # stay at Normal settings even while working (the owner, via dashboard)


def run_profile() -> tuple[str, dict]:
    """Returns (name, settings). The file may override run_rpm, card_fan or pl for tuning, e.g. {"profile": "unattended", "run_rpm": 8000}."""
    try:
        f = json.load(open(PROFILE_FILE))
        name = f.get("profile", "daytime") if f.get("profile") in PROFILES else "daytime"
        return name, dict(
            PROFILES[name], **{k: f[k] for k in ("run_rpm", "card_fan", "pl") if isinstance(f.get(k), (int, float))}
        )
    except Exception:
        return "daytime", PROFILES["daytime"]


def pct_for(rpm: float) -> int:
    pts = sorted(FAN_CAL)
    for (p0, r0), (p1, r1) in zip(pts, pts[1:]):
        if rpm <= r1 or (p1, r1) == pts[-1]:
            return max(10, min(100, round(p0 + (p1 - p0) * (rpm - r0) / (r1 - r0))))


NORMAL_FAN, RUN_FAN = pct_for(NORMAL_RPM), pct_for(RUN_RPM)


def rpm_for(pct: float) -> int:  # inverse of pct_for: the RPM a fan % gives, from FAN_CAL
    pts = sorted(FAN_CAL)
    for (p0, r0), (p1, r1) in zip(pts, pts[1:]):
        if pct <= p1 or (p1, r1) == pts[-1]:
            return round(r0 + (r1 - r0) * (pct - p0) / (p1 - p0))


EVENTS, EVENT_KEEP = (
    [],
    40,
)  # recent fan decisions for the dashboard's "why the fans changed" list (2026-09-27); streamed via the state file
EVENT_RE = re.compile(
    r"^(adaptive: \d|adaptive: level|EMERGENCY|emergency cleared|run profile|power limit|quick cool|note:|started|limits:|card)"
)
MODE_RE = re.compile(r"^mode (\S+) -> ([^:\s]+)")


def log(msg: str) -> None:
    m = MODE_RE.match(msg)
    if EVENT_RE.match(msg) or (
        m and m.group(1) != m.group(2)
    ):  # mode lines only when the mode itself changes, not every ramp step
        EVENTS.append([round(time.time()), msg[:300]])
        del EVENTS[:-EVENT_KEEP]
    with open(LOG, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")


def ipmi(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([IPMITOOL, *args], capture_output=True, text=True, timeout=20, env=ENV)


def sdr(*args: str) -> str:
    if not os.path.exists(SDR):
        ipmi("sdr", "dump", SDR)
    return ipmi("-S", SDR, "sdr", *args).stdout


def dell_auto() -> None:
    ipmi("raw", "0x30", "0x30", "0x01", "0x01")


def set_fan(pct: int) -> None:
    if (
        ipmi("raw", "0x30", "0x30", "0x01", "0x00").returncode
        or ipmi("raw", "0x30", "0x30", "0x02", "0xff", f"0x{pct:02x}").returncode
    ):
        raise RuntimeError("ipmitool fan command failed")


def cpu_temps() -> dict:
    out = {}
    for h in glob.glob("/sys/class/hwmon/hwmon*"):
        if open(f"{h}/name").read().strip() != "coretemp":
            continue
        vals = [int(open(f).read()) / 1000 for f in glob.glob(f"{h}/temp*_input")]
        if vals:
            out[f"cpu{len(out)}"] = max(vals)
    if not out:
        raise RuntimeError("no coretemp sensors")
    return out


def nvme_temps() -> dict:
    out = {}
    for h in glob.glob("/sys/class/nvme/nvme*/device/hwmon/hwmon*") + glob.glob("/sys/class/nvme/nvme*/hwmon*"):
        name = "/dev/" + h.split("/sys/class/nvme/")[1].split("/")[0]
        vals = [int(open(f).read()) / 1000 for f in glob.glob(f"{h}/temp*_input")]
        if vals:
            out[name] = max(out.get(name, 0), max(vals))
    return out


def exhaust_temp() -> int | None:
    m = re.search(r"Exhaust Temp\s*\|.*\|\s*(\d+) degrees", sdr("type", "Temperature"))
    return int(m.group(1)) if m else None


def inlet_temp() -> int | None:
    m = re.search(r"Inlet Temp\s*\|.*\|\s*(\d+) degrees", sdr("type", "Temperature"))
    return int(m.group(1)) if m else None


def fan_rpm() -> int | None:
    m = re.search(r"Fan1 RPM\s*\|.*\|\s*(\d+) RPM", sdr("type", "Fan"))
    return int(m.group(1)) if m else None


def smart(args: list[str]) -> str:
    try:
        return subprocess.run([SMARTCTL, *args], capture_output=True, text=True, timeout=20).stdout
    except subprocess.TimeoutExpired:
        return ""


def temp_of(txt: str) -> int | None:
    m = (
        re.search(r"Current Drive Temperature:\s+(\d+)", txt)
        or re.search(r"Temperature_Celsius.*?(\d+)(?:\s+\(|\s*$)", txt, re.M)
        or re.search(r"^Temperature:\s+(\d+)", txt, re.M)
    )
    return int(m.group(1)) if m else None


PERCCLI = env("PERCCLI", "/usr/local/sbin/perccli64")
_raid = {"t": None, "at": 0, "next": 0}


def raid_temp() -> (
    float | None
):  # PERC ROC temperature (2026-09-27, after the hub H730 fault): every 60 s, back off 5 min on failure
    now = time.time()
    if now < _raid["next"]:
        return _raid["t"] if now - _raid["at"] < 300 else None
    try:
        out = subprocess.run([PERCCLI, "/c0", "show", "all"], capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"ROC temperature\(Degree Celsius\)\s*=\s*(\d+)", out)
        _raid.update(t=int(m.group(1)) if m else None, at=now, next=now + 60)
    except Exception:
        _raid.update(next=now + 300)
    return _raid["t"] if now - _raid["at"] < 300 else None


def drive_temps() -> dict:
    out = {}
    if GPU_SERVER:
        for n in re.findall(r"-d megaraid,(\d+)", smart(["--scan"])):
            txt = smart(["-a", "-d", f"megaraid,{n}", "/dev/bus/0"])
            t = temp_of(txt)
            if t is not None:
                out[f"bay{n}"] = (t, "toshiba" if "MG03SCA" in txt else "hdd")
    else:
        for d in sorted(glob.glob("/dev/sd[a-z]")):
            t = temp_of(smart(["-A", d]))
            if t is not None:
                out[d] = (t, "ssd")
    return out


class Activity:
    """Run Mode is the default while the server works (2026-09-24): CPU, disk or GPU busy; Normal only when idle."""

    def __init__(self) -> None:
        self.cpu, self.disk, self.t = self._cpu(), self._disk(), time.time()

    @staticmethod
    def _cpu() -> tuple[int, int]:
        v = [int(x) for x in open("/proc/stat").readline().split()[1:]]
        return sum(v), v[3] + v[4]

    @staticmethod
    def _disk() -> dict:
        out = {}
        for line in open("/proc/diskstats"):
            f = line.split()
            if re.match(r"(sd[a-z]+|nvme\d+n\d+|dm-\d+|md\d+)$", f[2]):
                out[f[2]] = int(f[12])
        return out

    def sample(self) -> str:
        cpu, disk, t = self._cpu(), self._disk(), time.time()
        dt = max(0.1, t - self.t)
        busy = 100 * (1 - (cpu[1] - self.cpu[1]) / max(1, cpu[0] - self.cpu[0]))
        util = {d: 100 * (disk[d] - self.disk.get(d, disk[d])) / (dt * 1000) for d in disk}
        self.cpu, self.disk, self.t = cpu, disk, t
        top = max(util.items(), key=lambda kv: kv[1], default=(None, 0))
        self.load = {"cpu": busy, "disk": min(100, top[1])}
        why = []
        if busy >= CPU_BUSY:
            why.append(f"CPU {busy:.0f}%")
        if top[1] >= DISK_BUSY:
            why.append(f"{top[0]} {min(100, top[1]):.0f}% busy")
        return why


class Smooth:
    """Per-sensor EWMA (tau ADAPT_EWMA_TAU) and slope in C/min over the last ADAPT_SLOPE_WIN seconds of the EWMA."""

    def __init__(self) -> None:
        self.v, self.hist, self.t = {}, {}, None

    def update(self, now: float, sensors: dict) -> None:
        dt = 0 if self.t is None else now - self.t
        a = 1 - math.exp(-dt / ADAPT_EWMA_TAU) if dt else 1
        for k, (t, _) in sensors.items():
            self.v[k] = t if k not in self.v else self.v[k] + a * (t - self.v[k])
            h = self.hist.setdefault(k, [])
            h.append((now, self.v[k]))
            while h and now - h[0][0] > 600:  # 10 min kept: slow sensors need a longer slope window (Equilibrium)
                h.pop(0)
        self.t = now

    def slope(self, k: str, win: float = ADAPT_SLOPE_WIN) -> float:
        h = [p for p in (self.hist.get(k) or []) if p[0] >= (self.hist[k][-1][0] - win)] if self.hist.get(k) else []
        return 0.0 if len(h) < 2 or h[-1][0] - h[0][0] < 30 else 60 * (h[-1][1] - h[0][1]) / (h[-1][0] - h[0][0])

    def proj(self, k: str) -> float:
        return self.v[k] + max(0.0, self.slope(k)) * ADAPT_LOOKAHEAD / 60


EQ_TAU = {
    "raid": 120,
    "cpu": 45,
    "gpu": 90,
    "exhaust": 120,
    "nvme": 150,
    "ssd": 300,
    "hdd": 600,
    "toshiba": 600,
}  # s, thermal time constants (guesses; fit from the corpus)
EQ_EARLY = {
    "cpu": 2
}  # aim this many C under yellow: CPUs react 2C early ("carryover is a real thing", 2026-09-27; OK 2026-09-28)
EQ_N_KIND = {
    "cpu": 0.3
}  # CPU temps respond weakly to case airflow: a much steeper RPM response per degree (was the 0.8 air law: +250 steps while the hub CPU sat at 98-100C)
EQ_BAND_LOW, EQ_BAND_HIGH = 1.5, 0.5  # hold while the worst settle point is within aim - 1.5 .. aim + 0.5 C
EQ_N, EQ_DWELL, EQ_DEADBAND, EQ_MAX_DOWN = (
    0.8,
    60,
    250,
    1000,
)  # (T - inlet) ~ RPM^-0.8; wait after the fans arrive; ignore < 250; cap one decrease


class Equilibrium:
    """Adaptive Run Mode v3 case-fan logic (2026-09-27): temperature is the RESULT of fan speed and load and settles
    at a stable point, so don't chase slopes. Per sensor: settle point Tss = T + tau * slope; (Tss - inlet) scales as
    RPM^-0.8, so the RPM that puts Tss at the aim (the sensor's yellow; 1.5C under Emergency at most) is
    rpm * ((Tss - inlet) / (aim - inlet)) ** (1 / 0.8). Take the highest over the sensors, set it, wait for the fans to get
    there plus 60 s, re-check. The hottest sensor should then sit at yellow: the lowest RPM that keeps everything in spec."""

    def __init__(self, name: str = "adaptive") -> None:
        self.name, self.unit, self.sm = name, "RPM", Smooth()
        self.reset()

    def reset(self) -> None:
        self.target, self.phase, self.ends, self.phase_start, self.last = None, None, 0, 0, {}
        self.projection, self.down_at, self.down_from, self.blocked, self.settle_reason, self.step_size = (
            None,
            0,
            None,
            {},
            None,
            None,
        )

    def _set(self, now: float, to: float, phase: str, why: str) -> None:
        if to != self.target:
            log(f"{self.name}: {self.target} -> {to} {self.unit} ({why}); {phase}")
            if self.target is not None and to < self.target:
                self.down_at, self.down_from = now, self.target
            self.last = {"at": now, "from": self.target, "to": to, "why": why}
            self.step_size = abs(to - self.target) if self.target is not None else None
        ramp = abs(to - (self.target or to)) / FAN_RAMP_RPM * FAN_RAMP_S
        self.target, self.phase, self.phase_start, self.ends = to, phase, now, now + ramp + EQ_DWELL

    def force_max(self, now: float, max_v: float, why: str) -> None:
        if self.target is not None and self.target != max_v:
            self._set(now, max_v, "settle", why)

    def up(self, now: float, max_v: float, amount: float, why: str) -> None:
        if self.target is not None and self.target < max_v:
            self._set(now, min(max_v, self.target + amount), "settle", why)

    def tick(
        self,
        now: float,
        sensors: dict,
        max_v: float,
        floor_v: float,
        margin_keys: list[str] | None = None,
        jump_keys: list[str] | None = None,
        gpu: tuple | None = None,
        load: dict | None = None,
        ceil_v: float | None = None,
    ) -> float:
        self.sm.update(now, sensors)
        ceil = max(max_v, ceil_v or max_v)
        mk = [
            k for k in (margin_keys if margin_keys is not None else sensors) if k in sensors and sensors[k][1] not in DISPLAY_ONLY
        ]
        enter = {k: EMERGENCY[sensors[k][1]][0] for k in sensors}
        if (
            self.target is None
        ):  # 2026-09-27: start from the fans' current speed, not the center (no jump without a thermal reason)
            start = int(max(floor_v, min(ceil, round((getattr(self, "actual", None) or max_v) / 250) * 250)))
            self._set(now, start, "settle", "start from the current fan speed")
            return self.target
        self.target = max(floor_v, min(ceil, self.target))
        # 2026-09-27: no "near Emergency -> ceiling" here. Emergency (yellow + 2) is its own category, handled by the
        # Emergency path; Adaptive only aims at yellow (the target) and raises by calculation.
        tin = inlet_temp() or 27
        base = getattr(self, "actual", None) or self.target  # the speed the fans are really at (ramp), not the last target
        aim = lambda k: (
            AIM[sensors[k][1]]
            if sensors[k][1] in AIM
            else YELLOW.get(sensors[k][1], enter[k] - 2) - EQ_EARLY.get(sensors[k][1], 0)
        )
        nexp = lambda k: 1 / EQ_N_KIND.get(sensors[k][1], EQ_N)
        need, why = floor_v, "all settle points at or under their aim"
        worst = None
        for k in mk:
            tau = EQ_TAU.get(sensors[k][1], 120)
            T, s = (
                self.sm.v[k],
                self.sm.slope(k, max(ADAPT_SLOPE_WIN, min(300, tau / 2))),
            )  # longer window for slow sensors: whole-degree readings
            tss = max(T - 8, min(T + 15, T + s / 60 * tau))
            a = aim(k)
            r = floor_v if tss - tin <= 1 else base * ((tss - tin) / max(0.5, a - tin)) ** nexp(k)
            if worst is None or r > worst[1]:
                worst = (k, r, T, tss, a)
        if worst:
            k, r, T, tss, a = worst
            need = r
            why = f"equilibrium: {k} {T:.0f}C settling at {tss:.0f}C vs aim {a:.0f}C"
            self.projection = {"sensor": k, "c": round(tss, 1), "limit": round(a, 1)}
        if gpu and gpu[0] in sensors:  # GPU server: the case fans barely cool the GPU; it may only stop a decrease
            gk = gpu[0]
            gtss = self.sm.v[gk] + self.sm.slope(gk) / 60 * EQ_TAU["gpu"]
            if gtss > gpu[1] + EQ_BAND_HIGH:
                need, why = (
                    max(need, base + 500),
                    f"GPU card fan at 100%, GPU settling at {gtss:.0f}C (aim {gpu[1]:.0f}C): case fans help",
                )
            elif gtss >= gpu[1] - EQ_BAND_LOW and need < self.target:
                need, why = self.target, f"hold: card fan at 100%, GPU settling at {gtss:.0f}C (aim {gpu[1]:.0f}C)"
        # settling in red = the sensor's YELLOW + 1, not aim + 1 (2026-09-29: GPU server bay HDDs aim 47 but yellow 56, so a 48C
        # projection skipped the settle wait every 30 s and walked the fans 3000 -> 7000 RPM in 4 min, the owner: "moving up too quickly")
        rising_to_red = (
            worst and worst[3] >= YELLOW.get(sensors[worst[0]][1], worst[4]) + 1 and now - getattr(self, "urgent_at", 0) >= 30
        )
        if now < self.ends and not rising_to_red:
            return self.target  # the last change is still taking effect
        if rising_to_red:
            self.urgent_at = now
        # hold band: readings are whole degrees, so only move when the worst settle point is clearly outside
        # [aim - EQ_BAND_LOW, aim + EQ_BAND_HIGH]; then aim for the middle of the band
        if worst and worst[4] - EQ_BAND_LOW <= worst[3] <= worst[4] + EQ_BAND_HIGH:
            self.phase, self.ends, self.settle_reason = "hold", now + 30, why + " (inside the hold band)"
            return self.target
        if worst:
            k, r, T, tss, a = worst
            mid = a - (EQ_BAND_LOW - EQ_BAND_HIGH) / 2
            need = floor_v if tss - tin <= 1 else base * ((tss - tin) / max(0.5, mid - tin)) ** nexp(k)
            if gpu and gpu[0] in sensors and why.startswith("hold:"):
                need = max(need, self.target)
            if gpu and gpu[0] in sensors and why.startswith("GPU card fan at 100%"):
                need = max(need, base + 500)  # keep the GPU raise (it was overwritten here before 2026-09-27 17:15)
        new = int(max(floor_v, min(ceil, math.ceil(need / 250) * 250)))
        if new < self.target:
            new = max(new, self.target - EQ_MAX_DOWN)
            if worst and now - self.phase_start < max(EQ_DWELL, 0.5 * EQ_TAU.get(sensors[worst[0]][1], 120)):
                return self.target  # a slow sensor hasn't shown where it settles yet: no decrease on its word
        if abs(new - self.target) < EQ_DEADBAND:
            self.phase, self.ends, self.settle_reason = "hold", now + 30, why
            return self.target
        if (
            new > self.target
            and worst
            and not rising_to_red
            and (self.last or {}).get("to") == self.target
            and (self.last.get("from") or 0) < self.target
            and now - self.last.get("at", 0) < EQ_TAU.get(sensors[worst[0]][1], 120) / 4
            and worst[2] < worst[4] + 1
        ):
            # slow sensor (HDD tau 600 s): after a raise, give it tau/4 to answer before raising again, unless its actual
            # reading (not the projection) is already over aim + 1 (2026-09-29)
            self.phase, self.ends, self.settle_reason = "hold", now + 30, why + " (waiting for the last raise to take effect)"
            return self.target
        self.settle_reason = why
        self._set(now, new, "settle", why)
        return self.target

    def state(self, max_v: float, floor_v: float) -> dict:
        return {
            "target_rpm": self.target,
            "phase": self.phase,
            "phase_ends": self.ends,
            "floor_rpm": floor_v,
            "max_rpm": max_v,
            "last_change": self.last,
            "settle_reason": self.settle_reason,
            "step_size": self.step_size,
            "projection": self.projection,
            "pace": 1.0,
            "blocked": {},
        }


GPU_CASE_KICK = False  # 2026-09-27 18:30: the case fans barely move the GPU (its blower at 100% does; it self-limits at 87-88C), so the GPU no longer moves the case fans
CARD_BUSY_PCT, CARD_SPIKE_C, CARD_SPIKE_HOLD = (
    100,
    90,
    300,
)  # 2026-09-27: 100% while busy (a 90% test throttled at 88C: 1,230 vs 1,395 MHz); spike guard kept


class CardFan:
    """v3 GPU server GPU card fan (2026-09-27: "add variable control to the GPU fan ... always move the GPU fans up first,
    before adjusting case fans"). Same settle-point method as the case fans: Tss = T + tau * slope; % needed =
    pct * ((Tss - inlet) / (aim - inlet)) ** (1 / 0.8), aim = the GPU's yellow; hold band aim - 1.5 .. aim + 0.5.
    Raises at once (5% steps, re-check after 45 s); lowers at most 10% per 90 s; between card_floor and 100%.
    The case fans help the GPU only once this is at 100% (see Equilibrium.tick's gpu argument)."""

    def __init__(self) -> None:
        self.sm, self.pct, self.hold, self.last, self.reason = Smooth(), None, 0, {}, None

    def reset(self) -> None:
        self.pct, self.hold = None, 0

    def tick(
        self, now: float, t: float, floor: float, start: float, busy: bool = True, reported: float | None = None
    ) -> float | None:
        self.sm.update(now, {"gpu0": (t, "gpu")})
        if (reported or 0) > 100 or t >= GPU_AIM[0] + 1:  # firmware spike, or past the aim (90C): 100% for 5 min
            self.spike_until = now + CARD_SPIKE_HOLD
        if busy:  # 2026-09-27: while busy the card fan holds CARD_BUSY_PCT (90), or 100 after a spike
            want = 100 if now < getattr(self, "spike_until", 0) else CARD_BUSY_PCT
            if self.pct != want:
                why = f"GPU busy, {'spike or >= ' + str(int(GPU_AIM[0] + 1)) + 'C: 100% for 5 min' if want == 100 else 'busy target'}"
                log(f"adaptive card fan: {self.pct} -> {want} % ({why}, GPU {t}C)")
                self.last = {"at": now, "from": self.pct, "to": want, "why": why}
            self.pct, self.hold, self.reason = want, now + 120, f"GPU busy: {want}%"
            return self.pct
        if self.pct is None:
            self.pct, self.hold = int(start), now + 60
            return self.pct
        if now < self.hold:
            return self.pct
        aim, tin = GPU_AIM[0], inlet_temp() or 27
        T, s = self.sm.v["gpu0"], self.sm.slope("gpu0")
        tss = max(T - 8, min(T + 15, T + s / 60 * EQ_TAU["gpu"]))
        self.reason = f"GPU {T:.0f}C settling at {tss:.0f}C vs aim {aim:.0f}C"
        if aim - EQ_BAND_LOW <= tss <= aim + EQ_BAND_HIGH:
            self.hold = now + 30
            return self.pct
        mid = aim - (EQ_BAND_LOW - EQ_BAND_HIGH) / 2
        need = self.pct * ((tss - tin) / max(0.5, mid - tin)) ** (1 / EQ_N)
        new = int(max(floor, min(100, math.ceil(need / 5) * 5)))
        if new < self.pct:
            new = max(new, self.pct - 5)  # idle only, and slowly
        if new != self.pct:
            log(f"adaptive card fan: {self.pct} -> {new} % ({self.reason})")
            self.last = {"at": now, "from": self.pct, "to": new, "why": self.reason}
            self.hold, self.pct = now + (45 if new > self.pct else 180), new
        else:
            self.hold = now + 30
        return self.pct

    def state(self, *_: object) -> dict:
        return {
            "target_pct": self.pct,
            "phase": "hold" if self.pct is not None else None,
            "last_change": self.last,
            "settle_reason": self.reason,
        }


class Adaptive:
    """Adaptive Run Mode v2, one instance per axis (case fans RPM; GPU server card fan %). Decisions use smoothed temperatures:
    settle by trend then a short test; step size by headroom; look-ahead gate; step up on trend; load feed-forward;
    anti-hunt backoff for levels that had to be reverted."""

    def __init__(
        self,
        step: float = ADAPT_STEP,
        big: float = ADAPT_STEP_BIG,
        unit: str = "RPM",
        name: str = "adaptive",
        tiny: float = ADAPT_STEP_TINY,
    ) -> None:
        self.step, self.big, self.tiny, self.unit, self.name = step, big, tiny, unit, name
        self.sm = Smooth()
        self.blocked, self.load_hist, self.low_since = {}, [], None
        self.reset()

    def reset(self) -> None:
        self.target, self.phase, self.ends, self.phase_start, self.last, self.raw = None, None, 0, 0, {}, []
        self.settle_reason, self.step_size, self.projection, self.down_at, self.down_from = None, None, None, 0, None
        self.pace = 1.0

    def _set(self, now: float, to: float, phase: str, why: str) -> None:
        if to != self.target:
            log(f"{self.name}: {self.target} -> {to} {self.unit} ({why}); {phase}")
            if (
                self.target is not None
                and to > self.target
                and self.down_from
                and now - self.down_at <= ADAPT_REVERT_WIN
                and not why.startswith("load jump")
            ):  # a load kick is feed-forward, not proof the level was too low
                lvl = self.target  # a step down reverted within 5 min: block that level
                b = self.blocked.get(lvl, {"n": 0, "until": 0, "last": 0})
                n = b["n"] + 1 if now - b["last"] < ADAPT_BLOCK_RESET else 1
                hold = min(ADAPT_BLOCK_MAX, ADAPT_BLOCK_BASE * 2 ** (n - 1))
                self.blocked[lvl] = {"n": n, "until": now + hold, "last": now}
                log(f"{self.name}: level {lvl} {self.unit} blocked {hold // 60} min (revert #{n})")
                self.down_at, self.down_from = 0, None
            self.last = {"at": now, "from": self.target, "to": to, "why": why}
        self.target, self.phase, self.phase_start = to, phase, now
        self.ends = now + (ADAPT_SETTLE_MIN if phase == "settle" else ADAPT_TEST) * self.pace

    def force_max(self, now: float, max_v: float, why: str) -> None:
        self.pace = 1.0
        if self.target is not None and self.target != max_v:
            self._set(now, max_v, "settle", why)

    def up(self, now: float, max_v: float, amount: float, why: str) -> None:
        self.pace = 1.0
        if self.target is not None and self.target < max_v:
            self._set(now, min(max_v, self.target + amount), "settle", why)

    def tick(
        self,
        now: float,
        sensors: dict,
        max_v: float,
        floor_v: float,
        margin_keys: list[str] | None = None,
        jump_keys: list[str] | None = None,
        gpu: tuple | None = None,
        load: dict | None = None,
        ceil_v: float | None = None,
    ) -> float:
        """sensors {name: (temp, kind)}. margin_keys: sensors needing the enter-8 look-ahead (default all). jump_keys: sensors
        whose raw +5C/60s or near-enter sends to max (default all). gpu: (name, limit), judged by its own look-ahead limit and
        counted for the jump only when near its emergency. load: {name: %} for the feed-forward."""
        self.sm.update(now, sensors)
        ceil = max(
            max_v, ceil_v or max_v
        )  # 2026-09-26: load kicks stop at the target (max_v); temperature may go on to the ceiling
        mk = [k for k in (margin_keys if margin_keys is not None else sensors) if k in sensors]
        jk = [k for k in (jump_keys if jump_keys is not None else sensors) if k in sensors]
        enter = {k: EMERGENCY[sensors[k][1]][0] for k in sensors}
        if self.target is None:
            self.raw = []
            self._set(now, max_v, "settle", "start at max")
            return self.target
        if self.target > ceil:  # ceiling lowered live: clamp at once instead of at the next decision
            self._set(now, ceil, "settle", f"ceiling lowered to {ceil}")
        near = min((enter[k] - sensors[k][0] for k in jk), default=99)
        if gpu and gpu[0] in sensors:
            near = min(near, enter[gpu[0]] - sensors[gpu[0]][0])
        if near <= ADAPT_NEAR:  # within 1C of Emergency: straight to the ceiling
            if self.target != ceil:
                self._set(now, ceil, "settle", f"closest sensor {near:.0f}C from emergency")
            self.phase = "max"
            return self.target
        # Intercept (2026-09-27, replaces "+5C in 60 s -> max"): each sensor's raw temperature is a moving object
        # T(t) = T0 + v t + a t^2 / 2; its limit L = enter - 1 (the max run temperature) is fixed. Solve for the time they
        # meet; if it's within the look-ahead, raise the fans just enough to cancel the excess slope (never to max unless < 30 s).
        self.rawk = (
            {k: [(t0, c) for t0, c in self.rawk.get(k, []) if now - t0 <= ADAPT_JUMP_WINDOW] + [(now, sensors[k][0])] for k in jk}
            if hasattr(self, "rawk")
            else {k: [(now, sensors[k][0])] for k in jk}
        )
        best = None
        for k, h in self.rawk.items():
            if len(h) < 4 or h[-1][0] - h[0][0] < 20:
                continue
            mid = len(h) // 2
            sl = lambda seg: (seg[-1][1] - seg[0][1]) / max(1e-6, seg[-1][0] - seg[0][0])
            v1, v2 = sl(h[: mid + 1]), sl(h[mid:])
            v, acc = sl(h), max(0.0, (v2 - v1) / max(1e-6, (h[-1][0] - h[0][0]) / 2))
            gap = (enter[k] - ADAPT_NEAR) - h[-1][1]
            if v * 60 < ADAPT_INTERCEPT_MIN_SLOPE or gap <= 0:  # ignore drift: acceleration on a near-flat line is noise
                continue
            t_hit = gap / v if acc < 1e-6 else (-v + math.sqrt(v * v + 2 * acc * gap)) / acc
            if t_hit < ADAPT_LOOKAHEAD and (best is None or t_hit < best[1]):
                best = (k, t_hit, v, gap)
        if best:
            k, t_hit, v, gap = best
            if t_hit < ADAPT_INTERCEPT_FAST:
                amt, what = ceil - self.target, "ceiling"
            else:
                excess = (
                    v - gap / ADAPT_LOOKAHEAD
                ) * 60  # C/min the fans must remove so the limit isn't reached within the look-ahead
                amt = min(ceil - self.target, max(ADAPT_STEP_TINY, math.ceil(excess / ADAPT_INTERCEPT_GAIN * 1000 / 250) * 250))
                if self.unit != "RPM":  # the GPU server card-fan axis is in %: one big step
                    amt = min(ceil - self.target, self.big)
                what = f"+{amt}"
            if amt > 0 and now >= getattr(self, "icept_hold", 0):
                # judge the next step only on readings taken after the fans finish ramping to this one (+30 s)
                self.icept_hold = now + (amt / FAN_RAMP_RPM * FAN_RAMP_S if self.unit == "RPM" else 0) + 30
                self.rawk = {}
                self._set(
                    now,
                    self.target + amt,
                    "settle",
                    f"intercept: {k} {sensors[k][0]:.0f}C rising {v * 60:+.1f}C/min, "
                    f"reaches {enter[k] - ADAPT_NEAR:.0f}C in {t_hit:.0f}s -> {what} RPM",
                )
                return self.target
        if self.phase == "max":
            self.phase, self.phase_start, self.ends = "settle", now, now + ADAPT_SETTLE_MIN
        load = {k: v for k, v in (load or {}).items() if k != "disk"}  # 2026-09-26: disk busy % out of the feed-forward
        if load:  # compare 30 s averages (last 30 s vs the 30 s before), not single samples
            self.load_hist = [(t0, l) for t0, l in self.load_hist if now - t0 <= 2 * ADAPT_LOAD_WIN] + [(now, load)]
            new_w = [l for t0, l in self.load_hist if now - t0 < ADAPT_LOAD_WIN]
            old_w = [l for t0, l in self.load_hist if now - t0 >= ADAPT_LOAD_WIN]
            keys = [k for k in load if all(k in l for l in new_w + old_w)]
            if old_w and keys and now - self.load_hist[0][0] >= 2 * ADAPT_LOAD_WIN - TICK:
                avg = lambda w, k: sum(l[k] for l in w) / len(w)
                jump = max(avg(new_w, k) - avg(old_w, k) for k in keys)
                if jump >= ADAPT_LOAD_JUMP and self.target < max_v:
                    self.up(
                        now,
                        max_v,
                        ADAPT_LOAD_KICK if self.unit == "RPM" else self.big,
                        f"load jump +{jump:.0f} points (30 s avg)",
                    )
                    self.load_hist = [(now, load)]
                    return self.target
                drop = max(avg(old_w, k) - avg(new_w, k) for k in keys)
                self.low_since = (self.low_since or now) if drop >= ADAPT_LOAD_JUMP else None
        proj = {k: self.sm.proj(k) for k in mk}
        if gpu and gpu[0] in sensors:
            proj[gpu[0]] = self.sm.proj(gpu[0])
        limit = lambda k: gpu[1] if gpu and k == gpu[0] else enter[k] - ADAPT_MARGIN
        worst = min(proj, key=lambda k: limit(k) - proj[k]) if proj else None
        self.projection = {"sensor": worst, "c": round(proj[worst], 1), "limit": limit(worst)} if worst else None
        for k in (
            mk if now >= getattr(self, "icept_hold", 0) else []
        ):  # no trend step while an intercept step is still taking effect
            if (
                len(getattr(self, "rawk", {}).get(k, [])) >= 4
            ):  # the intercept already judged this sensor; the linear trend is only a fallback
                continue
            if proj[k] >= enter[k] - ADAPT_UP_MARGIN:
                self.up(
                    now,
                    ceil,
                    self.big if self.sm.slope(k) > ADAPT_UP_FAST_SLOPE else self.step,
                    f"{k} heading to {proj[k]:.0f}C (enter {enter[k]}C, slope {self.sm.slope(k):+.1f}C/min)",
                )
                return self.target
        if self.phase == "settle":
            dt = now - self.phase_start
            calm = all(abs(self.sm.slope(k)) < ADAPT_SETTLE_SLOPE for k in sensors)
            early = self.low_since is not None and now - self.low_since >= ADAPT_LOAD_DROP_HOLD
            smin, smax = ADAPT_SETTLE_MIN * self.pace, ADAPT_SETTLE_MAX * self.pace
            if dt >= smax or (dt >= smin and (calm or early)):
                self.settle_reason = "trend" if calm else "load drop" if early and dt < smax else "max"
                if dt < smax and calm and dt <= smin + 5:
                    self.settle_reason = "min"
                self.phase, self.phase_start, self.ends = "test", now, now + ADAPT_TEST * self.pace
            return self.target
        if self.phase == "test":
            gate = all(proj[k] < limit(k) if not (gpu and k == gpu[0]) else proj[k] <= limit(k) for k in proj)
            if not gate:
                if self.down_from and now - self.down_at <= ADAPT_REVERT_WIN:
                    self._set(
                        now,
                        self.down_from,
                        "settle",
                        f"look-ahead failed after a step down ({worst} heading to {proj[worst]:.0f}C)",
                    )
                else:
                    self.phase, self.phase_start, self.ends = "settle", now, now + ADAPT_SETTLE_MIN
                return self.target
            if now < self.ends:
                return self.target
            head = min((enter[k] - self.sm.v[k] for k in mk), default=99)
            size = (
                self.big
                if head >= ADAPT_BIG_HEADROOM
                else self.step
                if head >= ADAPT_MID_HEADROOM
                else self.tiny
                if head >= ADAPT_MARGIN
                else 0
            )
            if self.unit == "RPM":
                size = ADAPT_DOWN_STEP_RPM if head >= ADAPT_MARGIN else 0
                self.pace = (
                    ADAPT_PACE[2]
                    if head >= ADAPT_BIG_HEADROOM
                    else ADAPT_PACE[1]
                    if head >= ADAPT_MID_HEADROOM
                    else ADAPT_PACE[0]
                )
            self.step_size = size
            for sz in [x for x in (self.big, self.step, self.tiny) if 0 < x <= size] or ([size] if size else [0]):
                cand = max(floor_v, self.target - sz) if sz else self.target
                b = self.blocked.get(cand)
                if cand < self.target and not (b and now < b["until"]):
                    self.down_at, self.down_from = now, self.target
                    self._set(now, cand, "settle", f"test passed: headroom {head:.0f}C, step {self.target - cand}")
                    return self.target
            self.phase, self.phase_start, self.ends = "test", now, now + ADAPT_TEST * self.pace
        return self.target

    def state(self, max_v: float, floor_v: float, unit: str = "rpm") -> dict | None:
        if not self.target:
            return None
        now = time.time()
        return {
            f"target_{unit}": self.target,
            "phase": self.phase,
            "phase_ends": self.ends,
            f"floor_{unit}": floor_v,
            f"max_{unit}": max_v,
            "last_change": self.last,
            "settle_reason": self.settle_reason,
            "step_size": self.step_size,
            "projection": self.projection,
            "pace": self.pace,
            "blocked": {str(k): v["until"] for k, v in self.blocked.items() if v["until"] > now},
        }


MEETING_RPM = NORMAL_RPM  # Meeting Mode overlay: fixed quiet case speed (2026-09-28: an overlay like Quick Cool; never changes the run profile)
_rapl = {"w": None}


def apply_freq(
    low: bool,
) -> None:  # both hosts: cap CPU clock to its minimum in low modes (2026-09-28: lower CPU wattage; RAPL alone didn't hold)
    try:
        for c in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq"):
            src = "cpuinfo_min_freq" if low else "cpuinfo_max_freq"
            with open(os.path.join(c, src)) as f:
                v = f.read().strip()
            with open(os.path.join(c, "scaling_max_freq"), "w") as f:
                f.write(v)
        log(f"cpu clock cap -> {'minimum (low mode)' if low else 'maximum'}")
    except OSError as e:
        log(f"cpu clock cap failed: {e}")


def apply_dram(low: bool) -> None:  # GPU server: memory RAPL cap per socket in low modes, uncapped otherwise
    try:
        for d in glob.glob("/sys/class/powercap/intel-rapl:*:*"):
            with open(os.path.join(d, "name")) as f:
                if f.read().strip() != "dram":
                    continue
            if low:
                with open(os.path.join(d, "constraint_0_power_limit_uw"), "w") as f:
                    f.write(str(DRAM_LOW_W * 1000000))
            with open(os.path.join(d, "enabled"), "w") as f:
                f.write("1" if low else "0")
        log(f"memory power cap -> {f'{DRAM_LOW_W} W/socket (low mode)' if low else 'off'}")
    except OSError as e:
        log(f"memory power cap failed: {e}")


def apply_rapl(
    w: int | None,
) -> None:  # hub: iDRAC DCMI whole-server cap (its BIOS locks RAPL). Both hosts: CPU clock cap via apply_freq.
    if w == _rapl["w"]:
        return
    apply_freq(w == RAPL_LOW)
    if GPU_SERVER:
        # apply_dram() tried 2026-09-28: the GPU server accepted a 20 W/socket memory cap but kept drawing ~40 W/socket, so it's off
        _rapl["w"] = w
        return
    _rapl["w"] = w
    apply_dcmi()


THROTTLE_FILE = "/dev/shm/power-throttle.json"  # written by the dashboard's POST /api/throttle (power-management thread)
_gpu_th = {"t": 0.0, "w": None, "by": None}


def gpu_throttle_w() -> float | None:
    """GPU server: power-guard's lower-only GPU power-limit throttle (2026-10-01: GPU joins the power-target levers).
    GET /api/throttle every 10 s; an expired or missing throttle = no bound. The profile limit stays the ceiling."""
    now = time.time()
    if now - _gpu_th["t"] >= 10:
        _gpu_th["t"] = now
        try:
            import urllib.request

            th = (json.load(urllib.request.urlopen(DASH_URL + "/api/throttle", timeout=4)) or {}).get(GPU_THROTTLE_KEY) or {}
            _gpu_th["w"] = th.get("gpu_w") if (th.get("left_s") or 0) > 0 else None
            _gpu_th["by"] = th.get("by")
        except Exception:
            _gpu_th["w"] = None  # dashboard unreachable: no throttle (it expires anyway)
    return _gpu_th["w"]


_dcmi = {"cap": None}


def apply_dcmi() -> None:  # hub: mode cap, lowered (never raised) by an unexpired power-management throttle; called every tick
    if GPU_SERVER or _rapl["w"] is None:
        return
    cap, why = (DCMI_LOW, "low mode") if _rapl["w"] == RAPL_LOW else (DCMI_HIGH, "normal")
    try:
        with open(THROTTLE_FILE) as f:
            th = json.load(f)
        if (th.get("until") or 0) > time.time() and th.get("dcmi_w") and th["dcmi_w"] < cap:
            cap, why = (
                int(th["dcmi_w"]),
                f"power-management throttle until {time.strftime('%H:%M:%S', time.localtime(th['until']))} ({th.get('by', '?')})",
            )
    except (OSError, ValueError):
        pass
    if cap == _dcmi["cap"]:
        return
    try:
        out = subprocess.run(
            [IPMITOOL, "dcmi", "power", "set_limit", "limit", str(cap)], capture_output=True, text=True, timeout=20, env=ENV
        )
        subprocess.run([IPMITOOL, "dcmi", "power", "activate"], capture_output=True, text=True, timeout=20, env=ENV)
        log(
            f"server power cap (DCMI) -> {cap} W ({why}){'' if out.returncode == 0 else ': ' + (out.stderr or out.stdout).strip()[:80]}"
        )
        _dcmi["cap"] = cap
    except Exception as e:
        log(f"server power cap failed: {e}")


# GPU -> GPU server case fans (2026-09-28: "the GPU is a special case: a subcomponent with its own fan"). Order of help:
# 1) its own card fan (always first), 2) the case fans via this map, 3) the power limit (warm-room 200 W; PL_EMERG in Emergency).
# The map only applies while the GPU is busy AND its card fan is already >= GPU_CASE_CARD_MIN. The lift above the 4,000 RPM
# base is scaled by how hard the card works (draw / power limit, 0.5..1). Rises at once; after GPU_CASE_HOLD_S it eases
# 500 RPM a minute once the temperature is back under the map. It is a floor: Adaptive can still run the fans higher.
GPU_CASE_MAP = [
    (86, 5000),
    (87, 5750),
    (88, 6500),
    (89, 7250),
    (90, 8000),
]  # GPU C -> case RPM floor. v2 values again at 210 W (2026-09-29; v3 was +500 for the 230 W trial). v2 (2026-09-28 "GPU fan spiking"): one step earlier and stronger, so the GPU settles at 86-87C instead of 88-89C, where its firmware pushes the card fan past 100%
GPU_CASE_CARD_MIN, GPU_CASE_HOLD_S, GPU_CASE_EASE_BELOW, GPU_CASE_REF_W = (
    95,
    180,
    86,
    200,
)  # ease only once the GPU is below 86C (no easing while still hot)
_gcf = {"rpm": None, "since": 0, "last_down": 0, "why": "", "draws": []}


def gpu_case_floor(now: float, gs: dict, busy: bool, draw: float | None) -> int | None:
    t, card = gs.get("temp") or 0, gs.get("card_fan") or 0
    pl = gs.get("limit_set") or gs.get("limit") or 250
    _gcf["draws"] = [(a, w) for a, w in _gcf["draws"] if now - a <= 120] + [(now, draw or 0)]
    peak = max(w for _, w in _gcf["draws"])  # 2-min peak: the jobs burst 100 -> 190 W between calls; don't flap with them
    want = None
    if busy and card >= GPU_CASE_CARD_MIN:
        for temp, rpm in GPU_CASE_MAP:
            if t >= temp:
                want = rpm
        if want:
            frac = max(
                0.5, min(1.0, peak / GPU_CASE_REF_W)
            )  # fixed reference: raising the power limit must not weaken the case help (v3)
            want = int(round((NORMAL_RPM + (want - NORMAL_RPM) * frac) / 250) * 250)
    cur = _gcf["rpm"]
    if want and (cur is None or want > cur):
        _gcf.update(
            rpm=want,
            since=now,
            last_down=now,
            why=f"GPU {t}C, load {'busy' if busy else 'idle'}, card fan {card}%, peak {peak:.0f} of {pl:.0f} W",
        )
        log(f"GPU case floor -> {want} RPM ({_gcf['why']})")
    elif (
        cur and (t < GPU_CASE_EASE_BELOW or not busy) and now - _gcf["since"] >= GPU_CASE_HOLD_S and now - _gcf["last_down"] >= 60
    ):
        new = max(want or 0, cur - 500)
        _gcf.update(rpm=new if new > NORMAL_RPM else None, last_down=now)
        log(f"GPU case floor -> {str(new) + ' RPM' if new > NORMAL_RPM else 'off'} (GPU {t}C, easing)")
    return _gcf["rpm"]


GPU_PAUSE_FILE = "/dev/shm/gpu-pause.json"  # GPU server: GPU jobs frozen by Low Power (pid + start time), resumed on leaving it
GPU_PAUSE_CLIENTS = re.compile(
    r"copilot_history(\.cli)?\s.*?\b(conv-topics|bakeoff|turns|topic-merge|label-turns|label-screenshots)\b"
)
_gp = {"at": 0, "held": None}
CARD_PAUSED_PCT, CARD_PAUSED_MAX_C = (
    50,
    85,
)  # card fan while Low Power has the GPU jobs frozen: 50% (2026-09-28; 18% let the idle card creep to 80C); back to the adaptive card fan at 85C+
CARD_PAUSED_WARM = (77, 73, 50)  # warm step kept for tuning; same 50% for now


def _pstart(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[19]
    except OSError:
        return None


def _cmd(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


def _pstate(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0]
    except OSError:
        return None


def _hooks() -> dict:  # {hook name: [compiled "# manages:" regexes]} from /dev/shm/power-hooks
    out = {}
    for hk in sorted(glob.glob("/dev/shm/power-hooks/*")):
        if not os.path.isfile(hk) or not os.access(hk, os.X_OK) or hk.endswith((".json", ".log", ".tmp")):
            continue
        try:
            with open(hk, errors="replace") as f:
                out[hk] = [re.compile(m.group(1).strip()) for m in re.finditer(r"^#\s*manages:\s*(.+)$", f.read(), re.M)]
        except (OSError, re.error):
            pass
    return out


def gpu_census() -> tuple:
    """Who is on the GPU right now, found from the driver rather than by name (2026-09-28: the GPU jobs change):
    - gpu: every process NVML lists as a compute app (with its VRAM);
    - server: a gpu process with a listening TCP port (llama-server, ollama, vLLM, ...). Never frozen: its clients are;
    - client: a local process holding a connection to a server's port (plus GPU_PAUSE_CLIENTS, which catches a runner
      between requests); remote clients (other hosts) are listed by address only;
    - handler: the power hook whose "# manages:" regex matches the command line, else None (fan-control's fallback)."""
    gpu = {}
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        for ln in out.splitlines():
            a = [x.strip() for x in ln.split(",")]
            if a and a[0].isdigit():
                gpu[int(a[0])] = int(a[1]) if len(a) > 1 and a[1].isdigit() else None
    except Exception:
        pass
    listen, est, mine = {}, [], set()  # inode -> port ; (inode, local port, remote addr hex, remote port) ; this host's addresses
    for fn in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(fn) as f:
                for ln in f.readlines()[1:]:
                    c = ln.split()
                    lport, (raddr, rport), st, ino = int(c[1].split(":")[1], 16), c[2].split(":"), c[3], c[9]
                    mine.add(c[1].split(":")[0])
                    if st == "0A":
                        listen[ino] = lport
                    elif st == "01":
                        est.append((ino, lport, raddr, int(rport, 16)))
        except (OSError, IndexError, ValueError):
            pass
    owner = {}  # socket inode -> pid, for every process
    for d in glob.glob("/proc/[0-9]*/fd"):
        pid = int(d.split("/")[2])
        try:
            for fd in os.listdir(d):
                try:
                    t = os.readlink(f"{d}/{fd}")
                except OSError:
                    continue
                if t.startswith("socket:["):
                    owner[t[8:-1]] = pid
        except OSError:
            pass
    ports = {}  # server pid -> its listening ports
    for ino, port in listen.items():
        if owner.get(ino) in gpu:
            ports.setdefault(owner[ino], set()).add(port)
    local = ("0100007F", "00000000000000000000000001000000", "0000000000000000FFFF00000100007F")
    clients, remote = {}, {}
    for ino, lport, raddr, rport in est:
        pid = owner.get(ino)
        for srv, pp in ports.items():
            if pid is not None and pid != srv and rport in pp:
                clients.setdefault(pid, srv)  # local client connected to a GPU server
            elif pid == srv and lport in pp and raddr not in local and raddr not in mine:
                a = raddr[-8:]
                remote.setdefault(srv, set()).add(".".join(str(int(a[i : i + 2], 16)) for i in (6, 4, 2, 0)))
    for d in glob.glob("/proc/[0-9]*/cmdline"):
        pid = int(d.split("/")[2])
        if pid not in gpu and pid not in clients and GPU_PAUSE_CLIENTS.search(_cmd(pid)):
            clients[pid] = None
    hooks = _hooks()
    procs = []
    for pid, role in [(p, "server" if p in ports else "gpu") for p in gpu] + [(p, "client") for p in clients if p not in gpu]:
        if pid == os.getpid():
            continue
        cmd = _cmd(pid)
        if not cmd:
            continue
        procs.append(
            {
                "pid": pid,
                "role": role,
                "cmd": cmd[:160],
                "vram_mib": gpu.get(pid),
                "state": _pstate(pid),
                "server": clients.get(pid),
                "ports": sorted(ports.get(pid, [])),
                "remote_clients": sorted(remote.get(pid, [])),
                "handler": next((os.path.basename(h) for h, rx in hooks.items() if any(r.search(cmd) for r in rx)), None),
            }
        )
    return procs, hooks


def gpu_pause(
    on: bool, now: float
) -> list:  # 2026-09-28: Low Power pauses the GPU jobs without losing work (SIGSTOP / SIGCONT; state stays in memory)
    if not GPU:
        return []
    if _gp["held"] is None:
        try:
            with open(GPU_PAUSE_FILE) as f:
                _gp["held"] = json.load(f)
        except (OSError, ValueError):
            _gp["held"] = []
    held, changed = _gp["held"], False
    if now - _gp["at"] >= 30:
        _gp["at"] = now
        procs, hooks = gpu_census()
        _gp["procs"] = procs
    else:
        procs, hooks = _gp.get("procs", []), None
    if on and hooks is not None:
        have = {(h["pid"], h["start"]) for h in held}
        called = _gp.setdefault("hook_called", {})
        for p in procs:
            pid, stopped = p["pid"], p["state"] in ("T", "t")
            if p["role"] == "server" or stopped:
                continue  # a server idles once its clients are paused; freezing it would strand remote clients mid-request
            if p["handler"]:  # its owner's hook pauses it; ask again if it is running, fall back after 2 min
                hk = p["handler"]
                try:  # the dash-agent already fired the hooks at the switch: count the 2 min from then, don't ask twice
                    with open("/dev/shm/power-hooks/state.json") as f:
                        st_at = json.load(f).get("at") or 0
                except (OSError, ValueError):
                    st_at = 0
                agent_fired = now - st_at < 120
                first = called.setdefault((hk, pid), st_at if agent_fired else now)
                if now - first < 120:
                    if first == now and not agent_fired:
                        subprocess.Popen(
                            [f"/dev/shm/power-hooks/{hk}", "down", "lowpower"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True,
                        )
                        log(f"GPU pause: {p['cmd'][:60]} (pid {pid}) is running in Low Power; asked its hook {hk} to pause it")
                    continue
                log(f"GPU pause: hook {hk} did not pause pid {pid} within 2 min; freezing it (fallback)")
            st = _pstart(pid)
            if (pid, st) in have:  # something else resumed a job we froze: freeze it again
                try:
                    os.kill(pid, signal.SIGSTOP)
                    log(f"GPU pause: pid {pid} was resumed by something else; froze it again")
                except OSError:
                    pass
                continue
            try:
                os.kill(pid, signal.SIGSTOP)
                held.append(
                    {
                        "pid": pid,
                        "start": st,
                        "comm": p["cmd"].split()[0].rsplit("/", 1)[-1] + (" (client)" if p["role"] == "client" else ""),
                        "cmd": p["cmd"][:120],
                        "at": now,
                    }
                )
                changed = True
                log(f"GPU pause: froze {p['role']} pid {pid} ({p['cmd'][:80]}) for Low Power")
            except OSError as e:
                log(f"GPU pause: could not freeze pid {pid}: {e}")
    if not on:
        _gp["hook_called"] = {}
    if not on and held:
        for h in held:
            if _pstart(h["pid"]) == h["start"]:
                try:
                    os.kill(h["pid"], signal.SIGCONT)
                    log(f"GPU pause: resumed {h.get('comm')} (pid {h['pid']})")
                except OSError:
                    pass
        held.clear()
        changed = True
    if changed:  # frozen jobs keep their CUDA contexts, which pin the card in P2 (~82 W idle); lock the core clock low while paused (~68 W)
        subprocess.run(["nvidia-smi", "-lgc", "300,300"] if held else ["nvidia-smi", "-rgc"], capture_output=True, timeout=15)
        log(f"GPU pause: core clock {'locked at 300 MHz' if held else 'unlocked'}")
        tmp = GPU_PAUSE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(held, f)
        os.replace(tmp, GPU_PAUSE_FILE)
    return held


def meeting_on() -> (
    bool
):  # the dashboard's "meeting" flag rides in the Quick Cool file (the GPU server agent copies it); stale after 60 s = off
    try:
        q = json.load(open(QUICK_COOL))
        return bool(q.get("meeting")) and time.time() - q["ts"] < QUICK_COOL_STALE
    except Exception:
        return False


def quick_cool() -> int | None:  # 2026-09-27: Quick Cool = fans 100% only (was the file's pct, 80); no wattage change
    try:
        q = json.load(open(QUICK_COOL))
        return 100 if q.get("on") and time.time() - q["ts"] < QUICK_COOL_STALE else None
    except Exception:
        return None


class Util(C.Structure):
    _fields_ = [("gpu", C.c_uint), ("memory", C.c_uint)]


class Gpu:
    def __init__(self) -> None:
        self.n = C.CDLL("libnvidia-ml.so.1")
        if self.n.nvmlInit_v2():
            raise RuntimeError("nvmlInit failed")
        self.h = C.c_void_p()
        self.n.nvmlDeviceGetHandleByIndex_v2(0, C.byref(self.h))
        cnt = C.c_uint()
        self.n.nvmlDeviceGetNumFans(self.h, C.byref(cnt))
        self.fans = range(cnt.value)
        self.card_manual = None
        subprocess.run(["nvidia-smi", "-pm", "1"], capture_output=True, timeout=20)

    def _u(self, fn: str, *a: object) -> int:
        v = C.c_uint()
        getattr(self.n, fn)(self.h, *a, C.byref(v))
        return v.value

    def read(self) -> dict:
        u = Util()
        self.n.nvmlDeviceGetUtilizationRates(self.h, C.byref(u))
        return {
            "temp": self._u("nvmlDeviceGetTemperature", 0),
            "draw": self._u("nvmlDeviceGetPowerUsage") / 1000,
            "limit": self._u("nvmlDeviceGetEnforcedPowerLimit") / 1000,
            "util": u.gpu,
            "card_fan": self._u("nvmlDeviceGetFanSpeed_v2", 0),
            "sm_mhz": self._u("nvmlDeviceGetClockInfo", 1),
        }

    def set_limit(self, w: int) -> None:
        if self.n.nvmlDeviceSetPowerManagementLimit(self.h, int(w * 1000)):
            subprocess.run(["nvidia-smi", "-pl", str(w)], capture_output=True, timeout=20)

    def card(self, pct: int | None) -> None:
        if pct == self.card_manual:
            return
        for i in self.fans:
            if pct is None:
                self.n.nvmlDeviceSetDefaultFanSpeed_v2(self.h, i)
            else:
                self.n.nvmlDeviceSetFanSpeed_v2(self.h, i, pct)
        self.card_manual = pct


def open_gpu() -> Gpu | None:
    if not shutil.which("nvidia-smi"):
        return None
    try:
        return Gpu()
    except Exception as e:
        log(f"GPU control unavailable: {e}")
        return None


GPU = open_gpu()


def hand_back(why: str) -> None:
    dell_auto()
    if GPU:
        GPU.card(None)
        GPU.set_limit(PL_FIXED)
    log(f"{why}: Dell auto fans" + (f", card fan automatic, power limit {PL_FIXED} W" if GPU else ""))


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *a: (hand_back("stopped"), sys.exit(0)))
    cur, mode, drives, last_drive = None, None, {}, 0
    hot = set()  # sensors currently in emergency (entered, not yet cleared)
    g = {"active": False, "last_busy": 0, "last_adj": 0, "pl": PL_FIXED, "hot_card": False, "warm_card": False}
    rpm, last_rpm, tw = None, 0, None
    act, last_work, work_why = Activity(), 0, ""
    cardfan = CardFan()
    adapt, card_adapt = (
        Equilibrium(),
        Adaptive(ADAPT_CARD_STEP, ADAPT_CARD_STEP_BIG, "%", "adaptive card fan", ADAPT_CARD_STEP_TINY),
    )
    gfloor = {"rpm": None, "until": None, "util": 0, "draw": 0}  # learned GPU-limited case floor (loop breaker)
    ramp = {"rpm": None, "at": 0}
    prev_card, prev_warm, gpu_hist, gpu_idle_since = None, False, [], None
    if GPU:
        GPU.set_limit(PL_FIXED)
        header = ["ts", "mode", "temp_c", "draw_w", "limit_w", "util_pct", "sm_mhz", "fan_pct", "fan_rpm", "card_fan_pct"]
        if os.path.exists(TRACE) and open(TRACE).readline().strip() != ",".join(header):
            os.replace(TRACE, TRACE + ".1")
        new = not os.path.exists(TRACE)
        tw = csv.writer(open(TRACE, "a", newline="", buffering=1))
        if new:
            tw.writerow(header)
    log(
        f"started ({ROLE} profile; fixed modes: normal {NORMAL_FAN}% (~{NORMAL_RPM} RPM), run {RUN_FAN}% (~{RUN_RPM} RPM), emergency {EMERG_FAN}%"
        + (f", card fan {CARD_FAN}%, power {PL_FIXED} W" if GPU else "")
        + ")"
    )
    while True:
        try:
            load_limits()
            now = time.time()
            if now - last_drive >= DRIVE_EVERY or not drives:
                drives, last_drive = drive_temps(), now
            sensors = {d: (t, kind) for d, (t, kind) in drives.items()}
            sensors.update({d: (round(t), "nvme") for d, t in nvme_temps().items()})
            sensors.update({c: (t, "cpu") for c, t in cpu_temps().items()})
            exh = exhaust_temp()
            if exh is not None:
                sensors["exhaust"] = (exh, "exhaust")
            rt = raid_temp()
            if rt is not None:
                sensors["raid0"] = (rt, "raid")
            gs = GPU.read() if GPU else None
            if gs:
                sensors["gpu0"] = (gs["temp"], "gpu")
            for s, (t, kind) in sensors.items():
                enter, clear = EMERGENCY[kind]
                if kind in NO_EMERG:  # tune-only kind (CPU): Adaptive steers by it, never Emergency
                    hot.discard(s)
                    continue
                if s not in hot and t >= enter:
                    hot.add(s)
                    log(f"EMERGENCY: {s} ({kind}) {t}C >= {enter}C")
                elif s in hot and t <= clear:
                    hot.discard(s)
                    log(f"emergency cleared: {s} ({kind}) {t}C <= {clear}C")
            hot &= set(sensors)
            qc = quick_cool()
            mt = meeting_on() and not qc
            apply_rapl(RAPL_LOW if (mt or run_profile()[0] in ("normal", "daytime", "lowpower")) else RAPL_HIGH)
            apply_dcmi()  # picks up / expires power-management throttles within one tick
            prof_name, prof = run_profile()
            gpaused = gpu_pause(prof_name == "lowpower", now)
            sig = (prof_name, prof["run_rpm"], prof["card_fan"], prof["pl"])
            if sig != g.get("profile"):
                log(
                    f"run profile -> {prof['label']}: case fans {prof['run_rpm']} RPM, card fan {prof['card_fan']}%, GPU {prof['pl']} W"
                )
                g["profile"] = sig
            run_fan = pct_for(prof["run_rpm"])
            why_work = act.sample()
            if gs and (gs["util"] >= BUSY_UTIL or gs["draw"] >= BUSY_W):
                why_work.insert(0, f"GPU {gs['util']}% {gs['draw']:.0f} W")
            if why_work:
                last_work, work_why = now, ", ".join(why_work)
            working = now - last_work < IDLE_S

            if gs:
                t = gs["temp"]
                if gs["util"] >= BUSY_UTIL or gs["draw"] >= BUSY_W:
                    g["last_busy"] = now
                    g["active"] = True
                elif g["active"] and now - g["last_busy"] >= IDLE_S:
                    g["active"] = False
                gpu_hot = "gpu0" in hot
                inl = inlet_temp()  # 2026-09-27: warm room -> 200 W (inlet >= 28C on, <= 27C off)
                if inl is not None:
                    g["warm_inlet"] = inl >= PL_WARM_INLET_ON or (g.get("warm_inlet") and inl > PL_WARM_INLET_OFF)
                base_pl = min(prof["pl"], PL_WARM) if (prof_name == "adaptive" and g.get("warm_inlet")) else prof["pl"]
                low_mode = mt or prof_name in ("normal", "daytime", "lowpower")
                pl = PL_EMERG if gpu_hot else min(base_pl, PL_LOW) if low_mode else base_pl
                gth = gpu_throttle_w()
                if gth and gth < pl:  # power-guard holding the combined power line (lower-only, never below the card minimum)
                    pl = max(PL_LOW, int(gth))
                if pl != g["pl"]:
                    GPU.set_limit(pl)
                    log(
                        f"power limit {g['pl']} -> {pl} W ({t}C, draw {gs['draw']:.0f} W, inlet {inl}C{', warm room' if g.get('warm_inlet') and pl == PL_WARM else ''}{', power-guard throttle' if gth and pl == max(PL_LOW, int(gth)) else ''})"
                    )
                    g["pl"] = pl
                g["hot_card"] = t >= CARD_HOT_ON or (g["hot_card"] and t > CARD_HOT_OFF)
                g["warm_card"] = t >= CARD_WARM_ON or (g["warm_card"] and t > CARD_WARM_OFF)
                base = CARD_FAN_IDLE if mt else prof["card_fan"] if g["active"] else CARD_FAN_IDLE
                gpu_hist = [x for x in gpu_hist if now - x[0] <= ADAPT_GPU_BUSY_AVG] + [(now, gs["util"], gs["draw"])]
                gutil = sum(x[1] for x in gpu_hist) / len(gpu_hist)
                gdraw = sum(x[2] for x in gpu_hist) / len(gpu_hist)
                gpu_busy = gutil >= BUSY_UTIL or gdraw >= BUSY_W  # 30 s average, so pauses between calls don't flicker
                gpu_idle_since = None if gpu_busy else (gpu_idle_since or now)
                if prof_name in ("adaptive", "lowpower") and not qc and not hot and not mt:
                    # v3: variable card fan aimed at the GPU's yellow; it always moves first (2026-09-27)
                    base = cardfan.tick(
                        now,
                        t,
                        prof.get("card_floor", CARD_FAN_IDLE),
                        GPU.card_manual or prof["card_fan"],
                        busy=gpu_busy or now - gpu_idle_since < ADAPT_GPU_IDLE_S,
                        reported=gs.get("card_fan"),
                    )
                    gy = YELLOW.get("gpu", 90)
                    g["warm_card"] = False  # no fixed 90% step at 87C under v3
                    g["hot_card"] = t >= gy + 1 or (g["hot_card"] and t > gy)  # safety: 100% from red until back at yellow
                    if (
                        prof_name == "lowpower" and gpaused and t < CARD_PAUSED_MAX_C
                    ):  # 2026-09-28: jobs frozen -> card fan at its minimum
                        g["pp_warm"] = t >= CARD_PAUSED_WARM[0] or (
                            g.get("pp_warm") and t > CARD_PAUSED_WARM[1]
                        )  # hysteresis, no flapping
                        base = CARD_PAUSED_WARM[2] if g["pp_warm"] else CARD_PAUSED_PCT
                else:
                    cardfan.reset()
                if gfloor["rpm"] and (
                    now >= gfloor["until"]
                    or abs(gutil - gfloor["util"]) >= 100 * ADAPT_LOAD_CHANGE
                    or abs(gdraw - gfloor["draw"]) >= ADAPT_LOAD_CHANGE * max(1, gfloor["draw"])
                ):
                    log(
                        f"adaptive: GPU-limited floor {gfloor['rpm']} RPM cleared "
                        f"({'expired' if now >= gfloor['until'] else 'GPU load changed'})"
                    )
                    gfloor.update(rpm=None, until=None)
                GPU.card(
                    CARD_FAN_HOT
                    if (qc or gpu_hot or g["hot_card"])
                    else max(CARD_FAN_WARM, base or 0)
                    if g["warm_card"]
                    else base
                )

            if qc:
                new_mode, target, why = "quick cool", qc, "dashboard (manual)"
            elif hot:
                new_mode, target, why = "emergency", EMERG_FAN, ", ".join(f"{s} {sensors[s][0]}C" for s in sorted(hot))
            elif mt:  # overlay: quiet fixed fans; the run profile stays as it is
                new_mode, target, why = "meeting", pct_for(MEETING_RPM), "Meeting Mode (overlay; run profile unchanged)"
            elif prof_name in ("adaptive", "lowpower"):  # 2026-09-27: the run mode is manual; never switch to Normal on idle
                ceil_rpm = prof.get(
                    "ceil_rpm", PROFILES["unattended"]["run_rpm"]
                )  # Low Power: 5,500. 2026-09-26: target is theirs to set; min always Daytime, max always Unattended
                prof = dict(prof, run_rpm=max(prof["floor_rpm"], min(ceil_rpm, prof["run_rpm"])))
                floor = max(prof["floor_rpm"], gfloor["rpm"] or 0)
                if (
                    prof_name == "lowpower" and g.get("adapt_prof") != "lowpower"
                ):  # 2026-09-28: entering Low Power (or a restart in it) starts at its 4,000 target, then adapts
                    adapt.reset()
                    ramp["rpm"] = None
                    g["resume_rpm"] = prof["run_rpm"]
                g["adapt_prof"] = prof_name
                load = dict(getattr(act, "load", {}))
                if gs:  # GPU server: case fans adapt first while the GPU isn't near its limit (look-ahead <= 86C)
                    load["gpu"] = gutil
                    if card_adapt.target and card_adapt.phase == "settle" and adapt.target:
                        floor = max(floor, adapt.target)  # interlock: one axis at a time
                    gcf = gpu_case_floor(now, gs, gpu_busy, gdraw)  # 2026-09-28: the GPU's own mapping to the case fans
                    if gcf:
                        floor = max(floor, gcf)
                    nongpu = [k for k in sensors if k != "gpu0"]
                    before = adapt.target
                    adapt.actual = ramp["rpm"] or g.pop("resume_rpm", None) or (rpm_for(cur) if cur else None)
                    rpm_t = adapt.tick(
                        now,
                        sensors,
                        prof["run_rpm"],
                        floor,
                        margin_keys=nongpu,
                        jump_keys=nongpu,
                        gpu=None,
                        load=load,
                        ceil_v=ceil_rpm,
                    )  # case fans help the GPU only once the card fan is at 100%
                    why_last = adapt.last.get("why", "")
                    if (
                        before
                        and rpm_t > before
                        and adapt.last.get("at") == now
                        and "look-ahead failed" in why_last
                        and "gpu0" in why_last
                    ):
                        gfloor.update(rpm=rpm_t, until=now + ADAPT_GPU_FLOOR_S, util=gutil, draw=gdraw)  # loop breaker
                        log(f"adaptive: GPU-limited floor {rpm_t} RPM for {ADAPT_GPU_FLOOR_S // 60} min")
                    gnear = (gs.get("temp") or 0) >= YELLOW.get(
                        "gpu", ADAPT_GPU_CASE_LIMIT
                    ) - 2  # 2026-09-27: kick only near the GPU's yellow
                    if (
                        GPU_CASE_KICK
                        and (gs.get("card_fan") or 0) > 100
                        and gnear
                        and adapt.target
                        and adapt.target < ceil_rpm
                        and now - g.get("cf_kick", 0) >= ADAPT_LOAD_WIN
                    ):
                        adapt.up(
                            now, ceil_rpm, ADAPT_LOAD_KICK, f"2080 fan over 100% ({gs['card_fan']}%) at {gs.get('temp')}C"
                        )  # 2026-09-26: card boosting past 100% = case too low
                        g["cf_kick"], rpm_t = now, adapt.target
                    elif (gs.get("card_fan") or 0) > 100 and now - g.get("cf_warn", 0) >= 300:
                        log(f"note: 2080 fan {gs['card_fan']}% at {gs.get('temp')}C, below yellow - 2; no case kick")
                        g["cf_warn"] = now
                    if adapt.phase == "max":
                        card_adapt.force_max(now, prof["card_fan"], "case fans jumped to max")
                    elif card_adapt.phase == "max":
                        adapt.force_max(now, ceil_rpm, "card fan jumped to max")
                else:
                    adapt.actual = ramp["rpm"] or g.pop("resume_rpm", None) or (rpm_for(cur) if cur else None)
                    rpm_t = adapt.tick(now, sensors, prof["run_rpm"], floor, load=load, ceil_v=ceil_rpm)
                if mode != "run-adaptive" or ramp["rpm"] is None:  # no near-Emergency bypass (2026-09-27): always ramp
                    ramp.update(rpm=rpm_t, at=now)
                elif ramp["rpm"] != rpm_t and now - ramp["at"] >= FAN_RAMP_S:
                    ramp.update(rpm=ramp["rpm"] + max(-FAN_RAMP_RPM, min(FAN_RAMP_RPM, rpm_t - ramp["rpm"])), at=now)
                new_mode, target, why = (
                    "run-adaptive",
                    pct_for(ramp["rpm"]),
                    f"adaptive {rpm_t} RPM: {adapt.settle_reason or adapt.phase}"
                    + (f", ramping at {ramp['rpm']}" if ramp["rpm"] != rpm_t else ""),
                )
            else:  # fixed profiles hold their own speed whether or not the server is working (the mode is the owner's to change)
                new_mode, target, why = (
                    ("normal" if prof_name == "normal" else f"run-{prof_name}"),
                    run_fan,
                    f"{prof['label']} (set manually)",
                )
            if new_mode != "run-adaptive":
                if (
                    new_mode in ("quick cool", "meeting") and adapt.target
                ):  # 2026-09-27: resume where Adaptive was after Quick Cool, not from 100%
                    g["resume_rpm"] = adapt.target
                elif new_mode not in ("quick cool", "meeting"):
                    g["resume_rpm"] = None
                adapt.reset()
                ramp["rpm"] = None
                if gfloor["rpm"]:
                    gfloor.update(rpm=None, until=None)
            if new_mode != mode or target != cur:
                set_fan(target)
                log(f"mode {mode} -> {new_mode}: fans {target}% ({why})")
                mode, cur = new_mode, target
            if gs and now - last_rpm >= RPM_EVERY:
                rpm, last_rpm = fan_rpm(), now
            if tw:
                tw.writerow(
                    [int(now), mode, gs["temp"], round(gs["draw"]), g["pl"], gs["util"], gs["sm_mhz"], cur, rpm, gs["card_fan"]]
                )
            st = {
                "ts": now,
                "events": EVENTS,
                "fan_pct": cur,
                "mode": mode,
                "driven_by": why,
                "quick_cool": bool(qc),
                "emergency": sorted(hot),
                "emergency_detail": {
                    h: {
                        "kind": sensors[h][1],
                        "temp": sensors[h][0],
                        "enter": EMERGENCY[sensors[h][1]][0],
                        "clear": EMERGENCY[sensors[h][1]][1],
                    }
                    for h in sorted(hot)
                },
                "profile": prof_name,
                "gpu_paused": [h.get("comm") for h in gpaused],
                "gpu_procs": _gp.get("procs", []) if GPU else None,
                "gpu_case_floor": dict(_gcf) if GPU else None,
                "adaptive": (adapt.state(prof["run_rpm"], prof.get("floor_rpm")) or {})
                | {"ramp_rpm": ramp["rpm"], "ceiling_rpm": prof.get("ceil_rpm", PROFILES["unattended"]["run_rpm"])}
                | (
                    {
                        "card": cardfan.state(prof["card_fan"], prof.get("card_floor", PROFILES["daytime"]["card_fan"]), "pct"),
                        "gpu_floor_rpm": gfloor["rpm"],
                        "gpu_floor_until": gfloor["until"],
                    }
                    if GPU
                    else {}
                )
                if prof_name in ("adaptive", "lowpower")
                else None,
                "drives": {d: t for d, (t, _) in drives.items()},
                "nvme": nvme_temps(),
                "cpus": {s: t for s, (t, k) in sensors.items() if k == "cpu"},
                "gpus": {"gpu0": gs["temp"]} if gs else {},
                "exhaust": exh,
                "raid": {"raid0": rt} if rt is not None else {},
            }
            if gs:
                st["gpu"] = gs | {
                    "run_mode": g["active"],
                    "emergency": "gpu0" in hot,
                    "limit_set": g["pl"],
                    "card_manual": GPU.card_manual,
                }
            with open(STATE + ".tmp", "w") as f:
                json.dump(st, f)
            os.replace(STATE + ".tmp", STATE)
        except Exception as e:
            hand_back(f"ERROR {e}; retry in {TICK * 12} s")
            cur = mode = None
            time.sleep(TICK * 11)
        time.sleep(TICK)


if __name__ == "__main__":
    main()
