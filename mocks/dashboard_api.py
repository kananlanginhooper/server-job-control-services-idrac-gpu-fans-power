#!/usr/bin/env python3
"""Mock of the hub dashboard API that the services call (no fans, no caps, no hardware behind it).

Serves the endpoints the services in this repo use, with state you set from a test or from a JSON file:
  GET  /api/powertarget   {scale, mode, combined_w, <GPU_SERVER_KEY>_w, gpu_off}
  GET  /api/state         {fast: {power: {peak_5s, avg, t}}, remote: {fast: {power, power_watch: {pkg_w}, fan_control: {gpu: {draw}}}}}
  GET  /api/throttle      active throttles {host: {..., by, until, left_s}}
  POST /api/throttle      {host, <cap field>, seconds, by, reason} sets one; {host, clear: true} clears it
  GET  /api/powercaps     {<host>: {...}}
  GET  /api/runprofile    {profile}
  GET  /api/quickcool     {on, meeting, ...}
  POST /api/tasks         a list of task dicts (stored by id)
Every POST is also appended to .posts, so a test can check exactly what a service sent.

Run it for a demo or a manual test:   python mocks/dashboard_api.py --port 8090 [--state state.json]
then point HUB_API_URL at http://127.0.0.1:8090.
"""

from __future__ import annotations

import argparse
import copy
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HUB, SRV = "hub", "gpu-server"


def default_state(hub: str = HUB, srv: str = SRV) -> dict:
    now = time.time()
    return {
        "powertarget": {"scale": "up", "mode": "adaptive", "combined_w": 1250, f"{srv}_w": 1000, "gpu_off": False},
        "state": {
            "fast": {"power": {"peak_5s": 450.0, "avg": 430.0, "t": now}},
            "remote": {
                "fast": {
                    "power": {"peak_5s": 500.0, "avg": 480.0, "t": now},
                    "power_watch": {"pkg_w": {"package-0": 80.0, "package-1": 78.0}},
                    "fan_control": {"gpu": {"draw": 180.0}},
                }
            },
        },
        "powercaps": {srv: {"rapl_package_w": None}},
        "runprofile": {"profile": "adaptive"},
        "quickcool": {"on": False, "meeting": False},
    }


class MockDashboard:
    """A dashboard API on 127.0.0.1:<port> (0 = any free port) in a background thread."""

    def __init__(self, port: int = 0, state: dict | None = None, hub: str = HUB, srv: str = SRV) -> None:
        self.state = state or default_state(hub, srv)
        self.throttles, self.tasks, self.posts = {}, {}, []
        self.lock = threading.Lock()
        mock = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: object) -> None:
                pass

            def _send(self, obj: object, code: int = 200) -> None:
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                path = self.path.split("?")[0]
                with mock.lock:
                    if path == "/api/throttle":
                        now = time.time()
                        return self._send(
                            {h: dict(v, left_s=round(v["until"] - now)) for h, v in mock.throttles.items() if v["until"] > now}
                        )
                    if path == "/api/tasks":
                        return self._send(list(mock.tasks.values()))
                    key = path.removeprefix("/api/") if hasattr(path, "removeprefix") else path[5:]
                    if key in mock.state:
                        return self._send(copy.deepcopy(mock.state[key]))
                self._send({"error": "not found"}, 404)

            def do_POST(self) -> None:
                path = self.path.split("?")[0]
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"null")
                except ValueError:
                    return self._send({"error": "bad json"}, 400)
                with mock.lock:
                    mock.posts.append((path, body))
                    if path == "/api/throttle" and isinstance(body, dict) and body.get("host"):
                        if body.get("clear"):
                            mock.throttles.pop(body["host"], None)
                        else:
                            mock.throttles[body["host"]] = dict(body, until=time.time() + float(body.get("seconds") or 900))
                        return self._send({"ok": True})
                    if path == "/api/tasks":
                        for t in body if isinstance(body, list) else [body]:
                            mock.tasks[t.get("id") or len(mock.tasks)] = t
                        return self._send({"ok": True})
                self._send({"error": "not found"}, 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> MockDashboard:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()

    def set_power(
        self,
        hub_peak: float | None = None,
        hub_avg: float | None = None,
        srv_peak: float | None = None,
        srv_avg: float | None = None,
        pkg_w: float | None = None,
        gpu_draw: float | None = None,
    ) -> None:
        """Change the live readings the next GET /api/state returns (and mark them fresh)."""
        with self.lock:
            s, now = self.state["state"], time.time()
            lp, rp = s["fast"]["power"], s["remote"]["fast"]["power"]
            for d, k, v in ((lp, "peak_5s", hub_peak), (lp, "avg", hub_avg), (rp, "peak_5s", srv_peak), (rp, "avg", srv_avg)):
                if v is not None:
                    d[k] = float(v)
            lp["t"] = rp["t"] = now
            if pkg_w is not None:
                s["remote"]["fast"]["power_watch"]["pkg_w"] = {"package-0": float(pkg_w), "package-1": float(pkg_w)}
            if gpu_draw is not None:
                s["remote"]["fast"]["fan_control"]["gpu"]["draw"] = float(gpu_draw)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Mock hub dashboard API (no hardware behind it)")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--state", help="JSON file with any of: powertarget, state, powercaps, runprofile, quickcool")
    ap.add_argument("--hub-key", default=HUB)
    ap.add_argument("--gpu-server-key", default=SRV)
    a = ap.parse_args()
    st = default_state(a.hub_key, a.gpu_server_key)
    if a.state:
        with open(a.state) as f:
            st.update(json.load(f))
    with MockDashboard(a.port, st, a.hub_key, a.gpu_server_key) as m:
        print(f"mock dashboard API on {m.url} (Ctrl-C to stop)")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
