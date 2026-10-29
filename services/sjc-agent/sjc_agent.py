#!/usr/bin/env python3
"""sjc-agent: one per server. Publishes this server's hardware and controller state to MQTT, runs commands sent to it.
The topic contract (the one source of truth, shared with the dashboard backend) is contracts/mqtt-topics.md.

Topics (all retained, JSON), under <prefix>/<host>/:
  status    "online" / "offline" (last will)
  idrac     fans, temperatures, PSUs                      every SJC_SENSOR_EVERY_S
  power     DCMI watts now + 5 s / 60 s / 5 min stats       every SJC_POWER_EVERY_S (published at sensor pace)
  gpu       nvidia-smi per card                             every SJC_GPU_EVERY_S
  safety    fan-control state, Emergency, manual-control guard
  runlevel  run profile, power throttle, power scaler
  system    CPU, memory, load, disk + network I/O, hwmon temps
Commands: <prefix>/<host>/cmd/<name> with {"id": "...", ...}; the reply goes to <prefix>/<host>/result/<id>.
  fans       {"pct": 20-100} or {"auto": true}
  gpu_limit  {"w": watts, "index": 0}
Fan and GPU commands are refused while fan-control is running: it owns them. When the agent does hold manual fan
control, it hands back to Dell auto when the lease runs out or any sensor reaches its Emergency temperature.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque

import paho.mqtt.client as mqtt

from sjc_hw import gpu, idrac, safety, system
from sjc_hw.config import settings as S

log = logging.getLogger("sjc-agent")
BASE = f"{S.prefix}/{S.host}"
latest: dict = {"idrac": {}, "gpu": None}
power_q: deque = deque(maxlen=int(300 / max(S.power_every_s, 0.2)) + 5)
manual = {"pct": None, "until": 0.0, "by": None, "released": None}   # the agent's own manual fan hold
lock = threading.Lock()


def pub(client: mqtt.Client, part: str, payload) -> None:
    client.publish(f"{BASE}/{part}", json.dumps(payload, default=str), qos=0, retain=True)


def power_stats() -> dict | None:
    q = list(power_q)
    if not q:
        return None
    now = time.time()
    win = lambda s: [w for t, w in q if now - t <= s] or [q[-1][1]]
    return {"t": q[-1][0], "w": q[-1][1], "peak_5s": max(win(5)), "max_60s": max(win(60)), "avg_60s": sum(win(60)) / len(win(60)),
            "min_60s": min(win(60)), "max_5m": max(win(300)), "every_s": S.power_every_s}


def release_manual(why: str) -> None:
    try:
        idrac.dell_auto()
        log.warning("manual fans released to Dell auto: %s", why)
    except Exception as e:
        log.error("dell auto failed (%s): %s", why, e)
    manual.update(pct=None, until=0.0, released={"at": time.time(), "why": why})


def guard_tick() -> None:
    """Manual-control safety: lease expiry, Emergency temperatures, or fan-control coming back all end a manual hold."""
    with lock:
        if manual["pct"] is None:
            return
        if time.time() >= manual["until"]:
            return release_manual("lease ran out")
        hot = safety.over_limit((latest["idrac"] or {}).get("temps") or [], latest["gpu"])
        if hot:
            return release_manual("emergency: " + ", ".join(hot))
        if safety.controller_running():
            manual.update(pct=None, until=0.0, released={"at": time.time(), "why": "fan-control took over"})


def loop(every: float, fn) -> None:
    def run():
        while True:
            t0 = time.time()
            try:
                fn()
            except Exception:
                log.exception("%s failed", fn.__name__)
            time.sleep(max(0.1, every - (time.time() - t0)))
    threading.Thread(target=run, name=fn.__name__, daemon=True).start()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sjc-agent-{S.host}")
    client.will_set(f"{BASE}/status", "offline", qos=1, retain=True)
    client.reconnect_delay_set(1, 30)

    def on_connect(c, _u, _f, rc, _p=None):
        log.info("mqtt connected (%s)", rc)
        c.publish(f"{BASE}/status", "online", qos=1, retain=True)
        c.subscribe(f"{BASE}/cmd/+", qos=1)

    def on_message(c, _u, msg):
        name = msg.topic.rsplit("/", 1)[-1]
        try:
            body = json.loads(msg.payload or b"{}")
        except ValueError:
            body = {}
        res = {"id": body.get("id"), "cmd": name, "at": time.time()} | command(name, body)
        if body.get("id"):
            c.publish(f"{BASE}/result/{body['id']}", json.dumps(res), qos=1)
        log.info("cmd %s %s -> %s", name, body, res.get("ok"))

    client.on_connect, client.on_message = on_connect, on_message
    client.connect_async(S.mqtt_host, S.mqtt_port, keepalive=30)
    client.loop_start()

    def sensors():
        v = idrac.sensors()
        latest["idrac"] = v
        pub(client, "idrac", v)
        pub(client, "power", power_stats())

    def power():
        r = idrac.dcmi()
        if r:
            power_q.append((r["t"], r["w"]))

    def gpus():
        latest["gpu"] = gpu.gpus()
        pub(client, "gpu", {"at": time.time(), "cards": latest["gpu"]})

    def safety_pub():
        guard_tick()
        pub(client, "safety", {"at": time.time(), "fan_control": safety.fan_control(), "emergency_c": safety.emergency_limits(),
                               "manual": dict(manual)})
        pub(client, "runlevel", {"at": time.time()} | safety.runlevel())

    def system_pub():
        pub(client, "system", system.snapshot())

    loop(S.sensor_every_s, sensors)
    loop(S.power_every_s, power)
    if S.nvidia_smi:
        loop(S.gpu_every_s, gpus)
    loop(S.safety_every_s, safety_pub)
    loop(S.system_every_s, system_pub)
    while True:
        time.sleep(3600)


def command(name: str, b: dict) -> dict:
    if name in ("fans", "gpu_limit") and safety.controller_running():
        return {"ok": False, "error": "fan-control is running and owns fans and GPU limits; use the run-mode controls"}
    try:
        if name == "fans":
            with lock:
                if b.get("auto"):
                    release_manual("requested")
                    return {"ok": True, "mode": "dell-auto"}
                pct = int(b.get("pct"))
                if not S.manual_min_pct <= pct <= 100:
                    return {"ok": False, "error": f"pct must be {S.manual_min_pct}-100"}
                hot = safety.over_limit((latest["idrac"] or {}).get("temps") or [], latest["gpu"])
                if hot:
                    return {"ok": False, "error": "a sensor is at its Emergency limit: " + ", ".join(hot)}
                idrac.set_fan_pct(pct)
                manual.update(pct=pct, until=time.time() + S.manual_lease_s, by=b.get("by"))
                return {"ok": True, "pct": pct, "until": manual["until"]}
        if name == "gpu_limit":
            idx, w = int(b.get("index") or 0), int(b.get("w"))
            card = next((g for g in latest["gpu"] or [] if int(g["index"] or 0) == idx), None)
            if not card:
                return {"ok": False, "error": f"no GPU {idx}"}
            lo, hi = card.get("power_min_w") or 0, card.get("power_max_w") or 10_000
            if not lo <= w <= hi:
                return {"ok": False, "error": f"w must be {lo:.0f}-{hi:.0f}"}
            gpu.set_power_limit(w, idx)
            return {"ok": True, "index": idx, "w": w}
        return {"ok": False, "error": f"unknown command {name}"}
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad arguments"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


if __name__ == "__main__":
    main()
