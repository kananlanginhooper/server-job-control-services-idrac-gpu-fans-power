#!/usr/bin/python3
# GPU worker (owner, 2026-10-30): one per GPU, run as a continuous task-runner row on the GPU's host. It takes work
# items from the hub's run broker (priority first, then standard, then backup; services/run-broker/workq.py), runs each as one Ollama
# call on this GPU, and hands the result back. It never touches a database: claim, beat, done, failed and release are the
# broker's HTTP routes, and the broker writes everything.
#
#   claim     long-poll with this GPU's models (own model first, then fallbacks) and what Ollama has loaded right now
#             (model + context, from /api/ps), so the broker can keep the loaded model busy instead of paying a reload
#   run       the item's payload {"api": "chat"|"generate", "body": {...}} with model, stream and keep_alive filled in;
#             a model that rejects `think` is retried without it. Streams, so a long answer reports progress (tokens)
#   queue     (2026-11-02) about GPU_WORKER_QUEUE_S seconds of items are claimed in one batch and held locally, so the GPU
#             runs them back to back instead of waiting on a round trip per item (the GPU desktop's Wi-Fi loses 15-40 %)
#   sync      every GPU_WORKER_SYNC_S: finished results go back in one call; every 10 s it also renews the leases of all
#             held items, and the broker answers which to drop (paused, cancelled, handed out again, a backup item giving
#             way): a running one is aborted (Ollama stops generating when the connection closes)
#   done      the full answer (content + thinking + Ollama's counters) as the result; stats carry load_s (Ollama's
#             load_duration) so the broker learns this GPU's model switch cost
#   Low Power (power target scale=down): no new claims; a running item finishes. Ollama unreachable (GPU switched off):
#             the item is released, not counted as a failure, and the worker waits.
from __future__ import annotations

import gzip
import http.client
import json
import os
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

_here = os.path.dirname(os.path.realpath(__file__))
sys.path[:0] = [_here, os.path.join(_here, "..", "..", "lib")]
from svcenv import env  # noqa: E402

HOST = env("GPU_WORKER_HOST", socket.gethostname().split(".")[0].lower())
BROKER = env("GPU_WORKER_BROKER_URL", env("TASKRUNNER_BROKER_URL", "")).rstrip("/")  # http://<hub>:8099/api/v1/agent
GPU = env("GPU_WORKER_GPU", f"{HOST}-gpu0")
MODELS = [m.strip() for m in env("GPU_WORKER_MODELS", "").split(",") if m.strip()]  # own model(s) first, then fallbacks
OLLAMA = env("GPU_WORKER_OLLAMA", "http://127.0.0.1:11434").rstrip("/")
# the queues this GPU takes (empty = all, in the broker's order); e.g. "backup" for a GPU that only takes low-return work
QUEUES = [q.strip() for q in env("GPU_WORKER_QUEUES", "").split(",") if q.strip()]
SLOTS = int(env("GPU_WORKER_SLOTS", "1"))  # parallel calls; >1 only with OLLAMA_NUM_PARALLEL to match
KEEP_ALIVE = env("GPU_WORKER_KEEP_ALIVE", "30m")
STALL_S = float(env("GPU_WORKER_STALL_S", "600"))  # no byte from Ollama this long: the runner is dead, retry the item
# 1 = every layer on the GPU or not at all (owner, 2026-10-30: a model spilling into system RAM is unusable here).
# Calls ask Ollama for all layers on the GPU (num_gpu 999), so a model that doesn't fit fails at load instead of
# running from RAM, and after each call the worker checks /api/ps that the model sits entirely in VRAM.
GPU_ONLY = env("GPU_WORKER_GPU_ONLY", "") not in ("", "0")
SPILL_BACKOFF_S = float(env("GPU_WORKER_SPILL_BACKOFF_S", "600"))  # after a spill, don't claim that model here for this long
POWER_URL = env("TASKRUNNER_POWER_URL", env("HUB_API_URL", "http://localhost:8090") + "/api/powertarget")
BEAT_S, WAIT_S = 10, 25
QUEUE_S = float(env("GPU_WORKER_QUEUE_S", "60"))  # seconds of claimed work held locally (short: priorities change)
SYNC_S = float(env("GPU_WORKER_SYNC_S", "2"))  # how often results go back (leases renew every BEAT_S)
MAX_BATCH = 20
SYNC_MAX = 15  # results per sync
RANK = {"priority": 0, "standard": 1, "backup": 2}  # the broker's queue order
YIELDING = ("backup",)  # a running item from these gives way to better work


def queue_order(it: dict) -> tuple:
    """The broker's order for the local queue: priority queue, then standard, then backup; within a queue the higher level first."""
    return RANK.get(it["queue"], 9), -(it.get("priority") or 0)


def relevel(queue: list[dict], levels: dict) -> int:
    """Take the levels the broker holds now (a submitter may change one after the claim; 2026-11-05: four items raised
    to the top level sat 47 min behind the stream at their old level). Returns how many changed."""
    n = 0
    for it in queue:
        p = levels.get(f"{it['queue']}/{it['id']}")
        if p is not None and p != it.get("priority"):
            it["priority"], n = p, n + 1
    return n


def log(msg: str) -> None:
    print(time.strftime("%F %T"), msg, flush=True)


# ---------- pure helpers (tests/test_gpu_worker.py) ----------
def request_body(item: dict, keep_alive: str = KEEP_ALIVE, gpu_only: bool = GPU_ONLY) -> tuple[str, dict]:
    """(Ollama path, body) for an item: the payload's body with model, stream and keep_alive set by the worker
    (and, on a GPU-only worker, every layer on the GPU)."""
    p = item.get("payload") or {}
    api = p.get("api") or "chat"
    if api not in ("chat", "generate"):
        raise ValueError(f"unknown api {api!r}")
    body = dict(p.get("body") or {}, model=item["model"], stream=True)
    body.setdefault("keep_alive", keep_alive)
    if gpu_only:
        body["options"] = dict(body.get("options") or {}, num_gpu=999)
    return f"/api/{api}", body


def spill(ps: list[dict], model: str) -> tuple[float, float] | None:
    """(GB in VRAM, GB total) when `model` is loaded but not entirely in VRAM, else None."""
    for m in ps:
        if m.get("name") == model or m.get("model") == model:
            vram, size = m.get("size_vram") or 0, m.get("size") or 0
            return (vram / 1e9, size / 1e9) if size and vram < size else None
    return None


def assemble(api: str, chunks: list[dict]) -> dict:
    """Streamed chunks -> the same shape Ollama returns without streaming (minus the token `context` array)."""
    text, thinking = [], []
    for c in chunks:
        src = (c.get("message") or {}) if api == "chat" else c
        text.append(src.get("content" if api == "chat" else "response") or "")
        thinking.append(src.get("thinking") or "")
    last = dict(chunks[-1]) if chunks else {}
    last.pop("context", None)
    if api == "chat":
        last["message"] = {"role": "assistant", "content": "".join(text), "thinking": "".join(thinking)}
    else:
        last["response"], last["thinking"] = "".join(text), "".join(thinking)
    return last


def stats(result: dict, ctx: int | None) -> dict:
    ns = 1e9
    return {
        "gpu": GPU,
        "ctx": ctx,
        "load_s": (result.get("load_duration") or 0) / ns,
        "total_s": (result.get("total_duration") or 0) / ns,
        "eval_s": (result.get("eval_duration") or 0) / ns,
        "prompt_tokens": result.get("prompt_eval_count"),
        "output_tokens": result.get("eval_count"),
        "done_reason": result.get("done_reason"),
    }


# ---------- the worker ----------
class Abort(Exception):
    pass


class Worker:
    def __init__(self, broker: str = BROKER, token: str | None = None) -> None:
        self.broker = broker
        self.token = env("AGENT_TOKEN", "") if token is None else token
        self.stop = threading.Event()
        self.current = set()  # "queue/id" of the items running now: released if the worker is stopped
        self.avoid: dict[str, float] = {}  # model -> time until which it isn't claimed here (it spilled into system RAM)
        self.conns = threading.local()  # one kept-alive broker connection per thread (slots, beat threads)
        self.qlock = threading.Lock()
        self.queue: list[dict] = []  # claimed, not started
        self.running: dict[str, dict] = {}  # ref -> its beat dict (progress, cancel, resp)
        self.outbox: list[dict] = []  # finished results waiting for the broker's acknowledgement
        self.ready, self.synced = threading.Event(), threading.Event()

    def call_broker(self, path: str, body: dict, timeout: float = 15) -> tuple[int, dict | None]:
        """POST to the broker over this thread's kept-alive connection (2026-11-02: a new TCP connection per call cost
        0.7-2.6 s over the GPU desktop's Wi-Fi). A connection the broker dropped while idle is reopened once."""
        u = urlsplit(self.broker)
        data = json.dumps(dict(body, gpu=GPU), default=str).encode()
        hdr = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        if len(data) > 2048:  # fewer packets to lose on the Wi-Fi link: JSON results compress 5-10x
            data, hdr["Content-Encoding"] = gzip.compress(data, 6), "gzip"
        for attempt in (1, 2):
            c = getattr(self.conns, "c", None)
            if c is None:
                c = self.conns.c = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
            c.timeout = timeout
            if c.sock is not None:
                c.sock.settimeout(timeout)
            try:
                c.request("POST", f"{u.path}/servers/{HOST}/work/{path}", body=data, headers=hdr)
                r = c.getresponse()
                raw = r.read()
                if (r.getheader("Connection") or "").lower() == "close":
                    c.close()
                    self.conns.c = None
                return r.status, (json.loads(raw) if raw and r.status == 200 else None)
            except (http.client.HTTPException, OSError) as e:
                c.close()
                self.conns.c = None
                if attempt == 2 or isinstance(e, TimeoutError):  # a timed-out call may have reached the broker: don't repeat it
                    raise
        raise OSError("unreachable")

    @staticmethod
    def low_power() -> bool:
        try:
            return json.load(urllib.request.urlopen(POWER_URL, timeout=5)).get("scale") == "down"
        except Exception:
            return False  # power API unreachable: don't hold work back (the guard still trims)

    @staticmethod
    def loaded() -> dict | None:
        """What Ollama has loaded now: {model, ctx} of the first loaded model, or None."""
        try:
            ps = json.load(urllib.request.urlopen(OLLAMA + "/api/ps", timeout=5)).get("models") or []
        except Exception:
            return None
        return {"model": ps[0]["name"], "ctx": ps[0].get("context_length") or 0} if ps else None

    @staticmethod
    def unload(model: str) -> None:
        try:
            body = json.dumps({"model": model, "keep_alive": 0}).encode()
            urllib.request.urlopen(urllib.request.Request(OLLAMA + "/api/generate", data=body, method="POST"), timeout=60).read()
        except Exception:
            pass

    def run(self, item: dict, beat: dict) -> dict:
        """One streamed Ollama call. beat['resp'] lets the beat thread abort it."""
        path, body = request_body(item)
        api = path.rsplit("/", 1)[1]
        for attempt in (1, 2):
            req = urllib.request.Request(
                OLLAMA + path, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"}
            )
            try:
                # the longest silence allowed (load + prefill, then between tokens); 2026-11-05 a CUDA crash in the
                # laptop's runner left the request open with no answer and the beats held 20 claims for 20 min
                resp = urllib.request.urlopen(req, timeout=STALL_S)
            except urllib.error.HTTPError as e:
                msg = e.read().decode(errors="replace")[:500]
                if e.code == 400 and "think" in msg and "think" in body and attempt == 1:
                    body.pop("think")  # a model without thinking
                    continue
                if e.code >= 500:  # out of memory, a runner crash: worth another attempt
                    raise OSError(f"Ollama {e.code}: {msg}") from e
                raise ValueError(f"Ollama {e.code}: {msg}") from e
            beat["resp"] = resp
            chunks, n = [], 0
            try:
                for line in resp:
                    if beat.get("cancel"):
                        raise Abort()
                    if not line.strip():
                        continue
                    c = json.loads(line)
                    if c.get("error"):
                        raise ValueError(f"Ollama: {c['error']}")
                    chunks.append(c)
                    n += 1
                    beat["progress"] = f"{n} tokens"
            except Exception as e:  # closing the response from the beat thread can surface as any error here
                if beat.get("cancel"):
                    raise Abort() from e
                raise
            finally:
                resp.close()
            if beat.get("cancel"):
                raise Abort()
            return assemble(api, chunks)
        raise ValueError("unreachable")

    # ---------- the local queue (2026-11-02) ----------
    # The GPU desktop reaches the hub over Wi-Fi with 15-40 % packet loss; a round trip per item left its GPU idle 2 s of
    # every 4-8 s. So the worker keeps ~QUEUE_S seconds of claimed items locally (one batch claim, all for the same model),
    # runs them back to back, and talks to the broker in one batched sync every SYNC_S: finished results in, leases renewed,
    # dropped items out. A lost round trip costs nothing but a retry: results wait in the outbox until acknowledged.

    def fetch_loop(self) -> None:
        delay = 2
        while not self.stop.is_set():
            try:
                with self.qlock:
                    held = len(self.queue) + len(self.running)
                # the broker sizes the batch: ~QUEUE_S seconds held, at each model's measured item time on this GPU,
                # and past that only work that outranks everything held (so asking while holding costs nothing)
                need = MAX_BATCH - held
                models = [m for m in MODELS if self.avoid.get(m, 0) <= time.time()]
                if self.low_power() or need <= 0 or not models:
                    self.stop.wait(1)
                    continue
                body = {
                    "models": models,
                    "queues": QUEUES or None,
                    "loaded": self.loaded(),
                    "n": need,
                    "queue_s": QUEUE_S,
                    "wait": 0 if held else WAIT_S,
                }
                code, r = self.call_broker("claim", body, timeout=WAIT_S + 15)
                if code == 200 and r:
                    with self.qlock:
                        for it in r.get("items") or []:
                            self.queue.append(it)
                        self.queue.sort(key=queue_order)
                        better = min((RANK.get(it["queue"], 9) for it in self.queue), default=9)
                        for ref, b in self.running.items():
                            if RANK.get(ref.split("/")[0], 9) > better and ref.split("/")[0] in YIELDING:
                                b["cancel"], b["yield"] = True, True  # a backup item gives way to better work it now holds
                                try:
                                    b["resp"].close()
                                except Exception:
                                    pass
                                log(f"{ref}: gives way to better work held locally")
                    self.ready.set()
                    log(
                        f"claimed {len(r.get('items') or [])} item(s) on {r['items'][0]['model'] if r.get('items') else '-'}; holding {held + len(r.get('items') or [])}"
                    )
                elif code != 204:
                    raise OSError(f"claim: HTTP {code}")
                elif held:
                    self.stop.wait(2)  # nothing more for now; the queue still has work
                delay = 2
            except Exception as e:
                log(f"claim: {type(e).__name__}: {e}".splitlines()[0])
                self.stop.wait(delay)
                delay = min(30, delay * 2)

    def run_loop(self, n: int) -> None:
        while not self.stop.is_set():
            with self.qlock:
                item = self.queue.pop(0) if self.queue else None
                if item:
                    ref = f"{item['queue']}/{item['id']}"
                    beat = {"progress": None}
                    self.running[ref] = beat
            if not item:
                self.ready.clear()
                self.ready.wait(1)
                continue
            self.run_one(item, ref, beat)

    def run_one(self, item: dict, ref: str, beat: dict) -> None:
        ctx = ((item.get("payload") or {}).get("body") or {}).get("options", {}).get("num_ctx")
        t0 = time.time()
        out = None
        try:
            result = self.run(item, beat)
            sp = None
            if GPU_ONLY:
                try:
                    sp = spill(
                        json.load(urllib.request.urlopen(OLLAMA + "/api/ps", timeout=5)).get("models") or [], item["model"]
                    )
                except Exception:
                    sp = None
            if sp:  # it ran partly from system RAM: not usable here. Unload it, hand the item back (uncounted) for another
                # GPU, along with the queued items for that model, and stop claiming the model here for SPILL_BACKOFF_S.
                # (2026-11-02: the laptop's free VRAM moves with Windows' own use, so one spill shouldn't cost the item.)
                self.unload(item["model"])
                out = {"release": True}
                with self.qlock:
                    self.avoid[item["model"]] = time.time() + SPILL_BACKOFF_S
                    back = [it for it in self.queue if it["model"] == item["model"]]
                    self.queue = [it for it in self.queue if it["model"] != item["model"]]
                    self.outbox += [{"queue": it["queue"], "id": it["id"], "release": True} for it in back]
                log(
                    f"{ref}: {item['model']} at ctx {ctx} doesn't fit fully on {GPU} ({sp[0]:.1f} of {sp[1]:.1f} GB in VRAM); "
                    f"released with {len(back)} queued, not claiming it for {SPILL_BACKOFF_S:.0f} s"
                )
            else:
                st = stats(result, ctx)
                out = {"result": result, "stats": st}
                dt = time.time() - t0
                log(f"{ref}: {item.get('study')} {item.get('step') or ''} done in {dt:.1f} s ({st['output_tokens']} tokens)")
        except Abort:
            # gave way locally: hand it back uncounted; dropped by the broker (paused, cancelled, handed out again): nothing to report
            out = {"release": True} if beat.get("yield") else None
        except urllib.error.URLError as e:  # Ollama down or the GPU switched off: not the item's fault
            out = {"release": True}
            log(f"{ref}: Ollama unreachable ({e.reason}); releasing")
            self.stop.wait(30)
        except Exception as e:
            out = {"error": f"{type(e).__name__}: {e}", "retry": isinstance(e, (OSError, TimeoutError))}
            log(f"{ref}: failed: {type(e).__name__}: {e}")
        finally:
            with self.qlock:
                self.running.pop(ref, None)
                if out is not None:
                    self.outbox.append(dict(out, queue=item["queue"], id=item["id"]))
            self.synced.set()

    def sync_loop(self) -> None:
        last_held, delay = 0.0, SYNC_S
        while not self.stop.is_set():
            self.synced.wait(delay)
            self.synced.clear()
            now = time.time()
            with self.qlock:
                results = self.outbox[:SYNC_MAX]  # small batches: a big upload stalls on a lossy link
                held = [{"queue": it["queue"], "id": it["id"], "progress": None} for it in self.queue]
                held += [
                    {"queue": r.split("/")[0], "id": int(r.split("/")[1]), "progress": b.get("progress")}
                    for r, b in self.running.items()
                ]
            send_held = now - last_held >= BEAT_S
            if not results and not send_held:
                continue
            try:
                code, r = self.call_broker("sync", {"results": results, "held": held if send_held else []}, timeout=30)
                if code != 200 or r is None:
                    raise OSError(f"sync: HTTP {code}")
                acks = r.get("acks") or {}
                with self.qlock:
                    self.outbox = [x for x in self.outbox if f"{x['queue']}/{x['id']}" not in acks]
                    drop = set(r.get("drop") or [])
                    if drop:
                        self.queue = [it for it in self.queue if f"{it['queue']}/{it['id']}" not in drop]
                        for ref in drop & set(self.running):
                            b = self.running[ref]
                            b["cancel"] = True
                            try:
                                b["resp"].close()
                            except Exception:
                                pass
                        log(f"broker dropped {len(drop)} item(s): {sorted(drop)[:5]}")
                    if relevel(self.queue, r.get("levels") or {}):
                        self.queue.sort(key=queue_order)
                lost = [k for k, v in acks.items() if v != 200]
                if lost:
                    log(f"broker no longer held {len(lost)} finished item(s) (lease ran out): {lost[:5]}")
                if send_held:
                    last_held = now
                delay = SYNC_S
                with self.qlock:
                    if self.outbox:  # more results waiting: send the next batch at once
                        self.synced.set()
            except Exception as e:
                log(f"sync: {type(e).__name__}: {e}".splitlines()[0])
                delay = min(15, delay * 2)

    def release_all(self) -> None:
        """Hand everything held straight back (worker stopping), in one sync."""
        with self.qlock:
            refs = [(it["queue"], it["id"]) for it in self.queue] + [
                (r.split("/")[0], int(r.split("/")[1])) for r in self.running
            ]
            results = list(self.outbox) + [{"queue": q, "id": i, "release": True} for q, i in refs]
        try:
            self.call_broker("sync", {"results": results, "held": []}, timeout=20)
        except Exception:
            pass


if __name__ == "__main__":
    if not BROKER or not MODELS:
        sys.exit("set GPU_WORKER_BROKER_URL (or TASKRUNNER_BROKER_URL) and GPU_WORKER_MODELS for this server")
    w = Worker()

    def stop(*_: object) -> None:
        w.stop.set()
        w.release_all()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    log(
        f"GPU worker {GPU} on {HOST}: models {MODELS}, queues {QUEUES or 'all'}, {'GPU only, ' if GPU_ONLY else ''}"
        f"Ollama {OLLAMA}, {SLOTS} slot(s), local queue ~{QUEUE_S:.0f} s of work, sync every {SYNC_S:.0f} s, broker {BROKER}"
    )
    threads = [threading.Thread(target=w.fetch_loop, daemon=True), threading.Thread(target=w.sync_loop, daemon=True)]
    threads += [threading.Thread(target=w.run_loop, args=(i,), daemon=True) for i in range(SLOTS)]
    for t in threads:
        t.start()
    while not w.stop.is_set():
        time.sleep(5)
