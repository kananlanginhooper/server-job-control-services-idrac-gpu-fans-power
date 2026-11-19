#!/usr/bin/python3
# gpuq (owner, 2026-11-18; docs/gpu-scheduling.md): claim a GPU for work outside the queue (training, a llama-server).
# A claim is a JSON file in GPU_WORKER_CLAIMS_DIR/<gpu>/; that GPU's worker gives the GPU up while the claim is live and
# writes <gpu>/.state {"held_by": <id>, "free": true} once it has. Nothing here talks to the broker or a database.
#
#   gpuq hold --for "glyph-lora run 5" -- sudo -n docker run --rm --gpus all ...   claim, wait for the GPU, run, release
#   gpuq hold --for "235B blind decode" --pid 2434159                              live until that process exits
#   gpuq hold --for "maintenance" --until 2026-11-20T18:00-05:00                   live until a time
#   gpuq release --id <id>        gpuq status
from __future__ import annotations

import argparse
import getpass
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid

_here = os.path.dirname(os.path.realpath(__file__))
sys.path[:0] = [_here, os.path.join(_here, "..", "lib")]  # beside svcenv.py (/opt/gpu-worker) or run from the repo
from svcenv import env  # noqa: E402

HOST = env("GPU_WORKER_HOST", socket.gethostname().split(".")[0].lower())
CLAIMS_DIR = os.path.expanduser(env("GPU_WORKER_CLAIMS_DIR", ""))
DEFAULT_GPU = env("GPU_WORKER_GPU", f"{HOST}-gpu0")


def folder(gpu: str) -> str:
    if not CLAIMS_DIR:
        sys.exit("GPU_WORKER_CLAIMS_DIR is not set for this server")
    d = os.path.join(CLAIMS_DIR, gpu)
    os.makedirs(d, exist_ok=True)
    return d


def write_claim(gpu: str, claim: dict) -> str:
    path = os.path.join(folder(gpu), f"{claim['id']}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(claim, f)
    os.replace(tmp, path)
    return path


def state(gpu: str) -> dict:
    try:
        with open(os.path.join(folder(gpu), ".state"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def wait_free(gpu: str, cid: str, wait: float) -> bool:
    """True once the worker reports the GPU given up to this claim (its running work finished or stopped, Ollama unloaded)."""
    end = time.time() + wait
    while time.time() < end:
        st = state(gpu)
        if st.get("held_by") == cid and st.get("free"):
            return True
        time.sleep(2)
    return False


def cmd_hold(a: argparse.Namespace) -> int:
    cid = a.id or f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    claim = {"id": cid, "for": a.for_, "at": time.time(), "by": getpass.getuser()}
    if a.until:
        claim["until"] = a.until
    if a.command:
        claim["pid"] = os.getpid()  # live while this process runs the command
    elif a.pid:
        claim["pid"] = a.pid
    path = write_claim(a.gpu, claim)
    print(f"claim {cid} on {a.gpu}: {path}", flush=True)
    if not a.command:
        if a.no_wait:
            return 0
        ok = wait_free(a.gpu, cid, a.wait)
        print(f"{a.gpu} {'is free' if ok else f'not reported free within {a.wait:.0f} s (is its worker running?)'}", flush=True)
        return 0 if ok else 1
    try:
        if not a.no_wait and not wait_free(a.gpu, cid, a.wait):
            print(f"{a.gpu} not reported free within {a.wait:.0f} s; running anyway", file=sys.stderr, flush=True)
        p = subprocess.Popen(a.command)
        signal.signal(signal.SIGTERM, lambda *_: p.terminate())
        return p.wait()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
        print(f"claim {cid} released", flush=True)


def cmd_release(a: argparse.Namespace) -> int:
    path = os.path.join(folder(a.gpu), f"{a.id}.json")
    try:
        os.remove(path)
    except FileNotFoundError:
        print(f"no claim {a.id} on {a.gpu}", file=sys.stderr)
        return 1
    print(f"claim {a.id} released")
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    d = folder(a.gpu)
    print(json.dumps({"gpu": a.gpu, "state": state(a.gpu)}, indent=1))
    for f in sorted(os.listdir(d)):
        if f.endswith(".json"):
            with open(os.path.join(d, f), encoding="utf-8") as fh:
                print(f, fh.read().strip())
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gpuq", description="claim a GPU outside the work queue (docs/gpu-scheduling.md)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hold", help="claim the GPU; with a command: run it and release at its end")
    h.add_argument("--gpu", default=DEFAULT_GPU)
    h.add_argument("--for", dest="for_", required=True, help="who and why (shown in the worker's log)")
    h.add_argument("--id", help="claim id (default: time + random)")
    h.add_argument("--pid", type=int, help="live while this process runs (no command)")
    h.add_argument("--until", help="live until this time (ISO 8601 with offset)")
    h.add_argument("--wait", type=float, default=900, help="seconds to wait for the worker to give the GPU up")
    h.add_argument("--no-wait", action="store_true")
    h.add_argument("command", nargs=argparse.REMAINDER, help="-- command to run while holding the GPU")
    r = sub.add_parser("release", help="remove a claim")
    r.add_argument("--gpu", default=DEFAULT_GPU)
    r.add_argument("--id", required=True)
    s = sub.add_parser("status", help="the worker's state and the claim files")
    s.add_argument("--gpu", default=DEFAULT_GPU)
    a = ap.parse_args(argv)
    if a.cmd == "hold":
        if a.command and a.command[0] == "--":
            a.command = a.command[1:]
        if not a.command and not a.pid and not a.until:
            ap.error("hold needs a command, --pid or --until")
    return {"hold": cmd_hold, "release": cmd_release, "status": cmd_status}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
