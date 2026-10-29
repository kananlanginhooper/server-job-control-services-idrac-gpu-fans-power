#!/usr/bin/env python3
# Link pusher (SERVER-LINKS.md): watches this server's outboxes /link/<self>-<to> and pushes what is published there to
# the same folder on <to> with rsync over SSH (the <to>-link alias = rrsync-restricted link key), then removes the sent
# copies. inotify wakes it; a full sweep every LINK_SWEEP_S catches anything missed. One process per server, run as a
# continuous task-runner row. Writes a status JSON (per outbox: waiting files/bytes/age, last push, last error, rate).
from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "lib"))
from svcenv import env, env_int  # noqa: E402  (shared repo .env; see .env.example)

SELF = socket.gethostname().split(".")[0].lower()
ROOT = env("LINK_ROOT", "/link")
SWEEP_S = env_int("LINK_SWEEP_S", 300)
STATUS_FILE = os.path.expanduser(env("LINK_STATUS_FILE", "~/link-pusher/status.json"))
WARN_BYTES = int(float(env("LINK_WARN_GB", 50)) * 1e9)
WARN_AGE_S = int(float(env("LINK_WARN_AGE_H", 24)) * 3600)
IO_TIMEOUT_S = env_int("LINK_IO_TIMEOUT_S", 300)
BACKOFF_MIN, BACKOFF_MAX = 30, 900
NETWORK_FS = {"nfs", "nfs4", "cifs", "smb3", "fuse.sshfs", "9p"}
RSYNC_OK = (0, 24)  # 24 = some source files vanished before transfer (a consumer or producer moved them): not an error


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}", flush=True)


def parse_targets(spec: str) -> list[tuple[str, int]]:
    """'desk:1000 gpusrv:0' -> [('desk', 1000), ('gpusrv', 0)]; cap in KB/s, 0 = uncapped."""
    out = []
    for item in (spec or "").replace(",", " ").split():
        host, _, cap = item.partition(":")
        out.append((host.strip().lower(), int(cap or 0)))
    return out


def publishable(rel: str) -> bool:
    """A path inside an outbox that may be pushed: no dot component (.incoming, .rsync-partial, dotfiles), no .part."""
    parts = rel.replace("\\", "/").split("/")
    return not any(p.startswith(".") for p in parts if p) and not rel.endswith(".part")


def walk(top: str):
    """(relative path, size, mtime) of every publishable file under top; dot directories are not entered."""
    for d, dirs, files in os.walk(top):
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        for f in files:
            p = os.path.join(d, f)
            rel = os.path.relpath(p, top).replace("\\", "/")
            if not publishable(rel):
                continue
            try:
                st = os.lstat(p)
            except OSError:
                continue
            yield rel, st.st_size, st.st_mtime


def fs_type(path: str, mounts_text: str | None = None) -> str:
    """File system type of the mount that holds path (longest mount-point prefix in /proc/mounts)."""
    if mounts_text is None:
        try:
            with open("/proc/mounts") as f:
                mounts_text = f.read()
        except OSError:
            return ""
    best, typ = "", ""
    for line in mounts_text.splitlines():
        f = line.split()
        if len(f) < 3:
            continue
        mp = f[1].replace("\\040", " ")
        if (path == mp or path.startswith(mp.rstrip("/") + "/")) and len(mp) >= len(best):
            best, typ = mp, f[2]
    return typ


def rsync_stats(text: str) -> tuple[int, int]:
    """(files transferred, bytes transferred) from rsync --stats output."""
    files = re.search(r"Number of regular files transferred: ([\d,]+)", text)
    size = re.search(r"Total transferred file size: ([\d,]+)", text)
    num = lambda m: int(m.group(1).replace(",", "")) if m else 0
    return num(files), num(size)


def rsync_cmd(src: str, alias: str, cap: int, extra: list[str]) -> list[str]:
    cmd = [
        "rsync",
        "-a",
        "--stats",
        "--partial-dir=.rsync-partial",
        "--remove-source-files",
        "--chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r",
        f"--timeout={IO_TIMEOUT_S}",
    ]
    if cap > 0:
        cmd.append(f"--bwlimit={cap}")
    # rrsync on the receiver roots the key at its inbox, so the target is the inbox root "<to>-link:"
    return cmd + extra + [src.rstrip("/") + "/", f"{alias}:"]


class Outbox:
    def __init__(self, target: str, cap: int):
        self.target, self.cap = target, cap
        self.name = f"{SELF}-{target}"
        self.path = os.path.join(ROOT, self.name)
        self.alias = f"{target}-link"
        self.wake = threading.Event()
        self.fails = 0
        self.st = {
            "target": target,
            "cap_kbps": cap,
            "last_push_at": None,
            "last_ok_at": None,
            "last_error": None,
            "last_files": 0,
            "last_bytes": 0,
            "last_rate_Bps": None,
            "backoff_until": None,
            "pushing": False,
        }

    def push(self) -> bool:
        """Push everything published now. Batch markers (*.done) go last, in their own pass, and only those that
        existed before the first pass started, so a marker never arrives ahead of the files it covers."""
        typ = fs_type(os.path.realpath(self.path))
        if typ in NETWORK_FS:
            # e.g. the hub's /link/gpusrv-hub while it is still an NFS mount: --remove-source-files would delete the
            # only copy on the far side. Never push from a network mount.
            raise RuntimeError(f"{self.path} is on {typ}, not a local folder; refusing to push")
        markers = [rel for rel, _, _ in walk(self.path) if rel.endswith(".done")]
        t0 = time.time()
        base = ["--exclude=.*", "--exclude=*.part", "--exclude=*.done"]
        files, size = self._run(rsync_cmd(self.path, self.alias, self.cap, base))
        if markers:
            f2, s2 = self._run(rsync_cmd(self.path, self.alias, self.cap, ["--files-from=-"]), "\n".join(markers) + "\n")
            files, size = files + f2, size + s2
        dt = max(time.time() - t0, 0.001)
        self.st.update(last_files=files, last_bytes=size, last_rate_Bps=int(size / dt) if size else None)
        self._prune_empty_dirs()
        if files:
            log(f"{self.name}: pushed {files} files, {size / 1e6:.1f} MB in {dt:.0f}s ({size / dt / 1e6:.2f} MB/s)")
        return True

    def _run(self, cmd: list[str], stdin: str | None = None) -> tuple[int, int]:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
        if p.returncode not in RSYNC_OK:
            err = (p.stderr.strip().splitlines() or [f"exit {p.returncode}"])[-1]
            raise RuntimeError(f"rsync exit {p.returncode}: {err}")
        return rsync_stats(p.stdout)

    def _prune_empty_dirs(self) -> None:
        """--remove-source-files leaves the sent directories behind; drop empty ones untouched for 10 minutes."""
        cutoff = time.time() - 600
        for d, _dirs, _ in os.walk(self.path, topdown=False):
            if d == self.path or any(p.startswith(".") for p in os.path.relpath(d, self.path).split(os.sep)):
                continue
            try:
                if not os.listdir(d) and os.stat(d).st_mtime < cutoff:
                    os.rmdir(d)
            except OSError:
                pass

    def has_work(self) -> bool:
        return next(walk(self.path), None) is not None

    def loop(self, gate: threading.Semaphore) -> None:
        while True:
            if os.path.isdir(self.path) and self.has_work():
                with gate:
                    self._attempt()
            else:
                self.fails = 0
            if self.fails:
                # back-off: new files wait for the retry; nothing to gain hammering a link that is down
                time.sleep(max(self.st["backoff_until"] - time.time(), 1))
                continue
            self.wake.wait(timeout=SWEEP_S)  # inotify, or the periodic sweep
            time.sleep(2)  # let a burst of publishes land, then push them in one go
            self.wake.clear()

    def _attempt(self) -> None:
        self.st.update(pushing=True, last_push_at=time.time())
        try:
            self.push()
            self.fails = 0
            self.st.update(last_ok_at=time.time(), last_error=None, backoff_until=None)
        except Exception as e:
            self.fails += 1
            delay = min(BACKOFF_MIN * 2 ** (self.fails - 1), BACKOFF_MAX)
            self.st.update(last_error=str(e)[:500], backoff_until=time.time() + delay)
            log(f"{self.name}: push failed ({e}); retry in {delay}s")
        finally:
            self.st["pushing"] = False


def watch(outboxes: list[Outbox]) -> None:
    """inotifywait over every outbox; a finished file (close_write) or a publish rename (moved_to) wakes its pusher."""
    by_path = {ob.path.rstrip("/") + "/": ob for ob in outboxes}
    while True:
        paths = [ob.path for ob in outboxes if os.path.isdir(ob.path)]
        if not paths:
            time.sleep(60)
            continue
        try:
            p = subprocess.Popen(
                ["inotifywait", "-m", "-r", "-q", "-e", "close_write,moved_to", "--format", "%w%f", *paths],
                stdout=subprocess.PIPE,
                text=True,
            )
            for line in p.stdout:
                for prefix, ob in by_path.items():
                    if line.startswith(prefix) and publishable(line.strip()[len(prefix) :]):
                        ob.wake.set()
            p.wait()
        except FileNotFoundError:
            log("inotifywait not installed (apt install inotify-tools): relying on the periodic sweep")
            return
        log("inotifywait exited; restarting in 10s (the sweep covers the gap)")
        time.sleep(10)


def folder_stats(path: str) -> dict:
    n = size = 0
    oldest = None
    for _, sz, mt in walk(path):
        n, size = n + 1, size + sz
        oldest = mt if oldest is None else min(oldest, mt)
    return {"files": n, "bytes": size, "oldest_age_s": int(time.time() - oldest) if oldest else None}


def write_status(outboxes: list[Outbox]) -> None:
    now = time.time()
    st = {"host": SELF, "at": now, "outboxes": {}, "inboxes": {}}
    for ob in outboxes:
        s = dict(ob.st, **folder_stats(ob.path))
        s["warn"] = s["bytes"] > WARN_BYTES or (s["oldest_age_s"] or 0) > WARN_AGE_S
        st["outboxes"][ob.name] = s
    try:
        for d in sorted(os.listdir(ROOT)):
            if d.endswith(f"-{SELF}") and os.path.isdir(os.path.join(ROOT, d)):
                p = os.path.join(ROOT, d)
                st["inboxes"][d] = dict(folder_stats(p), fs=fs_type(os.path.realpath(p)))
    except OSError:
        pass
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATUS_FILE)


def main() -> None:
    targets = parse_targets(env("LINK_TARGETS", ""))
    if not targets:
        sys.exit(f"no LINK_TARGETS for {SELF} (set {SELF.upper()}_LINK_TARGETS in the repo .env)")
    serial = str(env("LINK_SERIAL", "0")).lower() in ("1", "true", "yes")
    outboxes = [Outbox(t, cap) for t, cap in targets]
    gate = threading.Semaphore(1 if serial else len(outboxes))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log(
        f"link-pusher on {SELF}: "
        + ", ".join(f"{ob.name} -> {ob.alias} cap {ob.cap or 'none'}" for ob in outboxes)
        + (" (one push at a time)" if serial else "")
    )
    threading.Thread(target=watch, args=(outboxes,), daemon=True).start()
    for ob in outboxes:
        threading.Thread(target=ob.loop, args=(gate,), daemon=True, name=ob.name).start()
    while True:
        try:
            write_status(outboxes)
        except Exception as e:
            log(f"status write failed: {e}")
        time.sleep(30)


if __name__ == "__main__":
    main()
