#!/opt/GPU server-runtime/whisper/venv/bin/python
# GPU server transcription worker for the video pipeline (2026-09-30, the owner: "set up whisper on GPU server" - use the GPU server's idle CPU).
# Stateless: POST a 16 kHz mono WAV to /transcribe, get {language, lang_prob, duration, segments:[{s,e,t}]} back.
# No database access and no files kept: the hub claims videos, extracts audio, sends it here and writes the results to MySQL.
# One model per process, one request at a time; systemd runs several instances (whisper-srv@PORT). Listens on the 10 Gb
# link address only. Privacy: never logs transcript text (counts and timings only).
import json, os, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from faster_whisper import WhisperModel
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "lib"))
from svcenv import env  # noqa: E402  (shared repo .env; see .env.example)

PORT = int(sys.argv[1]); THREADS = int(sys.argv[2]) if len(sys.argv) > 2 else 10
BIND = env("WHISPER_BIND", "127.0.0.1")   # 10 Gb port since the 2026-10-01 rebuild
# 2026-10-14 (the owner "yes, run the test"): WHISPER_DEVICE=cuda runs on the RTX 2080 Ti next to Ollama (int8_float16,
# ~1.5 GB VRAM; cuBLAS from Ollama's bundle via LD_LIBRARY_PATH in the unit drop-in). Default stays CPU.
DEVICE = env("WHISPER_DEVICE", "cpu")
MODEL = WhisperModel("medium", device=DEVICE, compute_type="int8_float16" if DEVICE == "cuda" else "int8",
                     cpu_threads=THREADS, download_root=env("WHISPER_MODELS", "/opt/server-services/whisper-server/models"))
POWER_STATE = "/dev/shm/power-hooks/state.json"


def gpu_low_power():
    """GPU jobs honour Low Power / Meeting (the owner 09-28): while scale=down the request waits instead of failing
    (a 503 would be recorded as a transcription error by the hub client)."""
    if DEVICE != "cuda":
        return False
    try:
        return json.load(open(POWER_STATE)).get("scale") == "down"
    except Exception:
        return False
LOCK = threading.Lock()
# Dashboard graph (the owner: every job reports its own feed). GPU instances only: /dev/shm/gpu-job.whisper-gpu-PORT.json,
# done_total = videos transcribed by this server process, rate over the last 10 min; paused while Low Power holds it.
FEED = f"/dev/shm/gpu-job.whisper-gpu-{PORT}.json"
DONE = []          # completion times


def feed_loop():
    while True:
        now = time.time()
        recent = [t for t in DONE if now - t <= 600]
        state = "paused" if gpu_low_power() else ("running" if recent else "idle")
        f = {"job": f"Whisper transcription on the GPU server (port {PORT})", "owner_thread": "whisper-server",
             "unit": "videos", "done_total": len(DONE), "rate_per_s": round(len(recent) / 600, 4),
             "updated_at": int(now), "state": state}
        try:
            with open(FEED + ".tmp", "w") as fh:
                json.dump(f, fh)
            os.replace(FEED + ".tmp", FEED)
        except OSError:
            pass
        time.sleep(60)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

    def do_POST(self):
        if self.path != "/transcribe":
            self.send_response(404); self.end_headers(); return
        n = int(self.headers.get("Content-Length") or 0)
        with tempfile.NamedTemporaryFile(dir="/dev/shm", suffix=".wav") as f:
            left = n
            while left > 0:
                b = self.rfile.read(min(left, 1 << 20))
                if not b:
                    break
                f.write(b); left -= len(b)
            f.flush()
            t0 = time.time()
            try:
                with LOCK:
                    while gpu_low_power():
                        time.sleep(30)
                    # 2026-10-16: transient CUDA OOM / invalid-device errors when the GPU is shared (Ollama image model +
                    # several Whisper servers) - wait and retry once before failing the video
                    for attempt in (1, 2):
                        try:
                            segs, info = MODEL.transcribe(f.name, vad_filter=True, beam_size=5, hotwords="Sync, Sync Lock, Sync Unlock")   # the owner's spoken markers
                            seg = [{"s": round(s.start, 2), "e": round(s.end, 2), "t": s.text.strip()} for s in segs]
                            break
                        except RuntimeError as re_:
                            if attempt == 2 or not any(k in str(re_) for k in ("out of memory", "invalid device")):
                                raise
                            print(f"{time.strftime('%FT%T')} port {PORT}: CUDA {str(re_)[:60]} - retrying in 10 s", flush=True)
                            time.sleep(10)
                out = {"language": info.language, "lang_prob": round(info.language_probability, 3),
                       "duration": round(info.duration, 1), "segments": seg}
                code = 200
                DONE.append(time.time())
            except Exception as e:
                out, code = {"error": str(e)[:250]}, 500
                # 2026-10-16: 924 videos failed with HTTP 500 and nothing said why - log the error type (no transcript text)
                print(f"{time.strftime('%FT%T')} port {PORT}: ERROR {type(e).__name__}: {str(e)[:200]}", flush=True)
        body = json.dumps(out, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        print(f"{time.strftime('%FT%T')} port {PORT}: {n // 32000} s audio in {time.time() - t0:.0f} s", flush=True)


if DEVICE == "cuda":
    threading.Thread(target=feed_loop, daemon=True).start()
ThreadingHTTPServer((BIND, PORT), H).serve_forever()
