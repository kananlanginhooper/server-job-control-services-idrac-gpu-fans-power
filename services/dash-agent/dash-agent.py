#!/usr/bin/env python3
import ctypes, glob, json, os, re, subprocess, sys, threading, time

sys.path.insert(0, "/dev/shm/pylib")
try:
    import pymysql
except ImportError:
    pymysql = None
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "lib"))
from svcenv import env  # noqa: E402  (shared repo .env; see .env.example)

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8091
NIC = sys.argv[2] if len(sys.argv) > 2 else "all"
MOUNTS = ["/", "/raid6"]  # 2026-09-30: /32a + /32b rebuilt as RAID 6 (/raid6)
DISK_RE = re.compile(r"^(sd[a-z]+|nvme\d+n\d+)$")
WATCH = {
    "rsync": r"\brsync\b",
    "photorec": r"photorec",
    "ddrescue": r"ddrescue",
    "zeromap": r"zeromap",
    "sshfs": r"sshfs",
    "smbd": r"smbd",
    "catalog.py": r"catalog\.py",
}
LOGS = ["/dev/shm/copy-prior.log"]
HUB_CNF = "/dev/shm/dash-db.cnf"
DASH_URL = env("HUB_API_URL", "http://localhost:8090")  # site settings: see .env.example
HOST_KEY = env("HOST_KEY", "gpu-server")  # this server's key in the dashboard's per-host JSON
DASH_CLIENT = env("DASH_CLIENT_IP", "127.0.0.1")  # the only address allowed to POST /runprofile
POWERCAPS = env("AGENT_POWERCAPS", "1") == "1"  # 0 on a host whose caps the dashboard doesn't manage
QC_URL, QC_FILE = DASH_URL + "/api/quickcool", "/dev/shm/quick-cool.json"
RP_URL, RP_FILE = DASH_URL + "/api/runprofile", "/dev/shm/run-profile.json"

state = {"fast": {}, "history": deque(maxlen=180), "updated": 0, "health": {}}
SMARTCTL = "/dev/shm/tools/smartctl"
HEALTH_EVERY = 600
lock = threading.Lock()


def read(p, default=""):
    try:
        with open(p) as f:
            return f.read().strip()
    except OSError:
        return default


def cpu_times():
    v = [int(x) for x in read("/proc/stat").splitlines()[0].split()[1:]]
    return sum(v), v[3] + v[4]


def diskstats():
    out = {}
    for line in read("/proc/diskstats").splitlines():
        f = line.split()
        if DISK_RE.match(f[2]):
            out[f[2]] = (int(f[5]) * 512, int(f[9]) * 512)
    return out


def nics():
    if NIC != "all":
        return NIC.split(",")
    return sorted(n for n in os.listdir("/sys/class/net") if os.path.exists(f"/sys/class/net/{n}/device"))


def netbytes():
    rx = tx = 0
    for n in nics():
        b = f"/sys/class/net/{n}/statistics/"
        rx += int(read(b + "rx_bytes", "0"))
        tx += int(read(b + "tx_bytes", "0"))
    return rx, tx


def nic_info():
    out = {}
    for n in nics():
        if read(f"/sys/class/net/{n}/operstate") == "up":
            b = f"/sys/class/net/{n}/statistics/"
            out[n] = {
                "speed_mbit": int(read(f"/sys/class/net/{n}/speed", "0") or 0),
                "rx": int(read(b + "rx_bytes", "0")),
                "tx": int(read(b + "tx_bytes", "0")),
            }
    return out


def mounts():
    opts = {l.split()[1]: l.split()[3] for l in read("/proc/mounts").splitlines()}
    out = []
    for m in MOUNTS:
        try:
            st = os.statvfs(m)
            u = {"total": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize}
        except OSError:
            u = None
        o = opts.get(m, "")
        out.append({"mount": m, "usage": u, "mounted": bool(o), "shutdown": "shutdown" in o or "emergency_ro" in o})
    return out


def temps():
    out = {}
    for h in glob.glob("/sys/class/hwmon/hwmon*"):
        vals = [int(read(t, "0")) / 1000 for t in glob.glob(h + "/temp*_input")]
        if vals:
            out.setdefault(read(h + "/name"), []).append(max(vals))
    return out


nvml = {"lib": None, "h": None}


class NvFan(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint), ("fan", ctypes.c_uint), ("speed", ctypes.c_uint)]


def gpu_fan_rpm():
    try:
        if nvml["lib"] is None:
            lib = ctypes.CDLL("libnvidia-ml.so.1")
            h = ctypes.c_void_p()
            if lib.nvmlInit_v2() or lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(h)):
                return None
            nvml.update(lib=lib, h=h)
        s = NvFan()
        s.version = ctypes.sizeof(NvFan) | (1 << 24)
        s.fan = 0
        return float(s.speed) if nvml["lib"].nvmlDeviceGetFanSpeedRPM(nvml["h"], ctypes.byref(s)) == 0 else None
    except (OSError, AttributeError):
        return None


def gpu_pcie():
    try:
        if nvml["lib"] is None:
            gpu_fan_rpm()
        if nvml["lib"] is None:
            return {}
        lib, h, out = nvml["lib"], nvml["h"], {}
        for key, counter in (("pcie_tx", 0), ("pcie_rx", 1)):
            v = ctypes.c_uint()
            if lib.nvmlDeviceGetPcieThroughput(h, counter, ctypes.byref(v)) == 0:
                out[key] = v.value * 1024.0
        for key, fn in (
            ("pcie_gen", "nvmlDeviceGetCurrPcieLinkGeneration"),
            ("pcie_width", "nvmlDeviceGetCurrPcieLinkWidth"),
            ("pcie_gen_max", "nvmlDeviceGetMaxPcieLinkGeneration"),
            ("pcie_width_max", "nvmlDeviceGetMaxPcieLinkWidth"),
        ):
            v = ctypes.c_uint()
            if getattr(lib, fn)(h, ctypes.byref(v)) == 0:
                out[key] = v.value
        return out
    except (OSError, AttributeError):
        return {}


def gpu_temp_nvml():
    try:
        if nvml["lib"] is None:
            gpu_fan_rpm()
        if nvml["lib"] is None:
            return None
        t = ctypes.c_uint()
        return float(t.value) if nvml["lib"].nvmlDeviceGetTemperature(nvml["h"], 0, ctypes.byref(t)) == 0 else None
    except (OSError, AttributeError):
        return None


def gpu():
    try:
        r = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total,fan.speed,power.draw,power.limit,temperature.gpu.tlimit,utilization.memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        n, t, u, mu, mt, fan, pw, pl, tl, mbw = [x.strip() for x in r.stdout.splitlines()[0].split(",")]
        num = lambda v: float(v) if v.replace(".", "", 1).lstrip("-").isdigit() else None
        return {
            "name": n,
            "temp": float(t),
            "util": float(u),
            "mem_used": float(mu) * 1e6,
            "mem_total": float(mt) * 1e6,
            "fan_pct": num(fan),
            "fan_rpm": gpu_fan_rpm(),
            "power_w": num(pw),
            "power_limit_w": num(pl),
            "tlimit_margin": num(tl),
            "mem_util": num(mbw),
        } | gpu_pcie()
    except Exception:
        return None


def edac():
    ce = ue = 0
    for m in glob.glob("/sys/devices/system/edac/mc/mc*"):
        ce += int(read(m + "/ce_count", "0"))
        ue += int(read(m + "/ue_count", "0"))
    return {"ce": ce, "ue": ue}


def processes():
    r = subprocess.run(["ps", "-eo", "pid,etimes,stat,pcpu,args", "--no-headers"], capture_output=True, text=True).stdout
    out, dstate = {}, 0
    for line in r.splitlines():
        f = line.split(None, 4)
        if len(f) < 5 or "agent.py" in f[4]:
            continue
        if f[2].startswith("D"):
            dstate += 1
        for k, pat in WATCH.items():
            if re.search(pat, f[4]):
                e = out.setdefault(k, {"count": 0, "cpu": 0.0, "oldest_s": 0, "sample": f[4][:160]})
                e["count"] += 1
                e["cpu"] += float(f[3])
                e["oldest_s"] = max(e["oldest_s"], int(f[1]))
                e["stopped"] = e.get("stopped", 0) + f[2].startswith("T")
    return out, dstate


KERR = re.compile(
    r"I/O error|critical medium error|medium error|unrecovered read|exception Emask|failed command|"
    r"nvme.*(timeout|reset)|resetting controller|megaraid.*(error|fault)|sense key",
    re.I,
)


def smart_parse(j, host, dev, rc):
    temp = j.get("temperature") or {}
    d = {
        "host": host,
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
            dcorr={k: (s.get(k) or {}).get("errors_corrected_by_eccdelayed", 0) for k in ("read", "write", "verify")},
        )
    return d


def smart_run(cmd, host, dev):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        return smart_parse(json.loads(r.stdout or "{}"), host, dev, r.returncode)
    except Exception as e:
        return {"host": host, "dev": dev, "error": str(e)[:160]}


def kernel_errors():
    try:
        r = subprocess.run(
            ["sudo", "-n", "journalctl", "-k", "--since", "-1h", "-o", "cat", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return None
    out = {}
    for line in r.stdout.splitlines():
        if KERR.search(line):
            m = re.search(r"\b(sd[a-z]+|nvme\d+(?:n\d+)?|ata\d+(?:\.\d+)?)\b", line)
            k = m.group(1) if m else "other"
            out[k] = out.get(k, 0) + 1
    return out


def health_loop():
    while True:
        t0 = time.time()
        if not os.path.exists(SMARTCTL):
            h = {"at": t0, "error": f"{SMARTCTL} missing (copy it back after a reboot)", "drives": []}
        else:
            ids = []
            try:
                scan = subprocess.run(["sudo", "-n", SMARTCTL, "--scan"], capture_output=True, text=True, timeout=30).stdout
                ids = sorted({int(m) for m in re.findall(r"-d megaraid,(\d+)", scan)})
            except Exception:
                pass
            drives = [
                smart_run(["sudo", "-n", SMARTCTL, "-j", "-a", "-d", f"megaraid,{i}", "/dev/bus/0"], HOST_KEY, f"bay {i}")
                for i in ids
            ]
            h = {"at": t0, "drives": drives, "kernel": kernel_errors()}
        with lock:
            state["health"] = h
        time.sleep(max(60, HEALTH_EVERY - (time.time() - t0)))


def ipmi_sensors(prefix):
    out = {"at": time.time(), "fans": [], "temps": [], "redundancy": None, "power_w": None}
    for typ in ("Fan", "Temperature", "Current"):
        try:
            r = subprocess.run(prefix + ["sdr", "type", typ], capture_output=True, text=True, timeout=20).stdout
        except Exception as e:
            out["error"] = str(e)[:120]
            continue
        for line in r.splitlines():
            f = [x.strip() for x in line.split("|")]
            if len(f) < 5:
                continue
            name, _, st, ent, val = f[:5]
            m = re.match(r"([\d.]+)\s*(RPM|degrees C|Watts)", val)
            if typ == "Fan":
                if m and m.group(2) == "RPM":
                    out["fans"].append({"name": name.replace(" RPM", ""), "rpm": float(m.group(1)), "ok": st == "ok"})
                elif "Redundan" in name:
                    out["redundancy"] = val
            elif typ == "Temperature" and m:
                label = {"3.1": "CPU1", "3.2": "CPU2"}.get(ent, name) if name == "Temp" else name.replace(" Temp", "")
                out["temps"].append({"name": label, "c": float(m.group(1)), "ok": st == "ok"})
            elif typ == "Current" and m and m.group(2) == "Watts":
                out["power_w"] = float(m.group(1))
    return out


SERVER_FAN_EVERY = 5  # server fans/temps every 5 s, re-read 0/3/8 s after fan-control changes the set %; GPU fan/temp every 1 s; always the latest reading


def ipmi_temps(txt):
    out = []
    for line in txt.splitlines():
        f = [x.strip() for x in line.split("|")]
        m = re.match(r"([\d.]+)\s*degrees C", f[4]) if len(f) >= 5 else None
        if m:
            label = {"3.1": "CPU1", "3.2": "CPU2"}.get(f[3], f[0]) if f[0] == "Temp" else f[0].replace(" Temp", "")
            out.append((label, float(m.group(1)), f[2] == "ok"))
    return out


def psu_read(prefix, cache):
    out = {1: {}, 2: {}}
    try:
        for typ in ("Voltage", "Current", "Power Supply"):
            r = subprocess.run(prefix + ["-S", cache, "sdr", "type", typ], capture_output=True, text=True, timeout=10).stdout
            st = 0
            for line in r.splitlines():
                f = [x.strip() for x in line.split("|")]
                if len(f) < 5:
                    continue
                m = re.match(r"(Voltage|Current) ([12])$", f[0])
                if m:
                    n = int(m.group(2))
                    v = re.match(r"([\d.]+)\s*(Volts|Amps)", f[4])
                    out[n]["v" if m.group(1) == "Voltage" else "a"] = float(v.group(1)) if v else None
                elif typ == "Power Supply" and f[0] == "Status":
                    st += 1
                    if st in out:
                        out[st]["status"] = f[4]
        return {f"ps{n}": d for n, d in out.items()}
    except (subprocess.TimeoutExpired, OSError):
        return None


def fan_loop(prefix, cache, gpu_fan=None, gpu_temp=None):
    srv, it, hw, gf, gt, last_srv = deque(), deque(), deque(), deque(), deque(), 0
    psu_now = None
    set_pct, resample = None, []
    while True:
        t0 = time.time()
        try:
            try:
                with open("/dev/shm/fan-control.json") as f:
                    pct = json.load(f).get("fan_pct")
            except (OSError, ValueError):
                pct = set_pct
            if pct != set_pct:
                if set_pct is not None:
                    resample = [t0, t0 + 3, t0 + 8]
                set_pct = pct
            due = [x for x in resample if x <= t0]
            if due:
                resample = [x for x in resample if x > t0]
                last_srv = 0
            if t0 - last_srv >= SERVER_FAN_EVERY:
                last_srv = t0
                if not os.path.exists(cache):
                    subprocess.run(prefix + ["sdr", "dump", cache], capture_output=True, timeout=60)
                r = subprocess.run(
                    prefix + ["-S", cache, "sdr", "type", "Fan"], capture_output=True, text=True, timeout=10
                ).stdout
                rpm = {}
                for line in r.splitlines():
                    f = [x.strip() for x in line.split("|")]
                    m = re.match(r"([\d.]+)\s*RPM", f[4]) if len(f) >= 5 else None
                    if m:
                        rpm[f[0].replace(" RPM", "")] = (float(m.group(1)), f[2] == "ok")
                if rpm:
                    srv.append((t0, rpm))
                psu_now = psu_read(prefix, cache)
                r = subprocess.run(
                    prefix + ["-S", cache, "sdr", "type", "Temperature"], capture_output=True, text=True, timeout=10
                ).stdout
                tl = ipmi_temps(r)
                if tl:
                    it.append((t0, tl))
                hw.append((t0, temps()))
            for q, fn in ((gf, gpu_fan), (gt, gpu_temp)):
                v = fn() if fn else None
                if v is not None:
                    q.append((t0, v))
            for h in (srv, it, hw, gf, gt):
                while len(h) > 1:
                    h.popleft()
            fans = [{"name": n, "rpm": v, "ok": ok, "at": srv[-1][0]} for n, (v, ok) in srv[-1][1].items()] if srv else None
            itemps = [{"name": n, "c": v, "ok": ok} for n, v, ok in it[-1][1]] if it else None
            hwlatest = hw[-1][1] if hw else None
            with lock:
                state["fanlatest"] = {
                    "at": t0,
                    "fans": fans,
                    "ipmi_temps": itemps,
                    "temps": hwlatest,
                    "gpu_rpm": gf[-1][1] if gf else None,
                    "gpu_temp": gt[-1][1] if gt else None,
                    "server_every_s": SERVER_FAN_EVERY,
                    "gpu_every_s": 1,
                }
                if isinstance(state.get("ipmi"), dict):
                    if psu_now:
                        state["ipmi"]["psu"] = dict(psu_now, at=t0)
                    if fans:
                        state["ipmi"]["fans"] = fans
                    if itemps:
                        state["ipmi"]["temps"] = itemps
        except Exception as e:
            print("fan_loop:", e, flush=True)
        time.sleep(max(0.2, 1 - (time.time() - t0)))


def ipmi_loop(prefix, check=None):
    while True:
        t0 = time.time()
        if check and not os.path.exists(check):
            v = {"at": t0, "error": f"{check} missing (copy ipmitool + libfreeipmi back after a reboot)"}
        else:
            v = ipmi_sensors(prefix)
        with lock:
            fm = state.get("fanlatest")
            if fm and t0 - fm["at"] < 15:
                if fm.get("fans"):
                    v["fans"] = fm["fans"]
                if fm.get("ipmi_temps"):
                    v["temps"] = fm["ipmi_temps"]
            state["ipmi"] = v
        time.sleep(max(5, 30 - (time.time() - t0)))


PW_SAMPLES = {"q": deque(maxlen=330), "gpu": deque(maxlen=330), "started": False}


def power_sampler(prefix, gpu_mw=None):
    while True:
        t0 = time.time()
        r = dcmi_read(prefix)
        if r and r.get("w") is not None:
            PW_SAMPLES["q"].append((t0, r["w"]))
        if gpu_mw:
            try:
                v = gpu_mw()
                if v is not None:
                    PW_SAMPLES["gpu"].append((t0, v))
            except Exception:
                pass
        time.sleep(max(0.2, 1 - (time.time() - t0)))


def dcmi_power(prefix, gpu_mw=None):
    if not PW_SAMPLES["started"]:
        PW_SAMPLES["started"] = True
        threading.Thread(target=power_sampler, args=(prefix, gpu_mw), daemon=True).start()
    q = list(PW_SAMPLES["q"])
    if not q:
        return None
    now = time.time()
    win = lambda s, qq=q: [w for t, w in qq if now - t <= s]
    g = list(PW_SAMPLES["gpu"])
    gw = lambda s: [w for t, w in g if now - t <= s]
    return {
        "t": q[-1][0],
        "w": q[-1][1],
        "peak_5s": max(win(5) or [q[-1][1]]),
        "max": max(win(60) or [q[-1][1]]),
        "avg": sum(win(60) or [q[-1][1]]) / len(win(60) or [1]),
        "min": min(win(60) or [q[-1][1]]),
        "samples_60s": len(win(60)),
        "max_5m": max(win(300) or [q[-1][1]]),
        "gpu_w": g[-1][1] if g else None,
        "gpu_peak_5s": max(gw(5)) if gw(5) else None,
        "every_s": 1,
    }


def gpu_power_mw():
    if nvml["lib"] is None:
        gpu_fan_rpm()
    if nvml["lib"] is None:
        return None
    v = ctypes.c_uint()
    return v.value / 1000 if nvml["lib"].nvmlDeviceGetPowerUsage(nvml["h"], ctypes.byref(v)) == 0 else None


def dcmi_read(prefix):
    try:
        r = subprocess.run(prefix + ["dcmi", "power", "reading"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    grab = lambda k: (lambda m: float(m.group(1)) if m else None)(re.search(k + r"[^:]*:\s*([\d.]+)\s*Watts", r))
    w = grab("Instantaneous power reading")
    return (
        {
            "t": time.time(),
            "w": w,
            "avg": grab("Average power reading"),
            "min": grab("Minimum during"),
            "max": grab("Maximum during"),
        }
        if w is not None
        else None
    )


IPMI_PREFIX = ["sudo", "-n", "env", "LD_LIBRARY_PATH=/dev/shm/tools/lib", "/dev/shm/tools/ipmitool"]
PW = {
    "last": None,
    "low_since": None,
    "prev_stat": None,
    "prev_thr": None,
    "thr_hist": deque(maxlen=120),
    "sel_at": 0,
    "sel": None,
    "psu_rating": None,
}
LOW_MHZ, LOW_SECS, BUSY = 1800, 30, 0.70
PSU_RE = re.compile(r"power supply|redundan|over.?current|over.?power|power cap|throttl|current|failure", re.I)


def cpu_busy():
    cur = {}
    for line in open("/proc/stat"):
        if line.startswith("cpu") and line[3].isdigit():
            f = line.split()
            v = list(map(int, f[1:9]))
            cur[int(f[0][3:])] = (sum(v), v[3] + v[4])
    prev, PW["prev_stat"] = PW["prev_stat"], cur
    if not prev:
        return {}
    return {c: 1 - (idle - prev[c][1]) / max(1, tot - prev[c][0]) for c, (tot, idle) in cur.items() if c in prev}


def pkg_watts():
    """Actual CPU package watts from intel-rapl energy_uj deltas (root-only files, one sudo cat per call; Power and Jobs
    Sizing 2026-09-29: a RAPL throttle only binds below actual draw, so the guard needs the real package watts)."""
    zs = sorted(glob.glob("/sys/class/powercap/intel-rapl:[0-9]"))
    if not zs:
        return None
    if "rapl_rng" not in PW:
        out = subprocess.run(
            ["sudo", "-n", "cat"] + [z + "/max_energy_range_uj" for z in zs], capture_output=True, text=True, timeout=5
        ).stdout.split()
        PW["rapl_rng"] = [int(x) for x in out] if len(out) == len(zs) else None
        PW["rapl_names"] = [read(z + "/name") or os.path.basename(z) for z in zs]
    out = subprocess.run(
        ["sudo", "-n", "cat"] + [z + "/energy_uj" for z in zs], capture_output=True, text=True, timeout=5
    ).stdout.split()
    if len(out) != len(zs) or not PW.get("rapl_rng"):
        return None
    now, vals = time.time(), [int(x) for x in out]
    prev, PW["rapl_prev"] = PW.get("rapl_prev"), (now, vals)
    if not prev or now - prev[0] < 1:
        return PW.get("pkg_w_last")
    dt = now - prev[0]
    PW["pkg_w_last"] = {
        n: round(((v - p) % r) / dt / 1e6, 1) for n, v, p, r in zip(PW["rapl_names"], vals, prev[1], PW["rapl_rng"])
    }
    PW["pkg_w_last"]["window_s"] = round(dt, 1)
    return PW["pkg_w_last"]


def power_watch():
    now = time.time()
    busy = cpu_busy()
    freqs = {}
    busy_ids = [c for c, u in busy.items() if u >= BUSY]
    for _ in range(5):
        for c in busy_ids:
            try:
                freqs[c] = max(
                    freqs.get(c, 0), int(open(f"/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_cur_freq").read()) / 1000
                )
            except (OSError, ValueError):
                pass
        time.sleep(0.06)
    hot = sorted(freqs[c] for c, u in busy.items() if u >= BUSY and c in freqs)
    med = hot[len(hot) // 2] if hot else None
    if med is not None and len(hot) >= 4 and med < LOW_MHZ:
        PW["low_since"] = PW["low_since"] or now
    else:
        PW["low_since"] = None
    thr = 0
    for f in glob.glob("/sys/devices/system/cpu/cpu*/thermal_throttle/package_throttle_count"):
        try:
            thr += int(open(f).read())
        except (OSError, ValueError):
            pass
    PW["thr_hist"].append((now, thr))
    old = next((v for t, v in PW["thr_hist"] if now - t <= 70), thr)
    gpu_thr = {}
    try:
        r = (
            subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=clocks_throttle_reasons.hw_slowdown,clocks_throttle_reasons.hw_power_brake_slowdown,"
                    "clocks_throttle_reasons.hw_thermal_slowdown,clocks_throttle_reasons.sw_thermal_slowdown,clocks.sm",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            .stdout.strip()
            .split(",")
        )
        keys = ("hw_slowdown", "hw_power_brake", "hw_thermal", "sw_thermal")
        gpu_thr = {k: r[i].strip() == "Active" for i, k in enumerate(keys) if i < len(r)}
        gpu_thr["sm_mhz"] = float(r[4]) if len(r) > 4 and r[4].strip().replace(".", "").isdigit() else None
    except (subprocess.TimeoutExpired, OSError, ValueError, IndexError):
        pass
    if now - PW["sel_at"] > 300:
        PW["sel_at"] = now
        try:
            if PW["psu_rating"] is None:
                fru = subprocess.run(IPMI_PREFIX + ["fru"], capture_output=True, text=True, timeout=60).stdout
                PW["psu_rating"] = [int(m) for m in re.findall(r"PWR SPLY,(\d+)W", fru)] or []
            sdr = subprocess.run(IPMI_PREFIX + ["sdr", "type", "Power Supply"], capture_output=True, text=True, timeout=20).stdout
            psus = [
                {"name": f[0], "status": f[4]}
                for f in ([x.strip() for x in l.split("|")] for l in sdr.splitlines())
                if len(f) >= 5 and f[0] == "Status"
            ]
            out = subprocess.run(IPMI_PREFIX + ["sel", "elist"], capture_output=True, text=True, timeout=60).stdout
            info = subprocess.run(IPMI_PREFIX + ["sel", "info"], capture_output=True, text=True, timeout=30).stdout
            ev = []
            for line in out.splitlines():
                f = [x.strip() for x in line.split("|")]
                if len(f) < 5 or not PSU_RE.search(" ".join(f[3:])):
                    continue
                try:
                    ts = time.mktime(time.strptime(f[1] + " " + f[2].rsplit(" ", 1)[0], "%m/%d/%Y %I:%M:%S %p"))
                    if f[2].endswith("UTC"):
                        ts -= time.timezone if not time.daylight else time.altzone
                except ValueError:
                    continue
                if now - ts < 7 * 86400:
                    ev.append({"id": f[0], "ts": ts, "sensor": f[3], "text": " | ".join(f[4:])})
            pct = (lambda m: int(m.group(1)) if m else None)(re.search(r"Percent Used\s*:\s*(\d+)", info))
            PW["sel"] = {"events": ev[-40:], "percent_used": pct, "psus": psus}
        except (subprocess.TimeoutExpired, OSError) as e:
            PW["sel"] = {"error": str(e)[:160]}
    dc = dcmi_power(IPMI_PREFIX, gpu_power_mw) or {}
    sel = PW["sel"] or {}
    live = {k: v for k, v in ((state.get("ipmi") or {}).get("psu") or {}).items() if isinstance(v, dict)}
    if live:  # 2026-09-28: a PSU delivering current counts as working even if the iDRAC still latches an old "Failure detected"
        healthy = sum(1 for v in live.values() if (v.get("v") or 0) > 0 and (v.get("a") or 0) > 0)
        flagged = [k for k, v in live.items() if (v.get("a") or 0) > 0 and "failure" in (v.get("status") or "").lower()]
    else:
        healthy = sum(
            1 for x in sel.get("psus") or [] if "failure" not in x["status"].lower() and "ac lost" not in x["status"].lower()
        )
        flagged = []
    rating = max(PW["psu_rating"] or [0]) or None
    PW["last"] = {
        "ts": now,
        "busy_cpus": len(hot),
        "median_busy_mhz": med,
        "low_clock_s": now - PW["low_since"] if PW["low_since"] else 0,
        "pkg_throttle_total": thr,
        "pkg_throttle_delta_60s": thr - old,
        "gpu_throttle": gpu_thr,
        "power": dc,
        "pkg_w": pkg_watts(),
        "psu_rating_w": rating,
        "psu_healthy": healthy,
        "psu_flagged": flagged,
        "capacity_w": rating * healthy if rating and healthy else None,
        "sel": sel,
    }


def power_watch_loop():
    while True:
        try:
            power_watch()
        except Exception as e:
            PW["last"] = {"ts": time.time(), "error": str(e)[:200]}
        time.sleep(5)


CAPS_URL = DASH_URL + "/api/powercaps"
CAPS = {"applied": None, "error": None}


def powercaps_loop():
    import urllib.request

    while True:
        try:
            with urllib.request.urlopen(CAPS_URL, timeout=4) as r:
                want = (json.loads(r.read() or b"{}") or {}).get(HOST_KEY) or {}
            uw = int(want["rapl_package_w"] * 1e6) if want.get("rapl_package_w") else None
            got = {}
            for z in sorted(glob.glob("/sys/class/powercap/intel-rapl:[0-9]")):
                for c in (0, 1):
                    f = f"{z}/constraint_{c}_power_limit_uw"
                    try:
                        cur = int(open(f).read())
                    except (OSError, ValueError):
                        continue
                    if uw and cur != uw:
                        subprocess.run(["sudo", "-n", "tee", f], input=str(uw), capture_output=True, text=True, timeout=10)
                        cur = int(open(f).read())
                    got[f"{os.path.basename(z)}/c{c}"] = cur / 1e6
            CAPS.update(
                applied={
                    "ts": time.time(),
                    "want_w": want.get("rapl_package_w"),
                    "limits_w": got,
                    "throttle": want.get("throttle"),
                },
                error=None,
            )
        except Exception as e:
            CAPS["error"] = str(e)[:160]
        time.sleep(10)  # 10 s (was 60): power-management throttles are short-lived (2026-09-29)


def quickcool_loop():
    import urllib.request

    while True:
        try:
            with urllib.request.urlopen(QC_URL, timeout=4) as r:
                d = json.loads(r.read() or b"{}")
            body = {
                "on": bool(d.get("on")),
                "pct": 80,
                "since": d.get("at"),
                "ts": time.time(),
                "by": "dashboard",
                "meeting": bool(d.get("meeting")),
            }
            tmp = QC_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(body, f)
            os.replace(tmp, QC_FILE)
        except Exception:
            pass
        time.sleep(10)


HOOK_DIR = "/dev/shm/power-hooks"  # 2026-09-28: jobs register here instead of polling /api/powertarget
LOW_PROFILES = ("lowpower", "normal", "daytime")


def fire_power_hooks(profile, reason="run profile change"):
    """Run every executable in HOOK_DIR as `<hook> down|up <profile>` (detached; output to hooks.log) and record the state in
    state.json so a job that starts later can read it. down = Low Power (pause GPU/heavy work), up = Adaptive/Unattended (resume)."""
    scale = "down" if profile in LOW_PROFILES else "up"
    try:
        os.makedirs(HOOK_DIR, exist_ok=True)
        with open(os.path.join(HOOK_DIR, "state.json.tmp"), "w") as f:
            json.dump({"scale": scale, "profile": profile, "at": time.time(), "reason": reason}, f)
        os.replace(os.path.join(HOOK_DIR, "state.json.tmp"), os.path.join(HOOK_DIR, "state.json"))
        with open(os.path.join(HOOK_DIR, "hooks.log"), "a") as lg:
            for h in sorted(glob.glob(os.path.join(HOOK_DIR, "*"))):
                if os.path.isfile(h) and os.access(h, os.X_OK) and not h.endswith((".json", ".log", ".tmp")):
                    lg.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {os.path.basename(h)} {scale} {profile} ({reason})\n")
                    lg.flush()
                    subprocess.Popen(
                        [h, scale, profile], stdout=lg, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True
                    )
    except Exception as e:
        print("power hooks:", e, flush=True)


def apply_profile(profile, at):
    if profile not in ("daytime", "unattended", "normal", "adaptive", "lowpower"):
        return False
    try:
        with open(RP_FILE) as f:
            cur = json.load(f)
    except (OSError, ValueError):
        cur = {}
    if (at or 0) <= (cur.get("dashboard_at") or 0):
        return True
    if cur.get("profile") != profile:
        for k in ("run_rpm", "pl", "card_fan"):
            cur.pop(k, None)
    changed = cur.get("profile") != profile
    cur.update(profile=profile, dashboard_at=at)
    try:
        with open(RP_FILE + ".tmp", "w") as f:
            json.dump(cur, f)
        os.replace(RP_FILE + ".tmp", RP_FILE)
    except PermissionError:
        r = subprocess.run(["sudo", "-n", "tee", RP_FILE], input=json.dumps(cur), capture_output=True, text=True, timeout=10)
        if r.returncode:
            raise OSError("sudo tee " + RP_FILE + ": " + r.stderr.strip()[:160])
    if changed:
        fire_power_hooks(profile)
    return True


def runprofile_loop():
    import urllib.request

    while True:
        try:
            with urllib.request.urlopen(RP_URL, timeout=4) as r:
                d = json.loads(r.read() or b"{}")
            by_host = (d.get("hosts") or {}).get(HOST_KEY) or {}
            if by_host.get("profile"):
                apply_profile(by_host["profile"], by_host.get("at"))
        except Exception:
            pass
        time.sleep(10)


CARVE = {"dirs": {}, "counts": {}}


def carve_scan():
    out, now = [], time.time()
    for d in sorted(glob.glob("/dev/shm/carve-*/")):
        d = d.rstrip("/")
        name = os.path.basename(d)
        if not os.path.exists(os.path.join(d, "photorec.pid")) and not os.path.exists(os.path.join(d, "run.log")):
            continue
        r = {"name": name}
        try:
            pid = int(open(os.path.join(d, "photorec.pid")).read().split()[0])
            st = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
            r.update(pid=pid, state=st[0], cpu_ticks=int(st[11]) + int(st[12]))
        except (OSError, ValueError, IndexError):
            r.update(pid=None, state=None)
        for f, k in (("photorec.log", "log_mtime"), ("photorec.ses", "ses_mtime")):
            try:
                r[k] = os.path.getmtime(os.path.join(d, f))
            except OSError:
                pass
        r["hold"] = os.path.exists(os.path.join(d, "HOLD"))
        try:
            lines = open(os.path.join(d, "run.log"), errors="replace").read().splitlines()
            r["started"] = next((l for l in lines if l.startswith("START")), None)
            r["exit"] = next((l for l in reversed(lines) if "photorec exit=" in l), None)
        except OSError:
            pass
        try:
            ml = open(os.path.join(d, "mover.log"), errors="replace").read().splitlines()
            r["moved_folders"] = sum(1 for l in ml if " moved recup" in l)
            r["mover_last"] = ml[-1][:200] if ml else None
            r["drained"] = any("buffer drained" in l for l in ml[-5:])
        except OSError:
            pass
        try:
            r["buffer_bytes"] = int(
                subprocess.run(["du", "-sb", os.path.join(d, "out")], capture_output=True, text=True, timeout=30).stdout.split()[
                    0
                ]
            )
        except (IndexError, ValueError, subprocess.TimeoutExpired, OSError):
            pass
        key = (r.get("log_mtime"),)
        c = CARVE["counts"].get(name)
        if c is None or c[0] != key:
            try:
                n = subprocess.run(
                    ["grep", "-c", "-F", "	 ", os.path.join(d, "photorec.log")], capture_output=True, text=True, timeout=120
                ).stdout.strip()
                CARVE["counts"][name] = c = (key, int(n or 0))
            except (ValueError, subprocess.TimeoutExpired, OSError):
                pass
        r["files_carved"] = c[1] if c else None
        prev = CARVE["dirs"].get(name)
        if prev and r.get("cpu_ticks") is not None and prev.get("cpu_ticks") is not None and prev.get("pid") == r.get("pid"):
            r["cpu_pct"] = (r["cpu_ticks"] - prev["cpu_ticks"]) / os.sysconf("SC_CLK_TCK") / max(1, now - prev["t"]) * 100
        r["t"] = now
        CARVE["dirs"][name] = r
        out.append(r)
    devs = {}
    try:
        for line in subprocess.run(
            ["lsblk", "-b", "-n", "-o", "NAME,SIZE,TYPE,MOUNTPOINT", "-l"], capture_output=True, text=True, timeout=10
        ).stdout.splitlines():
            f = line.split()
            if len(f) >= 3 and (f[2] == "disk" or f[0].startswith("carve-") or f[2] == "part"):
                devs[f[0]] = {"size": int(f[1]), "type": f[2], "mount": f[3] if len(f) > 3 else None}
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    return {"ts": now, "runs": out, "devs": devs}


def carve_loop():
    while True:
        try:
            c = carve_scan()
            CARVE["last"] = c
        except Exception as e:
            CARVE["last"] = {"ts": time.time(), "error": str(e)[:200]}
        time.sleep(60)


def archiver_progress():
    out = {}
    for p in glob.glob("/dev/shm/archiver/work/*.progress"):
        try:
            raw, files = read(p).split()[:2]
            out[os.path.basename(p)[: -len(".progress")]] = {"raw": int(raw), "files": int(files), "mtime": os.path.getmtime(p)}
        except (ValueError, OSError):
            pass
    return out


def log_tails():
    out = {}
    for p in LOGS:
        try:
            with open(p, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 8192))
                text = f.read().decode("utf-8", "replace").replace("\r", "\n")
            out[p] = [l for l in text.splitlines() if l.strip()][-6:]
        except OSError:
            pass
    return out


hub = {"conn": None}


def hub_write(host, report):
    if pymysql is None or not os.path.exists(HUB_CNF):
        return False
    try:
        if hub["conn"] is None:
            hub["conn"] = pymysql.connect(
                read_default_file=HUB_CNF,
                charset="utf8mb4",
                connect_timeout=5,
                read_timeout=10,
                write_timeout=10,
                autocommit=True,
            )
        c = hub["conn"].cursor()
        c.execute(
            "insert into host_reports(host, ts, report) values(%s, now(3), %s) "
            "on duplicate key update ts = values(ts), report = values(report)",
            (host, json.dumps(report, default=str)),
        )
        return True
    except Exception:
        try:
            hub["conn"].close()
        except Exception:
            pass
        hub["conn"] = None
        return False


def loop():
    pc, pd, pn, pt = cpu_times(), diskstats(), netbytes(), time.time()
    slow_at, slow = 0, {}
    while True:
        time.sleep(5)
        try:
            c, d, n, t = cpu_times(), diskstats(), netbytes(), time.time()
            ni = nic_info()
            dt = t - pt
            disks = {k: {"read": (d[k][0] - pd.get(k, d[k])[0]) / dt, "write": (d[k][1] - pd.get(k, d[k])[1]) / dt} for k in d}
            rx, tx = (n[0] - pn[0]) / dt, (n[1] - pn[1]) / dt
            mem = {l.split(":")[0]: int(l.split()[1]) * 1024 for l in read("/proc/meminfo").splitlines()}
            if t - slow_at > 30:
                slow = {"gpu": gpu(), "edac": edac(), "temps": temps()}
                slow_at = t
            procs, dcount = processes()
            fast = {
                "host": os.uname().nodename,
                "load": [float(x) for x in read("/proc/loadavg").split()[:3]],
                "cpu": 1 - (c[1] - pc[1]) / max(1, c[0] - pc[0]),
                "ncpu": os.cpu_count(),
                "mem": {"total": mem["MemTotal"], "avail": mem["MemAvailable"]},
                "net": {"rx": rx, "tx": tx, "speed_mbit": sum(v["speed_mbit"] for v in ni.values()), "nics": ni},
                "disks": disks,
                "mounts": mounts(),
                "procs": procs,
                "dstate": dcount,
                "logs": log_tails(),
                "archiver": archiver_progress(),
                "drive_health": state["health"],
                "ipmi": state.get("ipmi"),
                "fan_control": (lambda t: json.loads(t) if t else None)(
                    read("/dev/shm/fan-control.json") or read("/run/fan-control.json")
                ),
                "gpu_run_mode": (lambda t: json.loads(t) if t else None)(read("/dev/shm/gpu-run-mode.json")),
                "quick_cool": (lambda t: json.loads(t) if t else None)(read(QC_FILE)),
                "gpu_jobs": [
                    dict(j, _key=re.sub(r"^gpu-job\.?|\.json$", "", os.path.basename(fp)) or "job")
                    for fp in sorted(glob.glob("/dev/shm/gpu-job*.json"))
                    for j in [(lambda t: json.loads(t) if t else None)(read(fp))]
                    if isinstance(j, dict)
                ],  # one feed per GPU job (2026-09-28)
                "run_profile": (lambda t: json.loads(t) if t else None)(read(RP_FILE)),
                "ram_bw": (lambda t: json.loads(t) if t else None)(read("/dev/shm/imc-bw.json")),
                "peak_power": (lambda t: json.loads(t) if t else None)(
                    read("/dev/shm/peak-power.json")
                ),  # iDRAC true 30-s peak (peak-power.py)
                "carve": CARVE.get("last"),
                "power_watch": PW.get("last"),
                "power_caps": CAPS,
                "power": dcmi_power(
                    ["sudo", "-n", "env", "LD_LIBRARY_PATH=/dev/shm/tools/lib", "/dev/shm/tools/ipmitool"], gpu_power_mw
                )
                if os.path.exists("/dev/shm/tools/ipmitool")
                else None,
                "uptime_s": float(read("/proc/uptime", "0 0").split()[0]),
                **slow,
            }
            fm = state.get("fanlatest") or {}
            if fast.get("gpu") and time.time() - fm.get("at", 0) < 15:
                fast["gpu"] = dict(
                    fast["gpu"], **{k: fm[f] for k, f in (("fan_rpm", "gpu_rpm"), ("temp", "gpu_temp")) if fm.get(f) is not None}
                )
            if fm.get("temps") and time.time() - fm.get("at", 0) < 15:
                fast["temps"] = fm["temps"]
            sample = {
                "t": t,
                "rx": rx,
                "tx": tx,
                "cpu": fast["cpu"],
                "w": sum(v["write"] for v in disks.values()),
                "r": sum(v["read"] for v in disks.values()),
            }
            with lock:
                state["history"].append(sample)
                recent = list(state["history"])[-120:]
            fast["db"] = hub_write(HOST_KEY, fast | {"recent": recent})
            with lock:
                state["fast"] = fast
                state["updated"] = t
            pc, pd, pn, pt = c, d, n, t
        except Exception as e:
            with lock:
                state["fast"]["error"] = str(e)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        ok, msg = False, "not found"
        if self.path.split("?")[0] == "/runprofile":
            if self.client_address[0].removeprefix("::ffff:") != DASH_CLIENT:
                msg = "only the dashboard may set the run profile"
            else:
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    d = json.loads(self.rfile.read(n) if 0 < n <= 4096 else b"{}")
                    ok = apply_profile(d.get("profile"), float(d.get("at") or 0))
                    msg = "applied" if ok else "unknown profile"
                except (ValueError, OSError) as e:
                    msg = str(e)[:200]
        b = json.dumps({"ok": ok, "msg": msg}).encode()
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        with lock:
            b = json.dumps({"fast": state["fast"], "history": list(state["history"]), "updated": state["updated"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=health_loop, daemon=True).start()
    threading.Thread(target=quickcool_loop, daemon=True).start()
    try:  # publish the current profile at start, so hooks and jobs never miss a change made while the agent was down
        with open(RP_FILE) as f:
            _p = json.load(f).get("profile") or "adaptive"
        try:
            with open(os.path.join(HOOK_DIR, "state.json")) as f:
                _s = json.load(f)
        except (OSError, ValueError):
            _s = {}
        if _s.get("profile") != _p:
            fire_power_hooks(_p, "agent start: state was " + str(_s.get("profile")))
    except (OSError, ValueError):
        pass
    threading.Thread(target=runprofile_loop, daemon=True).start()
    threading.Thread(target=carve_loop, daemon=True).start()
    threading.Thread(target=power_watch_loop, daemon=True).start()
    if POWERCAPS:
        threading.Thread(target=powercaps_loop, daemon=True).start()
    threading.Thread(
        target=ipmi_loop,
        args=(["sudo", "-n", "env", "LD_LIBRARY_PATH=/dev/shm/tools/lib", "/dev/shm/tools/ipmitool"], "/dev/shm/tools/ipmitool"),
        daemon=True,
    ).start()
    threading.Thread(
        target=fan_loop,
        args=(
            ["sudo", "-n", "env", "LD_LIBRARY_PATH=/dev/shm/tools/lib", "/dev/shm/tools/ipmitool"],
            "/dev/shm/ipmi-sdr.cache",
            gpu_fan_rpm,
            gpu_temp_nvml,
        ),
        daemon=True,
    ).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
