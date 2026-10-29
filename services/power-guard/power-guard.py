#!/usr/bin/env python3
"""power-guard.py - fast scale-DOWN guard for the hub + GPU server power target (Power and Jobs Sizing thread).

2026-09-29: "if peak usage exceeds the line by 50w scale back, if the total usage exceeds the line, scale back;
run as a Python every 10 seconds ... You can then pick it up on your next run and decide how to scale back up."
the owner: "we don't actually want to slow the jobs down. Instead we probably want adjust the CPU wattage ... the easiest
fastest way, also easy to undo." the owner: "when you wake up every 15 minutes if the CPUs have been throttled, you should
try and adjust the jobs and remove the throttling."

Lever: Dashboard / RunModes' throttle override (POST /api/throttle). Lower-only (effective cap = min(mode cap, ours)),
survives the mode-cap loops, and every throttle expires by itself (max 900 s), so a dead guard can't leave caps behind.
  Each step takes about step_w from EACH server, set below its ACTUAL draw so the cap binds (the owner: limit each
  server approximately equally; a cap only works below actual usage):
  GPU server: rapl_package_w = actual package W (max of the last ~60 s of Dashboard's /api/state pkg_w; ssh RAPL read as
        fallback) - step_w/2, two packages; floor srv_floor_pkg_w.
  hub: dcmi_w, the iDRAC whole-server cap (its BIOS locks RAPL) = current hub 5-s peak - step_w; floor hub_floor_w.

Every 10 s it reads /api/powertarget + /api/state and checks:
  PEAK  : same-moment combined 5-s peak > combined_w + 50    (GPU server alone: GPU server 5-s peak > <GPU_SERVER_KEY>_w + 50)
  TOTAL : combined 60-s average          > combined_w         (GPU server alone: GPU server 60-s avg  > <GPU_SERVER_KEY>_w)
On a trigger it tightens ONE step on BOTH servers at once (2026-09-29): the GPU server's next rapl step and the hub's
DCMI cap, about step_w from each. GPU server-only overs touch only the GPU server. A cooldown lets readings
catch up. It never loosens: it re-posts its caps every hold_repost_s so they stay until the 15-min sizing pass asks
owners to trim jobs and runs `power-guard.py clear`. If a throttle vanishes (cleared or expired), it resets that level.
It acts only in Adaptive/Unattended (scale == "up").

2026-10-01 (the owner, Adaptive 1 kW: "add in power adjustments for the GPU ... keep the existing cap but add the possibility of
adjusting the power down in order to maintain the power target ... take full advantage of the hardware"):
  - third lever: the GPU server GPU power limit (throttle host GPUK, gpu_w; applied by GPU server fan-control as a lower-only bound
    under the profile limit). Each over-step also takes gpu_step_w off the GPU, set below its actual draw; floor gpu_floor_w.
    An idle GPU (draw near the floor) is left alone.
  - it now also steps back UP: when the combined and GPU server peaks stay up_margin_w under their lines for up_hold_s, every cap
    rises one step (hub +step_w, GPU server +step_w/2 per package, GPU +gpu_step_w); a cap that reaches its normal value is
    cleared. (The 15-min sizing pass that used to clear caps ended 2026-09-30, so caps stayed at the floor all day.)

Runs on the hub: ~/power-guard (systemd power-guard.service, User=<user>).
Usage: power-guard.py run [--dry-run] | status | clear [<GPU_SERVER_KEY>|<HUB_KEY>|<GPU_THROTTLE_KEY>|all]
"""
import json, os, subprocess, sys, time, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "lib"))
from svcenv import env  # noqa: E402  (shared repo .env; see .env.example)

HERE = os.path.dirname(os.path.abspath(__file__))
CFG, STATE, LOG = (os.path.join(HERE, f) for f in ("power-guard.json", "state.json", "guard.log"))
API = env("HUB_API_URL", "http://localhost:8090")
# Host keys as the dashboard names them (its /api/throttle hosts, /api/powertarget "<key>_w"); site values in the env file
HUB = env("HUB_KEY", "hub")                      # DCMI whole-server cap
SRV = env("GPU_SERVER_KEY", "gpu-server")         # RAPL package caps
GPUK = env("GPU_THROTTLE_KEY", "gpu-server-gpu")  # GPU power limit
GPU_SERVER_SSH = env("GPU_SERVER_SSH", "")        # user@host for the fallback RAPL read; empty = no fallback
BY = "Power and Jobs Sizing (power-guard.py)"
SSH_KEY = os.path.expanduser(env("SSH_KEY", "~/.ssh/id_ed25519"))   # hub -> GPU server (<user>), read-only use: RAPL energy counters


def log(msg):
    line = time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + msg
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save(st):
    with open(STATE + ".tmp", "w") as f:
        json.dump(st, f, indent=1)
    os.replace(STATE + ".tmp", STATE)


def api(path, body=None):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"}, method="POST" if body is not None else "GET")
    raw = urllib.request.urlopen(req, timeout=8).read()
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace")


def readings():
    t, s = api("/api/powertarget"), api("/api/state")
    rf = (s.get("remote") or {}).get("fast") or {}
    l, r = (s.get("fast") or {}).get("power"), rf.get("power")
    if not isinstance(l, dict) or not isinstance(r, dict) or not isinstance(t, dict):
        raise LookupError(f"dashboard state incomplete ({HUB} or {SRV} power missing); skipping this cycle")
    pk = (rf.get("power_watch") or {}).get("pkg_w") or {}
    pkgs = [v for k, v in pk.items() if k.startswith("package") and isinstance(v, (int, float))]
    return {"scale": t.get("scale"), "combined_w": t["combined_w"], "srv_w": t[SRV + "_w"],
            "srv_pkg_w": max(pkgs) if pkgs else None,
            "comb_peak": l["peak_5s"] + r["peak_5s"], "comb_avg": l["avg"] + r["avg"],
            "srv_peak": r["peak_5s"], "srv_avg": r["avg"], "hub_peak": l["peak_5s"],
            "gpu_draw": ((rf.get("fan_control") or {}).get("gpu") or {}).get("draw"),
            "age": time.time() - min(l.get("t") or 0, r.get("t") or 0)}


def ours():
    """Active throttles set by this thread: GET /api/throttle -> {host: {..., by, until, left_s}}."""
    got = api("/api/throttle")
    return {h: v for h, v in (got if isinstance(got, dict) else {}).items()
            if isinstance(v, dict) and "Power and Jobs Sizing" in str(v.get("by", ""))}


def triggers(x, margin):
    out = []
    if x["comb_peak"] > x["combined_w"] + margin:
        out.append(("combined", f"PEAK {x['comb_peak']:.0f} > {x['combined_w']}+{margin}"))
    if x["comb_avg"] > x["combined_w"]:
        out.append(("combined", f"TOTAL avg60 {x['comb_avg']:.0f} > {x['combined_w']}"))
    if x["srv_peak"] > x["srv_w"] + margin:
        out.append((SRV, f"{SRV} PEAK {x['srv_peak']:.0f} > {x['srv_w']}+{margin}"))
    if x["srv_avg"] > x["srv_w"]:
        out.append((SRV, f"{SRV} TOTAL avg60 {x['srv_avg']:.0f} > {x['srv_w']}"))
    return out


def post(host, st, cfg, reason, dry):
    body = {"host": host, "seconds": cfg["hold_s"], "by": BY, "reason": reason[:200]}
    body[{SRV: "rapl_package_w", HUB: "dcmi_w", GPUK: "gpu_w"}[host]] = st["cap"][host]
    if not dry:
        api("/api/throttle", body)
    st["posted"][host] = time.time()


def srv_pkg_w():
    """Actual GPU server CPU package watts (max of its packages), from intel-rapl energy over 1 s, via ssh with the hub key."""
    cmd = ("a=$(sudo -n cat /sys/class/powercap/intel-rapl:[0-9]/energy_uj | tr '\\n' ' '); sleep 1; "
           "b=$(sudo -n cat /sys/class/powercap/intel-rapl:[0-9]/energy_uj | tr '\\n' ' '); echo \"$a|$b\"")
    p = subprocess.run(["ssh", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", GPU_SERVER_SSH, cmd],
                       capture_output=True, text=True, timeout=15)
    a, b = (list(map(int, part.split())) for part in p.stdout.strip().split("|"))
    return max((y - x0) / 1e6 for x0, y in zip(a, b) if y >= x0)


def tighten(cfg, st, x, scope, reason, dry):
    """One step down. A combined over steps BOTH servers at once, about the same watts from each (2026-09-29:
    "reducing the CPU wattage for both servers at the same time", "limit each server approximately equally"). Each cap
    is set below the server's ACTUAL draw so it binds: GPU server per-package cap = actual package W - step_w/2 (two
    packages), hub DCMI = its 5-s peak - step_w. An GPU server-only over steps only the GPU server."""
    done = []
    step = cfg["step_w"]
    cur = st["cap"].get(SRV)
    # actual GPU server package draw: max over the last ~60 s of Dashboard's pkg_w (Dashboard: the packages burst, so use
    # the max of recent samples so the cap lands below the bursts); fall back to a direct RAPL read over ssh
    actual = max(st.get("pkg_hist") or [0]) or None
    if actual is None:
        try:
            actual = srv_pkg_w()
        except Exception as e:
            log(f"{SRV} package read failed ({e!r}); stepping from the current cap")
    base = min(v for v in (cur, actual) if v is not None) if (cur or actual) else cfg["srv_floor_pkg_w"]
    want = max(cfg["srv_floor_pkg_w"], int(base - step / 2))
    if cur is None or want < cur:
        st["cap"][SRV] = want
        post(SRV, st, cfg, reason, dry)
        done.append(f"{SRV} CPU cap -> {want} W/pkg (actual {actual:.0f} W/pkg)" if actual else f"{SRV} CPU cap -> {want} W/pkg")
    if scope == "combined":
        cur = st["cap"].get(HUB)
        want = int(x["hub_peak"]) - step
        if cur is not None:
            want = min(want, cur - step)
        want = max(cfg["hub_floor_w"], want)
        if cur is None or want < cur:
            st["cap"][HUB] = want
            post(HUB, st, cfg, reason, dry)
            done.append(f"{HUB} server cap -> {want} W ({HUB} peak {x['hub_peak']:.0f})")
    gd = x.get("gpu_draw")                               # GPU: below its actual draw so the limit binds; skip an idle GPU
    # 2026-10-01: one GPU, long jobs -> the GPU is the LAST lever: only once the CPUs are throttled a good share of the
    # way to their floors (average of both, 0 = normal cap, 1 = floor), and with a smaller step than the CPUs
    def frac(h, normal, floor):
        c = st["cap"].get(h)
        return 0.0 if c is None else max(0.0, min(1.0, (normal - c) / max(1, normal - floor)))
    cpu_frac = (frac(HUB, cfg["hub_max_w"], cfg["hub_floor_w"]) + frac(SRV, cfg["srv_max_pkg_w"], cfg["srv_floor_pkg_w"])) / 2
    if gd is not None and gd > cfg["gpu_floor_w"] + cfg["gpu_step_w"] and cpu_frac >= cfg.get("gpu_after_cpu_frac", 0.5):
        cur = st["cap"].get(GPUK)
        want = int(min(cur or 1e9, gd) - cfg["gpu_step_w"])
        want = max(cfg["gpu_floor_w"], want)
        if want < (cur or cfg["gpu_max_w"]):
            st["cap"][GPUK] = want
            post(GPUK, st, cfg, reason, dry)
            done.append(f"{SRV} GPU cap -> {want} W (draw {gd:.0f} W, CPUs {cpu_frac:.0%} throttled)")
    if not done:
        log(f"AT FLOOR ({SRV if scope == SRV else 'both'}) and still over ({reason}); tell the owner")
        return None
    log(f"DOWN {'(dry-run) ' if dry else ''}{'; '.join(done)} ({reason})")
    return "+".join(h for h in (SRV, HUB, SRV + " GPU") if any(d.startswith(h + " ") for d in done))


def loosen(cfg, st, x, dry):
    """One step UP on every active cap (2026-10-01: use the hardware fully). A cap that reaches its normal value is
    cleared instead of re-posted."""
    done = []
    steps = {HUB: (cfg["step_w"], cfg["hub_max_w"], "server cap", " W"),
             SRV: (cfg["step_w"] / 2, cfg["srv_max_pkg_w"], "CPU cap", " W/pkg"),
             GPUK: (cfg["gpu_step_w"], cfg["gpu_max_w"], "cap", " W")}
    k = cfg.get("up_step_frac", 1.0)                     # up-steps smaller than down-steps (bursty load: avoid see-sawing)
    steps = {h: (v[0] * (1.0 if h == GPUK else k),) + v[1:] for h, v in steps.items()}   # GPU recovers at full step
    order = [GPUK] if st["cap"].get(GPUK) is not None else [HUB, SRV]   # GPU back to normal first (2026-10-01)
    for h in order:
        stp, mx, what, unit = steps[h]
        cur = st["cap"].get(h)
        if cur is None:
            continue
        want = int(cur + stp)
        name = SRV + " GPU" if h == GPUK else h
        if want >= mx:
            if not dry:
                api("/api/throttle", {"host": h, "clear": True, "by": BY})
            del st["cap"][h]; st["posted"].pop(h, None)
            done.append(f"{name} {what} released (back to normal {mx}{unit})")
        else:
            st["cap"][h] = want
            post(h, st, cfg, "stepping back up: under the line", dry)
            done.append(f"{name} {what} -> {want}{unit}")
    if done:
        log(f"UP {'(dry-run) ' if dry else ''}{'; '.join(done)} (peak {x['comb_peak']:.0f} <= {x['combined_w']}-{cfg['up_margin_w']} "
            f"for {cfg['up_hold_s']} s)")
    return bool(done)


def run(dry):
    cfg = load(CFG, None) or sys.exit(f"missing {CFG}")
    st = load(STATE, {})
    st.setdefault("cap", {}); st.setdefault("posted", {}); st.setdefault("downs", [])
    log(f"start {'DRY-RUN ' if dry else ''}every {cfg['interval_s']} s, peak margin {cfg['peak_margin_w']} W, "
        f"step {cfg['step_w']} W per server, floors {SRV} {cfg['srv_floor_pkg_w']} W/pkg {HUB} {cfg['hub_floor_w']} W, hold {cfg['hold_s']} s")
    last_down = last_ok = last_up = 0.0
    under_since = None
    while True:
        t0 = time.time()
        try:
            cfg = load(CFG, cfg)
            if not dry:   # a throttle that vanished (cleared by the sizing pass, or expired) resets our level
                act = ours()
                for h in list(st["cap"]):
                    if h not in act:
                        log(f"reset: {h} throttle no longer active (cleared or expired)")
                        del st["cap"][h]
                        st["posted"].pop(h, None)
            x = readings()
            if x.get("srv_pkg_w"):
                st["pkg_hist"] = (st.get("pkg_hist", []) + [x["srv_pkg_w"]])[-6:]
            if cfg.get("test_combined_w"):   # temporary test line (2026-09-29: "temporarily set the power cap at 1000 W to test")
                x["combined_w"] = cfg["test_combined_w"]
            if x["age"] > cfg["stale_s"]:
                log(f"skip: dashboard readings {x['age']:.0f} s old")
            elif x["scale"] == "up":
                trig = triggers(x, cfg["peak_margin_w"])
                cool = cfg["cooldown_s"] if all("PEAK" in r for _, r in trig) else cfg["cooldown_avg_s"]
                if trig and t0 - last_down >= cool:
                    scope = SRV if all(s == SRV for s, _ in trig) else "combined"
                    reason = "; ".join(r for _, r in trig)
                    host = tighten(cfg, st, x, scope, reason, dry)
                    st["downs"] = (st["downs"] + [{"at": int(t0), "host": host, "cap": dict(st["cap"]),
                                                   "reason": reason, "dry": dry}])[-300:]
                    last_down = t0
                under = (not trig and x["comb_peak"] <= x["combined_w"] - cfg["up_margin_w"]
                         and x["srv_peak"] <= x["srv_w"] - cfg["up_margin_w"])
                under_since = (under_since or t0) if under else None
                if (under and st["cap"] and t0 - under_since >= cfg["up_hold_s"] and t0 - last_down >= cfg["up_hold_s"]
                        and t0 - last_up >= cfg["up_hold_s"]):
                    if loosen(cfg, st, x, dry):
                        last_up = t0; under_since = t0
                if not trig and t0 - last_ok >= 600:
                    log(f"ok: comb peak {x['comb_peak']:.0f} avg {x['comb_avg']:.0f} / {x['combined_w']}; "
                        f"{SRV} peak {x['srv_peak']:.0f} avg {x['srv_avg']:.0f} / {x['srv_w']}; caps {st['cap'] or 'none'}")
                    last_ok = t0
            for h in list(st["cap"]):   # hold: keep our caps alive until the sizing pass clears them
                if t0 - st["posted"].get(h, 0) >= cfg["hold_repost_s"]:
                    post(h, st, cfg, "hold until the sizing pass trims jobs", dry)
            save(st)
        except Exception as e:
            log(f"error: {e!r}")
        time.sleep(max(1, cfg["interval_s"] - (time.time() - t0)))


def clear(which):
    for h in ([SRV, HUB, GPUK] if which == "all" else [which]):
        api("/api/throttle", {"host": h, "clear": True, "by": BY})
        log(f"CLEAR {h} throttle (sizing pass)")


def status():
    st = load(STATE, {})
    print(json.dumps({"readings": readings(), "throttles": api("/api/throttle"), "guard_caps": st.get("cap"),
                      "recent_downs": st.get("downs", [])[-10:]}, indent=1, default=str))


if __name__ == "__main__":
    a = sys.argv[1:] or ["status"]
    if a[0] == "run":
        run("--dry-run" in a)
    elif a[0] == "clear":
        clear(a[1] if len(a) > 1 else "all")
    else:
        status()
