#!/usr/bin/python3
# Run broker (owner, 2026-10-30): ONE broker on the hub decides what runs where; one task runner per server (the hub's included)
# runs it. "The run broker is the one that would look at the queue, look at what's important, what's being requested, and then
# issue the runs to a runner... the broker writing the database."
#
#   requests  task_requests rows (TASK-REQUESTS standard) that are accepted: status 'queued' with no placed_task yet, or
#             'requested' too when RUN_BROKER_AUTO_ACCEPT=1. Taken in line order: priority, wanted_by, requested_at.
#   placement a host from RUN_BROKER_HOSTS with a free slot in the request's lane (gpu: needs.gpu required/preferred and enough
#             VRAM, never a host in RUN_BROKER_GPU_HELD; cpu: the host with the most free slots). A placed_host set by hand wins.
#   orders    each host's complete set of broker rows, served on the agent-API orders route (contracts/agent-api.md section 4):
#             the runner long-polls it and copies the rows into its local tasks table. Every change bumps the host's version.
#   reports   the runner posts its state and every run start/exit (section 5). The broker stores them and moves the request:
#             running -> done (oneshot exit 0) / failed (RUN_BROKER_MAX_FAILS failed runs in a row), withdrawing the order.
#             'paused' disables the order, back to queued/running enables it; cancelled/declined withdraws it.
# The broker is the only writer of the hub's broker tables and of task_requests' status/placement fields. Runners never write here.
# Runners keep running their last orders while the broker is down; only new placements wait.
# Its tables live in schema `broker` of the hub's `taskrunner` database, next to (never inside) the hub runner's own tables.
# Never touches run mode, power target, the GPU switch or any core-safety service; runners keep their own Low Power gate.
#
# Work queues (workq.py): the same pattern for single units of work. Two tables, work.priority_items (immediate,
# human-in-the-loop), work.standard_items (normal jobs) and work.backup_items (endless low-return work, gives way). One GPU worker per GPU (services/gpu-worker) claims items through the
# broker's work routes; the broker picks the item (priority table first, level, the loaded model, studies in turn) and writes
# every change. Request placement (above) only runs with RUN_BROKER_PLACE_REQUESTS=1; the work queues always do.
from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except ImportError:  # the pure placement/status helpers below are testable without it
    psycopg = Jsonb = dict_row = None

_here = os.path.dirname(os.path.realpath(__file__))
sys.path[:0] = [_here, os.path.join(_here, "..", "..", "lib")]
import svcenv  # noqa: E402
import workq  # noqa: E402
from svcenv import env  # noqa: E402

DSN = env("RUN_BROKER_DSN", "dbname=taskrunner")
PORT = int(env("RUN_BROKER_PORT", "8099"))
# {"<host>": {"cpu_slots": n, "gpu_slots": n, "gpu_vram_gb": n}}, in preference order (first = preferred for CPU work on a tie)
HOSTS = json.loads(env("RUN_BROKER_HOSTS", "{}"))
GPU_HELD = {h for h in env("RUN_BROKER_GPU_HELD", "").replace(",", " ").split() if h}  # GPU lent out: GPU work waits
AUTO_ACCEPT = env("RUN_BROKER_AUTO_ACCEPT", "") not in ("", "0")
PLACE_REQUESTS = env("RUN_BROKER_PLACE_REQUESTS", "") not in ("", "0")  # place task_requests (off: work queues only)
# the wrapper's tab list: dashboard order ranks standard and backup work (read every minute; last good answer kept)
TABS_URL = env("RUN_BROKER_TABS_URL", "http://127.0.0.1:8097/api/tabs")
# A reservation: until RUN_BROKER_RESERVE_UNTIL (ISO 8601 with offset), the priority queue's owner holds the priority slot
# (owner, 2026-11-18: a reservation is for that work to take the priority slot, not for all work to be priority).
# RUN_BROKER_RESERVE_FOR says who and why. Mode "fill" (docs/gpu-scheduling.md): standard and backup work fills a GPU once it
# has gone RUN_BROKER_FILL_IDLE_S with no priority work, and standard work gives way to priority work like backup does.
# Mode "exclusive": priority-queue work only.
RESERVE_UNTIL = env("RUN_BROKER_RESERVE_UNTIL", "")
RESERVE_FOR = env("RUN_BROKER_RESERVE_FOR", "")
RESERVE_MODE = env("RUN_BROKER_RESERVE_MODE", "fill")
FILL_IDLE_S = float(env("RUN_BROKER_FILL_IDLE_S", "30"))
# work giving way with less than this many seconds of estimated run time left finishes first (owner, 2026-11-18: a
# changeover may take minutes; don't thrash)
YIELD_FINISH_S = float(env("RUN_BROKER_YIELD_FINISH_S", "300"))
ITEM_S_DEFAULT = 20.0  # assumed seconds per item for a GPU, model and context with no finished item yet
JOB_S_DEFAULT = 3600.0  # the same for a job (model job:<name>): one job is a whole batch until one has finished
LEASE_S = int(env("RUN_BROKER_LEASE_S", "120"))  # a claimed item is handed out again if its worker stops beating for this long
MAX_FAILS = int(env("RUN_BROKER_MAX_FAILS", "3"))
CODE_ROOT = env("RUN_BROKER_CODE_ROOT", "~/server-projects")  # where every host keeps its repo clones
TICK, WAIT_MAX = 10, 25
PRIORITY = {"high": 0, "normal": 1, "low": 2}
RUNNER_FIELDS = (
    "name", "command", "workdir", "env", "kind", "instances", "instances_by_power",
    "every_secs", "at_times", "needs", "nice", "stop_secs", "enabled",
)  # fmt: skip

SCHEMA = """
create schema if not exists broker;
create table if not exists broker.hosts(
  host text primary key,
  version bigint not null default 0,         -- bumped on every change to this host's orders; only goes up
  last_report jsonb,
  last_seen timestamptz
);
create table if not exists broker.orders(
  name text primary key,                     -- the runner row's name: req-<id>-<short>
  host text not null,
  lane text not null check (lane in ('cpu', 'gpu')),
  request_id bigint,
  spec jsonb not null,                       -- the runner row (RUNNER_FIELDS)
  placed_at timestamptz not null default now(),
  removed_at timestamptz                     -- withdrawn; active = removed_at is null
);
create index if not exists orders_active on broker.orders(host) where removed_at is null;
create table if not exists broker.runs(
  host text not null,
  run_id bigint not null,                    -- the runner's own task_runs.id
  task text not null,
  instance int not null,
  pid int,
  started_at timestamptz,
  ended_at timestamptz,
  exit_code int,
  primary key (host, run_id)
);
create index if not exists runs_task on broker.runs(host, task, started_at);
"""


def log(msg: str) -> None:
    print(time.strftime("%F %T"), msg, flush=True)


# ---------- pure helpers (tests/test_run_broker.py) ----------
def lane(needs: dict) -> str:
    return "gpu" if (needs or {}).get("gpu") in ("required", "preferred", True) else "cpu"


def line_key(r: dict) -> tuple:
    """Place in line, as the task_queue view orders it."""
    return (
        PRIORITY.get(r.get("priority"), 1),
        r.get("wanted_by") is None,
        r.get("wanted_by") or 0,
        r.get("requested_at") or 0,
        r["id"],
    )


def choose_host(req: dict, hosts: dict, load: dict, held: set) -> tuple[str | None, str, str]:
    """(host or None, lane, why not). load = {host: {"cpu": n, "gpu": n}} active broker orders."""
    needs = req.get("needs") or {}
    names = [req["placed_host"]] if req.get("placed_host") else list(hosts)
    names = [h for h in names if h in hosts]
    if not names:
        return None, "cpu", f"unknown host {req.get('placed_host')}"

    def free(h: str, ln: str) -> int:
        return int(hosts[h].get(f"{ln}_slots", 0)) - (load.get(h) or {}).get(ln, 0)

    why = ""
    if needs.get("gpu") in ("required", "preferred"):
        vram = float(needs.get("gpu_vram_gb") or 0)
        fits = [h for h in names if int(hosts[h].get("gpu_slots", 0)) > 0 and float(hosts[h].get("gpu_vram_gb", 0)) >= vram]
        open_ = [h for h in fits if h not in held]
        ready = [h for h in open_ if free(h, "gpu") > 0]
        if ready:  # the smallest GPU that fits, so the big one stays free for big models
            return min(ready, key=lambda h: (float(hosts[h].get("gpu_vram_gb", 0)), names.index(h))), "gpu", ""
        why = (
            f"no GPU with {vram:g} GB"
            if not fits
            else "the GPUs are lent out; waits for them"
            if not open_
            else "waiting for a free GPU"
        )
        if needs.get("gpu") == "required":
            return None, "gpu", why
    ready = [h for h in names if free(h, "cpu") > 0]
    if ready:
        return max(ready, key=lambda h: (free(h, "cpu"), -names.index(h))), "cpu", ""
    return None, "cpu", why or "waiting for a free CPU slot"


def slug(title: str) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:24].strip("-")) or "job"


def order_spec(req: dict, ln: str, code_root: str = CODE_ROOT) -> dict:
    """The runner row for a request placed in lane `ln`."""
    needs = req.get("needs") or {}
    wd = None  # no repo: the runner's own working directory
    if req.get("repo"):
        wd = "/".join(p.strip("/") for p in (code_root, req["repo"], req.get("workdir") or "") if p.strip("/"))
        wd = "/" + wd if code_root.startswith("/") else wd
    run_needs = {}
    paths = [p for p in needs.get("reads") or [] if isinstance(p, str) and p.startswith("/")]
    if paths:
        run_needs["paths"] = paths
    if ln == "gpu":
        run_needs["gpu"] = True  # the runner's own Low Power gate
    return {
        "name": f"req-{req['id']}-{slug(req.get('title'))}",
        "command": req["command"],
        "workdir": wd,
        "env": {**(req.get("env") or {}), "TASK_REQUEST_ID": str(req["id"])},
        "kind": req.get("kind") or "oneshot",
        "instances": 1,
        "instances_by_power": None,
        "every_secs": req.get("every_secs"),
        "at_times": None,
        "needs": run_needs,
        "nice": 10,
        "stop_secs": 30,
        "enabled": req.get("status") != "paused",
    }


def request_update(kind: str, runs: list[dict], max_fails: int = MAX_FAILS) -> dict | None:
    """What a request should say, from its order's runs (oldest first). None = nothing ran yet.
    -> {status, started_at, finished_at, exit_code, status_note, withdraw}"""
    if not runs:
        return None
    out = {
        "status": "running",
        "started_at": runs[0]["started_at"],
        "finished_at": None,
        "exit_code": None,
        "status_note": None,
        "withdraw": False,
    }
    last = runs[-1]
    if last.get("ended_at") is None:
        return out
    fails = 0
    for r in reversed(runs):  # failed runs in a row, newest back
        if r.get("ended_at") is None or r.get("exit_code") == 0:
            break
        fails += 1
    if kind == "oneshot" and last.get("exit_code") == 0:
        out.update(status="done", finished_at=last["ended_at"], exit_code=0, withdraw=True)
    elif fails >= max_fails:
        out.update(
            status="failed", finished_at=last["ended_at"], exit_code=last.get("exit_code"), withdraw=True,
            status_note=f"{fails} failed runs in a row (last exit {last.get('exit_code')}); see the runner log on the host",
        )  # fmt: skip
    elif fails:
        out.update(
            exit_code=last.get("exit_code"),
            status_note=f"exit {last.get('exit_code')}; the runner retries ({fails} of {max_fails})",
        )
    else:
        out.update(exit_code=last.get("exit_code"))  # a schedule between runs, or a continuous copy restarting
    return out


def host_tokens() -> dict:
    """{host: token} from <HOST>_AGENT_TOKEN in the environment or the shared .env."""
    out = {}
    for h in HOSTS:
        k = re.sub(r"[^A-Z0-9]", "_", h.upper()) + "_AGENT_TOKEN"
        v = os.environ.get(k) or svcenv.FILE.get(k)
        if v:
            out[h] = v
    return out


def auth(header: str | None, host: str, tokens: dict) -> int:
    """200 ok, 401 bad/missing token, 403 a valid token for another host."""
    got = (header or "").removeprefix("Bearer ").strip()
    if not got:
        return 401
    for h, tok in tokens.items():
        if hmac.compare_digest(hashlib.sha256(got.encode()).digest(), hashlib.sha256(tok.encode()).digest()):
            return 200 if h == host else 403
    return 401


# ---------- the broker ----------
class Broker:
    def __init__(
        self,
        dsn: str = DSN,
        hosts: dict | None = None,
        held: set | None = None,
        auto_accept: bool = AUTO_ACCEPT,
        code_root: str = CODE_ROOT,
        place_requests: bool | None = None,
    ) -> None:
        self.dsn, self.hosts, self.code_root = dsn, HOSTS if hosts is None else hosts, code_root
        self.place_requests = PLACE_REQUESTS if place_requests is None else place_requests
        self.held, self.auto_accept = GPU_HELD if held is None else held, auto_accept
        self.local = threading.local()
        self.changed = threading.Condition()
        self.lock = threading.Lock()  # one writer at a time: the tick and report handlers
        self.item_s: dict[tuple, float] = {}  # (gpu, model, ctx) -> recent seconds per item (load excluded), for batch sizing
        self.conn().execute(SCHEMA)
        self.conn().execute(workq.SCHEMA)
        for h in self.hosts:
            self.conn().execute("insert into broker.hosts(host) values(%s) on conflict do nothing", (h,))

    def conn(self) -> psycopg.Connection:
        c = getattr(self.local, "db", None)
        if c is None or c.closed:
            c = self.local.db = psycopg.connect(self.dsn, autocommit=True, application_name="run-broker")
        return c

    def close_conn(self) -> None:
        """Close this thread's connection. Every HTTP request runs in a thread of its own, so each request closes its
        connection when it ends (a thread-local connection is NOT closed when its thread exits: that leaked one per request)."""
        c = getattr(self.local, "db", None)
        self.local.db = None
        if c is not None and not c.closed:
            c.close()

    def rows(self, sql: str, args: tuple = ()) -> list[dict]:
        cur = self.conn().cursor(row_factory=dict_row)
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else []

    def bump(self, cur: psycopg.Cursor, host: str) -> None:
        cur.execute("update broker.hosts set version = version + 1 where host = %s", (host,))

    def notify(self) -> None:
        with self.changed:
            self.changed.notify_all()

    # orders
    def version(self, host: str) -> int:
        r = self.rows("select version from broker.hosts where host = %s", (host,))
        return r[0]["version"] if r else 0

    def orders(self, host: str) -> dict:
        with self.conn().transaction():
            v = self.version(host)
            tasks = [
                r["spec"]
                for r in self.rows("select spec from broker.orders where host = %s and removed_at is null order by name", (host,))
            ]
        return {"version": v, "at": time.time(), "tasks": tasks, "settings": {}}

    def wait_orders(self, host: str, since: int, wait: float) -> dict | None:
        end = time.time() + min(max(wait, 0), WAIT_MAX)
        while True:
            if self.version(host) > since:
                return self.orders(host)
            left = end - time.time()
            if left <= 0:
                return None
            with self.changed:
                self.changed.wait(min(left, 5))

    # placement
    def load(self) -> dict:
        out = {h: {"cpu": 0, "gpu": 0} for h in self.hosts}
        for r in self.rows("select host, lane, count(*) n from broker.orders where removed_at is null group by host, lane"):
            out.setdefault(r["host"], {"cpu": 0, "gpu": 0})[r["lane"]] = r["n"]
        return out

    def tick(self) -> None:
        with self.lock:
            self.work_expire()
            touched = (self.follow_requests() | self.place()) if self.place_requests else set()
        if touched:
            self.notify()

    def follow_requests(self) -> set:
        """Requests changed by people: cancelled/declined withdraws the order; paused disables it; queued/running enables it."""
        touched = set()
        live = self.rows(
            "select o.name, o.host, o.spec, r.status from broker.orders o join task_requests r on r.id = o.request_id "
            "where o.removed_at is null"
        )
        with self.conn().transaction():
            cur = self.conn().cursor()
            for o in live:
                if o["status"] in ("cancelled", "declined", "done", "failed"):
                    cur.execute("update broker.orders set removed_at = now() where name = %s", (o["name"],))
                    log(f"{o['name']}: request {o['status']}; withdrawn from {o['host']}")
                elif o["spec"].get("enabled") != (o["status"] != "paused"):
                    spec = dict(o["spec"], enabled=o["status"] != "paused")
                    cur.execute("update broker.orders set spec = %s where name = %s", (Jsonb(spec), o["name"]))
                    log(f"{o['name']}: request {o['status']}; {'enabled' if spec['enabled'] else 'disabled'}")
                else:
                    continue
                self.bump(cur, o["host"])
                touched.add(o["host"])
        return touched

    def place(self) -> set:
        states = ("queued", "requested") if self.auto_accept else ("queued",)
        reqs = self.rows(
            "select id, title, repo, workdir, command, env, kind, every_secs, needs, priority, status, status_note, placed_host, "
            "extract(epoch from wanted_by) wanted_by, extract(epoch from requested_at) requested_at from task_requests "
            "where status = any(%s) and placed_task is null",
            (list(states),),
        )
        load, touched = self.load(), set()
        for req in sorted(reqs, key=line_key):
            host, ln, why = choose_host(req, self.hosts, load, self.held)
            if not host:
                if req["status"] == "queued" and req["status_note"] != why:
                    self.conn().execute("update task_requests set status_note = %s where id = %s", (why, req["id"]))
                continue
            spec = order_spec(req, ln, self.code_root)
            with self.conn().transaction():
                cur = self.conn().cursor()
                cur.execute(
                    "insert into broker.orders(name, host, lane, request_id, spec) values(%s, %s, %s, %s, %s) "
                    "on conflict (name) do update set host = excluded.host, lane = excluded.lane, spec = excluded.spec, "
                    "placed_at = now(), removed_at = null",
                    (spec["name"], host, ln, req["id"], Jsonb(spec)),
                )
                cur.execute(
                    "update task_requests set status = 'queued', placed_host = %s, placed_task = %s, placed_at = now(), "
                    "status_note = %s where id = %s",
                    (host, spec["name"], f"placed by the run broker ({ln})", req["id"]),
                )
                self.bump(cur, host)
            load.setdefault(host, {"cpu": 0, "gpu": 0})[ln] += 1
            touched.add(host)
            log(f"request {req['id']} -> {host} ({ln}) as {spec['name']}")
        return touched

    # reports
    def report(self, host: str, body: dict) -> None:
        with self.lock:
            touched = False
            with self.conn().transaction():
                cur = self.conn().cursor()
                cur.execute(
                    "update broker.hosts set last_report = %s, last_seen = now() where host = %s",
                    (Jsonb({k: v for k, v in body.items() if k != "runs"}), host),
                )
                for r in body.get("runs") or []:
                    cur.execute(
                        "insert into broker.runs(host, run_id, task, instance, pid, started_at, ended_at, exit_code) "
                        "values(%s, %s, %s, %s, %s, to_timestamp(%s), to_timestamp(%s), %s) on conflict (host, run_id) do update set "
                        "pid = excluded.pid, started_at = excluded.started_at, ended_at = excluded.ended_at, exit_code = excluded.exit_code",
                        (
                            host,
                            r["id"],
                            r["task"],
                            r["instance"],
                            r.get("pid"),
                            r.get("started_at"),
                            r.get("ended_at"),
                            r.get("exit_code"),
                        ),
                    )
                tasks = {r["task"] for r in body.get("runs") or []}
                for o in self.rows(
                    "select o.name, o.request_id, o.spec->>'kind' kind from broker.orders o "
                    "where o.host = %s and o.removed_at is null and o.request_id is not null and o.name = any(%s)",
                    (host, list(tasks)),
                ):
                    runs = self.rows(
                        "select started_at, ended_at, exit_code from broker.runs where host = %s and task = %s order by started_at, run_id",
                        (host, o["name"]),
                    )
                    u = request_update(o["kind"], runs)
                    if not u:
                        continue
                    cur.execute(
                        "update task_requests set status = %s, started_at = coalesce(started_at, %s), finished_at = %s, exit_code = %s, "
                        "status_note = coalesce(%s, status_note) where id = %s and status in ('queued', 'running')",
                        (u["status"], u["started_at"], u["finished_at"], u["exit_code"], u["status_note"], o["request_id"]),
                    )
                    if u["withdraw"]:
                        cur.execute("update broker.orders set removed_at = now() where name = %s", (o["name"],))
                        self.bump(cur, host)
                        touched = True
                        log(f"{o['name']}: {u['status']}; withdrawn from {host}")
        if touched:
            self.notify()

    # ---------- work queues (workq.py) ----------
    def work_expire(self) -> None:
        """Claims whose worker stopped beating go back to ready (or fail after max_attempts)."""
        # a lease that runs out while its GPU still checks in (a lossy link, a worker restart) isn't the item's fault: it
        # goes back to ready without using up an attempt. Only a GPU gone silent counts it (an item that kills its worker).
        alive = "exists (select 1 from work.gpus g where g.gpu = claimed_by and g.last_seen > now() - interval '120 seconds')"
        for q in workq.QUEUES:
            for r in self.rows(
                f"update work.{q}_items set attempt = case when {alive} then greatest(attempt - 1, 0) else attempt end, "
                f"state = case when not {alive} and attempt >= max_attempts then 'failed' else 'ready' end, "
                "error = 'lease expired on ' || claimed_by, claimed_by = null, lease_until = null, "
                f"finished_at = case when not {alive} and attempt >= max_attempts then now() end "
                "where state = 'claimed' and lease_until < now() returning id, state"
            ):
                log(f"work {q} {r['id']}: lease expired -> {r['state']}")

    def work_claim(self, host: str, body: dict) -> dict | None:
        """One item for this GPU, or None. body: {gpu, models: [own first, then fallbacks], loaded: {model, ctx} | null}."""
        gpu, models = body["gpu"], [m for m in body.get("models") or [] if m]
        queues = [q for q in workq.QUEUES if q in (body.get("queues") or workq.QUEUES)]  # this worker's queues, in claim order
        res = reservation()
        queues = claim_queues(queues, res, float(body.get("idle_s") or 0))
        ld = body.get("loaded") or {}
        loaded = (ld["model"], int(ld.get("ctx") or 0)) if ld.get("model") else None
        ctx_sql = "(payload #>> '{body,options,num_ctx}')::int"
        with self.lock:
            g = self.rows(
                "select model, ctx, extract(epoch from model_since)::float8 since from work.gpus where gpu = %s", (gpu,)
            )
            since = g[0]["since"] if g and loaded and (g[0]["model"], g[0]["ctx"] or 0) == loaded else None
            if loaded and since is None:
                since = time.time()
            self.conn().execute(
                "insert into work.gpus(gpu, host, model, ctx, model_since, last_seen, models, queues) "
                "values(%s, %s, %s, %s, to_timestamp(%s), now(), %s, %s) "
                "on conflict (gpu) do update set host = excluded.host, model = excluded.model, ctx = excluded.ctx, "
                "model_since = excluded.model_since, last_seen = now(), models = excluded.models, queues = excluded.queues",
                (gpu, host, loaded[0] if loaded else None, loaded[1] if loaded else None, since, models, queues),
            )
            busy = self.rows(
                " union all ".join(
                    f"select '{q}' queue, model, coalesce({ctx_sql}, 0) ctx, priority, dashboard, id from work.{q}_items "
                    "where state = 'claimed' and claimed_by = %s"
                    for q in workq.QUEUES
                ),
                (gpu,) * len(workq.QUEUES),
            )
            load_s = {
                r["model"]: r["avg_s"] for r in self.rows("select model, avg_s from work.model_loads where gpu = %s", (gpu,))
            }
            for q in queues:
                cands = workq.rank(self.rows(workq.claim_sql(q), (models, gpu)), self.tab_order())
                if busy:
                    cands = workq.busy_filter(cands, q, busy, self.tab_order(), same_ok=not body.get("outrank_only"))
                c = workq.pick(cands, loaded, since, load_s, time.time(), models)
                if not c:
                    continue
                got = self.rows(
                    f"update work.{q}_items set state = 'claimed', claimed_by = %s, claimed_at = now(), "
                    "lease_until = now() + make_interval(secs => %s), attempt = attempt + 1, progress = null, error = null "
                    "where id = %s and state = 'ready' returning id, study, parent_id, step, kind, model, priority, payload, attempt",
                    (gpu, LEASE_S, c["id"]),
                )
                if not got:
                    continue  # changed by its submitter a moment ago; the next claim looks again
                self.conn().execute(
                    "insert into work.study_turns(queue, study, last_at) values(%s, %s, now()) "
                    "on conflict (queue, study) do update set last_at = now()",
                    (q, got[0]["study"]),
                )
                return dict(got[0], queue=q, lease_s=LEASE_S, yields=q in yielding(res), finish_s=YIELD_FINISH_S)
        return None

    def wait_claim(self, host: str, body: dict, wait: float) -> dict | None:
        end = time.time() + min(max(wait, 0), WAIT_MAX)
        while True:
            item = self.work_claim(host, body)
            if item or time.time() >= end:
                return item
            time.sleep(1)

    def wait_claim_many(self, host: str, body: dict, n: int, wait: float) -> list[dict]:
        """Up to n items in one call (2026-11-02: a GPU on a lossy link keeps a local queue, so it isn't waiting on a round
        trip per item). After the first, the GPU counts as busy, so every item is for the same model and context.
        With queue_s, the batch is sized in time, not count: what the GPU already holds plus the new items stays within
        queue_s, at each model's measured seconds per item on that GPU. Once it holds that much, only work that outranks
        everything it holds is handed out (it runs next in the local queue). (2026-11-02: one average over all models
        let a GPU fresh from 3-s reads take 20 thinking-on reads of 30-60 s each, and the judge calls waited behind them.)"""
        budget = float(body.get("queue_s") or 0)
        held_s = sum(self.est_s(body["gpu"], r["model"], r["ctx"]) for r in self.held_rows(body["gpu"])) if budget else 0.0
        first = self.wait_claim(host, dict(body, outrank_only=True) if budget and held_s >= budget else body, wait)
        if not first:
            return []
        items = [first]
        held_s += self.est_s(body["gpu"], first["model"], self.item_ctx(first))
        while len(items) < n and (not budget or held_s < budget):
            nxt = self.work_claim(host, body)  # the GPU is busy now, so the busy rule keeps the same model and context
            if not nxt:
                break
            items.append(nxt)
            held_s += self.est_s(body["gpu"], nxt["model"], self.item_ctx(nxt))
        return items

    @staticmethod
    def item_ctx(it: dict) -> int:
        return int(((it.get("payload") or {}).get("body") or {}).get("options", {}).get("num_ctx") or 0)

    def est_s(self, gpu: str, model: str, ctx: int | None) -> float:
        default = JOB_S_DEFAULT if model.startswith("job:") else ITEM_S_DEFAULT
        return self.item_s.get((gpu, model, int(ctx or 0)), default)

    def held_rows(self, gpu: str) -> list[dict]:
        ctx_sql = "(payload #>> '{body,options,num_ctx}')::int"
        with self.lock:
            return self.rows(
                " union all ".join(
                    f"select model, coalesce({ctx_sql}, 0) ctx from work.{q}_items where state = 'claimed' and claimed_by = %s"
                    for q in workq.QUEUES
                ),
                (gpu,) * len(workq.QUEUES),
            )

    def work_sync(self, gpu: str, body: dict) -> dict:
        """One round trip for a GPU's local queue: finished results in, leases renewed for everything it still holds,
        and the refs it must drop (paused, cancelled, handed out again, or a backup item giving way)."""
        t0 = time.time()
        with self.lock:  # a syncing GPU is alive: its expired leases don't use up attempts
            self.conn().execute("update work.gpus set last_seen = now() where gpu = %s", (gpu,))
        acks = {}
        for r in body.get("results") or []:
            verb = "done" if "result" in r else "failed" if "error" in r else "release"
            code, _ = self.work_update(r["queue"], int(r["id"]), gpu, verb, r)
            acks[f"{r['queue']}/{r['id']}"] = code
        drop, levels = [], {}
        for h in body.get("held") or []:
            code, res = self.work_update(h["queue"], int(h["id"]), gpu, "beat", h)
            if code != 200 or (res or {}).get("cancel"):
                drop.append(f"{h['queue']}/{h['id']}")
            elif (res or {}).get("priority") is not None:
                levels[f"{h['queue']}/{h['id']}"] = res["priority"]
        dt = time.time() - t0
        if dt > 1:
            log(f"sync {gpu}: {len(acks)} results, {len(body.get('held') or [])} held in {dt:.1f} s")
        return {"acks": acks, "drop": drop, "levels": levels, "lease_s": LEASE_S}

    def work_update(self, q: str, item_id: int, gpu: str, verb: str, body: dict) -> tuple[int, dict]:
        """beat / done / failed / release from the worker that holds the claim. 409 = it no longer holds it."""
        if q not in workq.QUEUES:
            return 404, {"error": "no such queue"}
        held = "where id = %s and state = 'claimed' and claimed_by = %s"
        with self.lock:
            if verb == "beat":
                r = self.rows(
                    f"update work.{q}_items set lease_until = now() + make_interval(secs => %s), progress = %s {held} returning id, priority",
                    (LEASE_S, body.get("progress"), item_id, gpu),
                )
                if r and q in yielding(reservation()) and not finishing(body) and self.better_waiting(q, gpu):
                    self.conn().execute(
                        f"update work.{q}_items set state = 'ready', claimed_by = null, lease_until = null, "
                        f"attempt = greatest(attempt - 1, 0), progress = null {held}",
                        (item_id, gpu),
                    )
                    log(f"work {q} {item_id}: gives way on {gpu} to waiting work in an earlier queue")
                    return 200, {"cancel": True, "yielded": True}
                # paused, cancelled or handed out again: stop working on it. The current level goes back too: a submitter may
                # raise or lower it after the claim, and a GPU's local queue orders by it.
                return 200, {"cancel": not r, "priority": r[0]["priority"] if r else None}
            if verb == "release":  # Low Power, a worker stopping, or an aborted call: back to ready, the attempt not counted
                r = self.rows(
                    f"update work.{q}_items set state = 'ready', claimed_by = null, lease_until = null, "
                    f"attempt = greatest(attempt - 1, 0), progress = null {held} returning id",
                    (item_id, gpu),
                )
            elif verb == "done":
                stats = body.get("stats") or {}
                with self.conn().transaction():
                    r = self.rows(
                        f"update work.{q}_items set state = 'done', finished_at = now(), lease_until = null, stats = %s, "
                        f"progress = null {held} returning id, model",
                        (Jsonb(stats), item_id, gpu),
                    )
                    if r:
                        self.conn().execute(
                            "insert into work.results(queue, item_id, result) values(%s, %s, %s) "
                            "on conflict (queue, item_id) do update set result = excluded.result, at = now()",
                            (q, item_id, Jsonb(body.get("result"))),
                        )
                        self.record_load(gpu, r[0]["model"], stats.get("load_s"))
                        secs = float(stats.get("total_s") or 0) - float(stats.get("load_s") or 0)
                        if secs > 0:  # seconds per item, for sizing this GPU's batches of this model and context
                            k = (gpu, r[0]["model"], int(stats.get("ctx") or 0))
                            self.item_s[k] = secs if k not in self.item_s else 0.7 * self.item_s[k] + 0.3 * secs
            elif verb == "failed":
                r = self.rows(
                    f"update work.{q}_items set state = case when %s and attempt < max_attempts then 'ready' else 'failed' end, "
                    "error = %s, claimed_by = null, lease_until = null, "
                    f"finished_at = case when %s and attempt < max_attempts then null else now() end {held} returning id, state",
                    (bool(body.get("retry")), str(body.get("error"))[:2000], bool(body.get("retry")), item_id, gpu),
                )
                if r:
                    log(f"work {q} {item_id}: failed on {gpu} -> {r[0]['state']}: {str(body.get('error'))[:200]}")
            else:
                return 404, {"error": "no such action"}
        return (200, {"ok": True}) if r else (409, {"error": "this worker no longer holds the item"})

    def tab_order(self) -> dict:
        """{dashboard id: order} from the wrapper, cached for a minute; the last good answer is kept if it's unreachable."""
        now = time.time()
        if now - getattr(self, "_tabs_at", 0) > 60:
            self._tabs_at = now
            try:
                tabs = json.load(urllib.request.urlopen(TABS_URL, timeout=3)).get("tabs") or []
                self._tabs = {t["id"]: int(t.get("order") or 0) for t in tabs if t.get("id")}
            except Exception as e:
                log(f"dashboard tab order unreachable ({type(e).__name__}); keeping the last one")
        return getattr(self, "_tabs", {})

    def better_waiting(self, q: str, gpu: str) -> bool:
        """Is there ready work this GPU serves (its models, its queues) in a table ahead of q?"""
        earlier = workq.QUEUES[: workq.QUEUES.index(q)]
        sql = " union all ".join(
            f"select 1 from work.{e}_items i join work.gpus g on g.gpu = %s "
            "where i.state = 'ready' and i.attempt < i.max_attempts and i.model = any(g.models) "
            f"and (g.queues is null or '{e}' = any(g.queues))"
            for e in earlier
        )
        return bool(earlier) and bool(self.rows(f"select exists({sql}) e", (gpu,) * len(earlier))[0]["e"])

    def record_load(self, gpu: str, model: str, load_s: float | None) -> None:
        """Ollama's load_duration when a call had to load the model: the switch cost the claim weighs."""
        if not load_s or load_s < 1:
            return  # already loaded
        self.conn().execute(
            "insert into work.model_loads(gpu, model, n, avg_s, max_s, last_s) values(%s, %s, 1, %s, %s, %s) "
            "on conflict (gpu, model) do update set n = work.model_loads.n + 1, "
            "avg_s = (work.model_loads.avg_s * least(work.model_loads.n, 19) + excluded.avg_s) / (least(work.model_loads.n, 19) + 1), "
            "max_s = greatest(work.model_loads.max_s, excluded.max_s), last_s = excluded.last_s, last_at = now()",
            (gpu, model, load_s, load_s, load_s),
        )


def reservation(
    now: float | None = None, until: str | None = None, who: str | None = None, mode: str | None = None
) -> dict | None:
    """The active reservation, or None: {until, for, mode, priority_only, fill_idle_s}."""
    until = RESERVE_UNTIL if until is None else until
    if not until:
        return None
    try:
        end = datetime.fromisoformat(until).timestamp()
    except ValueError:
        return None
    if (time.time() if now is None else now) >= end:
        return None
    mode = RESERVE_MODE if mode is None else mode
    return {
        "until": until,
        "for": RESERVE_FOR if who is None else who,
        "mode": mode,
        "priority_only": mode == "exclusive",
        "fill_idle_s": FILL_IDLE_S,
    }


def claim_queues(queues: list[str], res: dict | None, idle_s: float) -> list[str]:
    """The queues a GPU may claim from now. Under a reservation, standard and backup only once the GPU has gone the fill
    time with no priority work (the worker sends idle_s; an older worker sends none, so it gets priority work only)."""
    if res and (res["priority_only"] or idle_s < res["fill_idle_s"]):
        return [q for q in queues if q == "priority"]
    return queues


def yielding(res: dict | None) -> tuple:
    """Queues whose running work gives way to ready work in an earlier queue: backup always; standard during a fill reservation."""
    return workq.YIELDING + (("standard",) if res and not res["priority_only"] else ())


def finishing(held: dict) -> bool:
    """Held work with under YIELD_FINISH_S of estimated run time left finishes instead of giving way. An Ollama call reports
    no estimate: it always counts as finishing."""
    left = held.get("remaining_s")
    return left is None or float(left) <= YIELD_FINISH_S


def serve(b: Broker, port: int = PORT, tokens: dict | None = None) -> ThreadingHTTPServer:
    tokens = host_tokens() if tokens is None else tokens
    route = re.compile(
        r"^/api/v1/agent/servers/([A-Za-z0-9_.-]+)/(orders|report|work/claim|work/sync|work/([a-z]+)/(\d+)/([a-z]+))$"
    )

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive: workers reuse one connection instead of paying a TCP connect per call
        timeout = 90  # an idle kept-alive connection is closed after this (longer than a 25-s long-poll plus slack)

        def handle(self) -> None:
            try:
                super().handle()
            finally:
                b.close_conn()

        def log_message(self, *a: object) -> None:
            pass

        def reply(self, code: int, body: object = None) -> None:
            b = b"" if body is None else json.dumps(body, default=str).encode()
            self.send_response(code)
            if b:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def check(self) -> re.Match | None:
            u = urlparse(self.path)
            m = route.match(u.path)
            if not m or m.group(1) not in b.hosts:
                self.reply(404, {"error": "not found"})
                return None
            code = auth(self.headers.get("Authorization"), m.group(1), tokens)
            if code != 200:
                self.reply(code, {"error": "unauthorized" if code == 401 else "token is for another server"})
                return None
            return m

        def body(self) -> dict | None:
            try:
                return json.loads(self._raw or b"{}")
            except ValueError:
                self.reply(400, {"error": "bad JSON"})
                return None

        def do_GET(self) -> None:
            if urlparse(self.path).path in ("/", "/status"):
                return self.reply(200, status(b))
            hit = self.check()
            if not hit or hit[2] != "orders":
                return None if not hit else self.reply(405, {"error": "POST"})
            q = parse_qs(urlparse(self.path).query)
            since, wait = int((q.get("version") or ["0"])[0]), float((q.get("wait") or ["0"])[0])
            o = b.wait_orders(hit[1], since, wait)
            self.reply(304) if o is None else self.reply(200, o)

        def do_POST(self) -> None:
            # read the whole body first: a reply sent without reading it would corrupt the next request on a kept-alive connection
            self._raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if (self.headers.get("Content-Encoding") or "").lower() == "gzip":  # workers on a lossy link compress large bodies
                try:
                    self._raw = gzip.decompress(self._raw)
                except OSError:
                    self._raw = b"not json"
            hit = self.check()
            if not hit:
                return
            if hit[2] == "orders":
                return self.reply(405, {"error": "GET"})
            body = self.body()
            if body is None:
                return
            if hit[2] == "report":
                b.report(hit[1], body)
                return self.reply(200, {"ok": True})
            if hit[2] == "work/claim":
                if not body.get("gpu") or not body.get("models"):
                    return self.reply(400, {"error": "gpu and models are required"})
                if body.get("n"):  # a batch for a local queue
                    items = b.wait_claim_many(hit[1], body, max(1, min(int(body["n"]), 50)), float(body.get("wait") or 0))
                    return self.reply(200, {"items": items, "lease_s": LEASE_S}) if items else self.reply(204)
                item = b.wait_claim(hit[1], body, float(body.get("wait") or 0))
                return self.reply(204) if item is None else self.reply(200, item)
            if hit[2] == "work/sync":
                if not body.get("gpu"):
                    return self.reply(400, {"error": "gpu is required"})
                return self.reply(200, b.work_sync(body["gpu"], body))
            if not body.get("gpu"):
                return self.reply(400, {"error": "gpu is required"})
            self.reply(*b.work_update(hit[3], int(hit[4]), body["gpu"], hit[5], body))

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    srv.daemon_threads = True
    return srv


def status(b: Broker) -> dict:
    """Read-only, open on the LAN like the runner's :8098: hosts, active orders and the placement backlog."""
    return {
        "at": int(time.time()),
        "hosts": b.rows("select host, version, last_seen from broker.hosts order by host"),
        "orders": b.rows(
            "select name, host, lane, request_id, placed_at from broker.orders where removed_at is null order by host, name"
        ),
        "load": b.load(),
        "gpu_held": sorted(b.held),
        "auto_accept": b.auto_accept,
        "place_requests": b.place_requests,
        "dashboard_order": b.tab_order(),  # ranks standard and backup work
        "reservation": reservation(),  # while set, priority work first; others fill idle GPUs (mode fill)
        "work": b.rows("select * from work.depth order by queue, model, priority desc, state"),
        "gpus": b.rows("select gpu, host, model, ctx, model_since, last_seen from work.gpus order by gpu"),
        "model_loads": b.rows("select * from work.model_loads order by gpu, model"),
    }


if __name__ == "__main__":
    if not HOSTS:
        sys.exit("RUN_BROKER_HOSTS is empty: nothing to place on (see .env.example)")
    broker = Broker()
    threading.Thread(target=serve(broker).serve_forever, daemon=True).start()
    log(
        f"run broker: hosts {', '.join(HOSTS)}, database '{DSN}', :{PORT}, GPU held {sorted(GPU_HELD) or 'none'}, "
        f"request placement {'on' if PLACE_REQUESTS else 'off'}, work queues on"
    )
    while True:
        try:
            broker.tick()
        except Exception as e:  # never die on one bad row; log and carry on
            log(f"tick error: {type(e).__name__}: {e}")
            broker.local.db = None
        time.sleep(TICK)
