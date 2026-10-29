#!/usr/bin/env python3
"""GPU server GPU off switch (2026-09-30, the owner: "add a toggle to manually disable the GPU (no matter what mode is running)").
Polls the dashboard's /api/powertarget (gpu_off) every 15 s. Runs as root (systemd gpu-switch.service).
OFF: Copilot runner held (its HOLD file, tagged "gpu-switch") + graceful stop via the power hook, Ollama stopped (frees VRAM;
     the hub image runner waits without marking images). ON: only what this switch did is undone (a HOLD someone else set
     stays), Ollama started again, Copilot resumed through the hook only when the run mode is not scaled down.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "lib"))
from svcenv import env  # noqa: E402  (shared repo .env; see .env.example)

API = env("HUB_API_URL", "http://localhost:8090") + "/api/powertarget"
OLLAMA_UNIT = env("OLLAMA_UNIT", "ollama")
HOLD = "/dev/shm/copilot-runner/HOLD"
HOOK = "/dev/shm/power-hooks/copilot.sh"
LOG, STATE = "/dev/shm/gpu-switch.log", "/dev/shm/gpu-switch.json"


def log(m: str) -> None:
    with open(LOG, "a") as f:
        f.write(time.strftime("%FT%T ") + m + "\n")


def run(cmd: list[str]) -> int:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120).returncode
    except Exception as e:
        log(f"{cmd}: {e}")
        return -1


def main() -> None:
    applied = None
    log("start")
    while True:
        try:
            pt = json.load(urllib.request.urlopen(API, timeout=10))
            off = bool(pt.get("gpu_off"))
        except Exception:
            time.sleep(15)
            continue  # dashboard unreachable: leave the GPU as it is
        if off != applied:
            if off:
                if not os.path.exists(HOLD):
                    with open(HOLD, "w") as f:
                        f.write("gpu-switch: GPU turned off from the dashboard\n")
                if os.path.exists(HOOK):
                    run(["sudo", "-u", "kj", HOOK, "down", "gpuoff"])
                run(["systemctl", "stop", OLLAMA_UNIT])
                log("GPU OFF: Copilot held + stopped, Ollama stopped")
            else:
                try:
                    if open(HOLD).read().startswith("gpu-switch"):
                        os.remove(HOLD)
                except OSError:
                    pass
                run(["systemctl", "start", OLLAMA_UNIT])
                if pt.get("scale") != "down" and os.path.exists(HOOK) and not os.path.exists(HOLD):
                    run(["sudo", "-u", "kj", HOOK, "up", pt.get("mode") or "adaptive"])
                log("GPU ON: Ollama started" + ("" if not os.path.exists(HOLD) else "; Copilot HOLD set by someone else kept"))
            applied = off
            with open(STATE, "w") as f:
                json.dump({"off": off, "at": time.time()}, f)
        time.sleep(15)


if __name__ == "__main__":
    main()
