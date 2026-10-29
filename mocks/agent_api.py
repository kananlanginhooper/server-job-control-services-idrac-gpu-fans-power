"""Mock of the control container's agent API (contract: contracts/agent-api.md, v1), stdlib only.

The reference both sides test against: the per-server agent (this repo) and the container (Recovery's hub/).
  GET  /api/v1/agent/servers/{host}/orders?version=N&wait=S     200 orders | 304 empty body
  POST /api/v1/agent/servers/{host}/report                      {ok}
  GET  /api/v1/agent/servers/{host}/commands?wait=S             [claimed commands]
  POST /api/v1/agent/servers/{host}/commands/{id}/result        {ok} | 409
  POST /api/v1/agent/queues/{queue}/claim                       [leased items]
  POST /api/v1/agent/queues/items/{id}/done | /failed           {ok} | 409
Auth: Bearer token per host (only SHA-256s are kept); 401 bad token, 403 another host's {host}.
The container's clock is `self.now()`; tests move it with `self.skew`.

Run it for a demo:   python mocks/agent_api.py --port 8110 --host-token myhost=secret
"""

from __future__ import annotations

import hashlib
import itertools
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HW_COMMANDS = {"fan.override", "gpu.limit"}
HW_EXPIRY_S, OTHER_EXPIRY_S, MAX_WAIT_S = 30, 600, 25


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


class MockAgentAPI:
    """The container side on 127.0.0.1:<port> (0 = any free port) in a background thread."""

    def __init__(self, tokens: dict[str, str] | None = None, port: int = 0) -> None:
        self.token_hashes = {sha256(t): h for h, t in (tokens or {}).items()}  # hash -> host
        self.skew = 0.0
        self.cv = threading.Condition()
        self.orders: dict[str, dict] = {}  # host -> {version, at, tasks, settings}
        self.reports: dict[str, list] = {}
        self.commands: dict[str, dict] = {}  # id -> {id, host, cmd, args, by, queued_at, expires_at, state, result}
        self.items: dict[str, dict] = {}  # id -> {id, queue, payload, attempt, state, lease_until, max_attempts, ...}
        self._ids = itertools.count(1)
        mock = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: object) -> None:
                pass

            def _send(self, obj: object, code: int = 200) -> None:
                body = b"" if code == 304 else json.dumps(obj).encode()
                self.send_response(code)  # also sends the Date header (the container's clock, for expiry checks)
                if code != 304:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _auth(self) -> str | None:
                h = self.headers.get("Authorization") or ""
                host = mock.token_hashes.get(sha256(h[7:])) if h.startswith("Bearer ") else None
                if not host:
                    self._send({"error": "bad token"}, 401)
                return host

            def _body(self) -> dict:
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def _route(self, method: str) -> None:
                u = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(u.query))
                parts = u.path.strip("/").split("/")
                if parts[:3] != ["api", "v1", "agent"]:
                    return self._send({"error": "not found"}, 404)
                parts = parts[3:]
                who = self._auth()
                if not who:
                    return
                if parts[:1] == ["servers"] and len(parts) >= 3:
                    if parts[1] != who:
                        return self._send({"error": "another host"}, 403)
                    host = parts[1]
                    wait = min(float(q.get("wait", 0)), MAX_WAIT_S)
                    if method == "GET" and parts[2:] == ["orders"]:
                        o = mock.wait_orders(host, int(q.get("version", 0)), wait)
                        return self._send(o) if o else self._send(None, 304)
                    if method == "POST" and parts[2:] == ["report"]:
                        with mock.cv:
                            mock.reports.setdefault(host, []).append(self._body())
                        return self._send({"ok": True})
                    if method == "GET" and parts[2:] == ["commands"]:
                        return self._send(mock.claim_commands(host, wait))
                    if method == "POST" and len(parts) == 5 and parts[2] == "commands" and parts[4] == "result":
                        ok = mock.finish_command(host, parts[3], self._body())
                        return self._send({"ok": True}) if ok else self._send({"error": "expired or finished"}, 409)
                if parts[:1] == ["queues"] and method == "POST":
                    if len(parts) == 3 and parts[2] == "claim":
                        b = self._body()
                        return self._send(
                            mock.claim_items(parts[1], b.get("worker", who), int(b.get("n", 1)), float(b.get("lease_s", 600)))
                        )
                    if len(parts) == 4 and parts[1] == "items" and parts[3] in ("done", "failed"):
                        ok = mock.finish_item(parts[2], parts[3], self._body())
                        return self._send({"ok": True}) if ok else self._send({"error": "lease ran out"}, 409)
                self._send({"error": "not found"}, 404)

            def do_GET(self) -> None:
                self._route("GET")

            def do_POST(self) -> None:
                self._route("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> MockAgentAPI:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()

    # ---------- the container's side (what the real back end does) ----------
    def now(self) -> float:
        return time.time() + self.skew

    def set_orders(self, host: str, tasks: list[dict], settings: dict | None = None) -> int:
        with self.cv:
            v = self.orders.get(host, {}).get("version", 0) + 1
            self.orders[host] = {"version": v, "at": self.now(), "tasks": tasks, "settings": settings or {}}
            self.cv.notify_all()
            return v

    def wait_orders(self, host: str, version: int, wait: float) -> dict | None:
        end = time.time() + wait
        with self.cv:
            while True:
                o = self.orders.get(host)
                if o and o["version"] > version:
                    return o
                left = end - time.time()
                if left <= 0:
                    return None
                self.cv.wait(left)

    def enqueue(self, host: str, cmd: str, args: dict | None = None, by: str = "test") -> str:
        with self.cv:
            cid = f"c{next(self._ids)}"
            t = self.now()
            self.commands[cid] = {
                "id": cid, "host": host, "cmd": cmd, "args": args or {}, "by": by, "queued_at": t,
                "expires_at": t + (HW_EXPIRY_S if cmd in HW_COMMANDS else OTHER_EXPIRY_S), "state": "queued", "result": None,
            }  # fmt: skip
            self.cv.notify_all()
            return cid

    def _expire(self) -> None:
        for c in self.commands.values():
            if c["state"] in ("queued", "claimed") and self.now() >= c["expires_at"]:
                c["state"] = "expired"

    def claim_commands(self, host: str, wait: float) -> list[dict]:
        end = time.time() + wait
        with self.cv:
            while True:
                self._expire()
                got = sorted(
                    (c for c in self.commands.values() if c["host"] == host and c["state"] == "queued"),
                    key=lambda c: c["queued_at"],
                )
                if got or time.time() >= end:
                    for c in got:
                        c["state"] = "claimed"  # at most once: never handed out again
                    return [{k: c[k] for k in ("id", "cmd", "args", "by", "queued_at", "expires_at")} for c in got]
                self.cv.wait(end - time.time())

    def finish_command(self, host: str, cid: str, result: dict) -> bool:
        with self.cv:
            self._expire()
            c = self.commands.get(cid)
            if not c or c["host"] != host or c["state"] != "claimed":
                return False
            c["state"] = "done" if result.get("ok") else ("refused" if result.get("refused") else "failed")
            c["result"] = result
            return True

    def add_items(self, queue: str, payloads: list[dict], max_attempts: int = 3) -> list[str]:
        with self.cv:
            ids = []
            for p in payloads:
                iid = f"i{next(self._ids)}"
                self.items[iid] = {"id": iid, "queue": queue, "payload": p, "attempt": 0, "state": "ready", "lease_until": None,
                                   "max_attempts": max_attempts, "result": None, "error": None}  # fmt: skip
                ids.append(iid)
            return ids

    def _sweep(self) -> None:
        for it in self.items.values():
            if it["state"] == "claimed" and self.now() >= it["lease_until"]:
                it["state"], it["lease_until"] = "ready", None

    def claim_items(self, queue: str, worker: str, n: int, lease_s: float) -> list[dict]:
        with self.cv:
            self._sweep()
            got = [it for it in self.items.values() if it["queue"] == queue and it["state"] == "ready"][:n]
            for it in got:
                it.update(state="claimed", worker=worker, lease_until=self.now() + lease_s, attempt=it["attempt"] + 1)
            return [{k: it[k] for k in ("id", "payload", "attempt", "lease_until")} for it in got]

    def finish_item(self, iid: str, how: str, body: dict) -> bool:
        with self.cv:
            self._sweep()
            it = self.items.get(iid)
            if not it or it["state"] != "claimed":
                return False
            if how == "done":
                it.update(state="done", result=body.get("result"), lease_until=None)
            else:
                again = body.get("retry") and it["attempt"] < it["max_attempts"]
                it.update(state="ready" if again else "failed", error=body.get("error"), lease_until=None)
            return True


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8110)
    ap.add_argument("--host-token", action="append", default=[], help="HOST=TOKEN (repeatable)")
    a = ap.parse_args()
    m = MockAgentAPI(dict(x.split("=", 1) for x in a.host_token), a.port)
    print("mock agent API on", m.url)
    m.thread.start()
    m.thread.join()
