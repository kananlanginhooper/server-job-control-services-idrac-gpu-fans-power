#!/usr/bin/python3
# One task runner per server (2026-10-28: "maybe one service for the server that monitors tasks... then entries using
# postgres or mysql" + "build this as a template, and use it on all three servers"). Same code on the hub, GPU server and GPU desktop.
#
# Reads the `tasks` table from the LOCAL Postgres database `taskrunner` (unix socket, peer auth as the runner's OS user, no
# passwords), so each server starts and resumes its own job work with the other machines down. Every few seconds it makes the
# running processes match the table:
#   continuous  keep `instances` copies running; a crashed copy is restarted with back-off (30 s doubling to 15 min, reset after
#               10 min of healthy running)
#   schedule    run once every `every_secs`, or at each wall-clock time in `at_times` ('HH:MM', TASKRUNNER_TZ); one copy at a
#               time. The last start comes from task_runs, so a runner restart does not re-run schedules; a slot missed
#               while the server was down (or a need was missing) runs once when it can
#   oneshot     run once after the row is enabled or changed (a successful run ends it)
# instances_by_power (jsonb, continuous only): {"<combined_w>": instances}; the step at or below the power target's
#                combined_w wins (below the lowest step: the lowest step). Power API unreachable = the row's `instances`.
#                Scaling down stops the extra copies like `taskctl set --instances` does.
# needs (jsonb): {"paths": [...]} every path must answer (checked off the main loop, so a hung mount never blocks the runner),
#                {"http": [...]} every URL must answer, {"gpu": true} = do not START while the power target says Low Power
#                (scale=down; running copies are left to finish their item). A missing need = wait quietly, never an error.
# task_runs older than TASKRUNNER_KEEP_DAYS (default 30, 0 = keep all) are pruned hourly; each task's latest run is kept.
# Processes are started in their own process group (TASK_NAME / TASK_INSTANCE in the environment) and stopped with SIGTERM,
# then SIGKILL after `stop_secs`. Logs: <LOG_DIR>/<task>.<instance>.log, trimmed at 50 MB (one .1 kept).
# Read-only status: http://<host>:8098/  (JSON: tasks + live processes). Changes go through the table (taskctl).
# Run broker (2026-10-30, off unless TASKRUNNER_BROKER_URL is set): the hub's broker decides what runs where. This runner then
# long-polls its orders (agent API section 4) into the local tasks table as origin='broker' rows, and reports its state and every
# run start/exit (section 5). Rows made with taskctl (origin='local') are never touched by a sync. The local table stays the
# runner's only input, so with the broker down it keeps running its last orders; unsent run changes wait in task_runs.
from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, tzinfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

try:
    import psycopg
except ImportError:  # the pure schedule/sizing helpers below are testable without it
    psycopg = None

# the shared .env: svcenv.py sits next to the installed copy (install.sh), or in the repo's lib/ when run from a clone
_here = os.path.dirname(os.path.realpath(__file__))
sys.path[:0] = [_here, os.path.join(_here, "..", "..", "lib")]
from svcenv import env  # noqa: E402

HOST = socket.gethostname()
DSN = env("TASKRUNNER_DSN", "dbname=taskrunner")
LOG_DIR = env("TASKRUNNER_LOGS", os.path.expanduser("~/taskrunner/logs"))
POWER_URL = env("TASKRUNNER_POWER_URL", env("HUB_API_URL", "http://localhost:8090") + "/api/powertarget")
PORT = int(env("TASKRUNNER_PORT", "8098"))
TZ = ZoneInfo(env("TASKRUNNER_TZ")) if env("TASKRUNNER_TZ") else None  # None = the server's local time
KEEP_DAYS = int(env("TASKRUNNER_KEEP_DAYS", "30"))
BROKER_URL = env("TASKRUNNER_BROKER_URL", "").rstrip("/")  # e.g. http://<hub>:8099/api/v1/agent; empty = no broker
BROKER_HOST = env("TASKRUNNER_BROKER_HOST", HOST.split(".")[0].lower())  # this server's name at the broker
REPORT_SECS, REPORT_BATCH = 10, 500
TICK, BACKOFF_MIN, BACKOFF_MAX, HEALTHY_SECS, LOG_MAX, PRUNE_SECS = 5, 30, 900, 600, 50_000_000, 3600

SCHEMA = """
create table if not exists tasks(
  name text primary key,
  description text,
  command text not null,                       -- run with /bin/bash -c; code must be local to this server
  workdir text,
  env jsonb not null default '{}',
  kind text not null default 'continuous' check (kind in ('continuous', 'schedule', 'oneshot')),
  instances int not null default 1,
  every_secs int,                              -- schedule only
  needs jsonb not null default '{}',
  nice int not null default 10,
  stop_secs int not null default 30,
  enabled boolean not null default true,
  updated_at timestamptz not null default now()
);
alter table tasks add column if not exists at_times text[];             -- schedule: daily 'HH:MM' (overrides every_secs)
alter table tasks add column if not exists instances_by_power jsonb;    -- continuous: {"<combined_w>": instances}
alter table tasks add column if not exists restart_gen int not null default 0;  -- taskctl restart bumps it: running copies restart
create table if not exists task_runs(
  id bigserial primary key,
  task text not null,
  instance int not null,
  pid int,
  started_at timestamptz not null default now(),
  ended_at timestamptz,
  exit_code int,
  note text
);
create index if not exists task_runs_task on task_runs(task, started_at desc);
alter table tasks add column if not exists origin text not null default 'local';  -- 'broker' = synced from the run broker
alter table tasks add column if not exists order_hash text;                       -- broker rows: the order they came from
create sequence if not exists task_runs_change_seq;
alter table task_runs add column if not exists change_seq bigint;                 -- report cursor: every insert/update bumps it
create or replace function task_runs_seq() returns trigger language plpgsql as
  $$ begin new.change_seq := nextval('task_runs_change_seq'); return new; end $$;
drop trigger if exists task_runs_seq on task_runs;
create trigger task_runs_seq before insert or update on task_runs for each row execute function task_runs_seq();
create table if not exists agent_state(key text primary key, value jsonb not null);  -- orders_version, report cursor
create or replace function tasks_touch() returns trigger language plpgsql as
  $$ begin new.updated_at := now(); return new; end $$;
drop trigger if exists tasks_touch on tasks;
create trigger tasks_touch before update on tasks for each row
  when (old.* is distinct from new.*) execute function tasks_touch();
"""


def log(msg: str) -> None:
    print(time.strftime("%F %T"), msg, flush=True)


def at_slots(at_times: list[str], now: float, tz: tzinfo | None = TZ) -> tuple[float, float]:
    """(latest slot at or before now, next slot after now) as epoch seconds, for daily 'HH:MM' times in tz."""
    today = datetime.fromtimestamp(now, tz).date()
    prev, nxt = 0.0, float("inf")
    for s in at_times:
        hh, mm = (int(x) for x in s.split(":"))
        for d in (-1, 0, 1):
            dt = datetime(today.year, today.month, today.day, hh, mm, tzinfo=tz) + timedelta(days=d)
            ts = dt.timestamp()
            if ts <= now:
                prev = max(prev, ts)
            else:
                nxt = min(nxt, ts)
    return prev, nxt


def schedule_due(t: dict, last: float | None, now: float, tz: tzinfo | None = TZ) -> tuple[bool, str]:
    """Is a schedule task due? `last` = its last recorded start (None = never ran)."""
    if t.get("at_times"):
        prev, nxt = at_slots(t["at_times"], now, tz)
        # never ran: wait for the first slot after the row was written instead of running at once
        since = last if last is not None else t.get("updated", now)
        if since < prev:
            return True, ""
        return False, "next " + datetime.fromtimestamp(nxt, tz).strftime("%a %H:%M")
    if last is None or now - last >= (t.get("every_secs") or 3600):
        return True, ""
    return False, "scheduled"


def wanted_instances(t: dict, power: dict | None) -> int:
    """How many copies a task should have now: 1 for schedule/oneshot; continuous follows instances_by_power if set."""
    if t["kind"] != "continuous":
        return 1
    n = t["instances"]
    steps = t.get("instances_by_power")
    w = (power or {}).get("combined_w")
    if steps and isinstance(w, (int, float)):
        steps = sorted((int(k), int(v)) for k, v in steps.items())
        n = steps[0][1]
        for k, v in steps:
            if w >= k:
                n = v
    return max(0, n)


class Need:
    """Need checks run in a background thread; the main loop only reads the last result (a hung NFS path can't stall it)."""

    def __init__(self, start: bool = True) -> None:
        self.ok, self.why, self.lock, self.wanted = {}, {}, threading.Lock(), {}
        self.power = None  # last /api/powertarget answer (None = unreachable or not needed)
        self.sized = False  # some task uses instances_by_power: fetch the power target even with no gpu need
        if start:
            threading.Thread(target=self.loop, daemon=True).start()

    def want(self, name: str, needs: dict) -> None:
        with self.lock:
            self.wanted[name] = needs

    def get(self, name: str) -> tuple[bool, str]:
        with self.lock:
            return self.ok.get(name, False), self.why.get(name, "checking")

    @staticmethod
    def path_ok(p: str) -> bool:
        try:
            return subprocess.run(["timeout", "15", "stat", "-t", p + "/."], capture_output=True).returncode == 0
        except Exception:
            return False

    @staticmethod
    def http_ok(u: str) -> bool:
        try:
            urllib.request.urlopen(u, timeout=5).read(64)
            return True
        except Exception:
            return False

    @staticmethod
    def fetch_power() -> dict | None:
        try:
            return json.load(urllib.request.urlopen(POWER_URL, timeout=5))
        except Exception:
            return None  # power API unreachable: don't hold work back (the guard still trims)

    def get_power(self) -> dict | None:
        with self.lock:
            return self.power

    def loop(self) -> None:
        while True:
            with self.lock:
                wanted = dict(self.wanted)
                sized = self.sized
            power = self.fetch_power() if sized or any(n.get("gpu") for n in wanted.values()) else None
            with self.lock:
                self.power = power
            lp = (power or {}).get("scale") == "down"
            for name, n in wanted.items():
                why = None
                for p in n.get("paths") or []:
                    if not self.path_ok(p):
                        why = f"waiting for {p}"
                        break
                if not why:
                    for u in n.get("http") or []:
                        if not self.http_ok(u):
                            why = f"waiting for {u}"
                            break
                if not why and n.get("gpu"):
                    if lp:
                        why = "Low Power: GPU work does not start"
                with self.lock:
                    self.ok[name], self.why[name] = why is None, why or "ok"
            time.sleep(10)


class Proc:
    def __init__(self, task: str, inst: int, popen: subprocess.Popen, run_id: int | None, gen: int = 0) -> None:
        self.task, self.inst, self.p, self.run_id, self.t0, self.stopping = task, inst, popen, run_id, time.time(), None
        self.gen = gen  # the row's restart_gen when this copy started


class Runner:
    def __init__(self) -> None:
        os.makedirs(LOG_DIR, exist_ok=True)
        self.db = None
        self.procs = {}  # (task, inst) -> Proc
        self.backoff = {}  # (task, inst) -> (next_start_at, delay)
        self.last_sched = {}  # task -> last start time (schedule); seeded from task_runs so restarts don't re-run
        self.seeded = False
        self.last_prune = 0.0
        self.want = {}  # task -> copies wanted at the last tick (instances_by_power applied)
        self.tasks = {}
        self.need = Need()
        self.state = {}  # (task, inst) -> short text for the status page
        self.changed = threading.Event()  # a run started or ended: the broker reporter sends at once

    # ---------- database ----------
    def conn(self) -> psycopg.Connection | None:
        if self.db is None or self.db.closed:
            self.db = psycopg.connect(DSN, autocommit=True)
            self.db.execute(SCHEMA)
            log("connected to the task database")
        return self.db

    def q(self, sql: str, args: tuple = ()) -> list[tuple] | None:
        try:
            cur = self.conn().execute(sql, args)
            return cur.fetchall() if cur.description else None
        except psycopg.Error as e:
            log(f"database: {e}".splitlines()[0])
            self.db = None
            return None

    def load(self) -> bool:
        rows = self.q(
            "select name, command, workdir, env, kind, instances, every_secs, needs, nice, stop_secs, enabled, "
            "extract(epoch from updated_at), at_times, instances_by_power, restart_gen, origin from tasks"
        )
        if rows is None:
            return False  # keep running what we have; retry next tick
        if not self.seeded:
            last = self.q("select task, extract(epoch from max(started_at))::float8 from task_runs group by task")
            if last is None:
                return False
            for name, ts in last:
                self.last_sched.setdefault(name, ts)
            self.seeded = True
        self.tasks = {
            r[0]: dict(
                zip(
                    (
                        "name",
                        "command",
                        "workdir",
                        "env",
                        "kind",
                        "instances",
                        "every_secs",
                        "needs",
                        "nice",
                        "stop_secs",
                        "enabled",
                        "updated",
                        "at_times",
                        "instances_by_power",
                        "restart_gen",
                        "origin",
                    ),
                    r,
                )
            )
            for r in rows
        }
        for t in self.tasks.values():
            self.need.want(t["name"], t["needs"] or {})
        with self.need.lock:
            self.need.sized = any(t["instances_by_power"] and t["enabled"] for t in self.tasks.values())
        return True

    def prune(self) -> None:
        """Hourly: drop task_runs older than KEEP_DAYS, always keeping each task's latest run (its schedule anchor)."""
        if KEEP_DAYS <= 0 or time.time() - self.last_prune < PRUNE_SECS:
            return
        self.last_prune = time.time()
        r = self.q(
            "with gone as (delete from task_runs r where r.started_at < now() - make_interval(days => %s) "
            "and exists (select 1 from task_runs n where n.task = r.task and n.started_at > r.started_at) returning 1) "
            "select count(*) from gone",
            (KEEP_DAYS,),
        )
        if r and r[0][0]:
            log(f"pruned {r[0][0]} task_runs rows older than {KEEP_DAYS} days")

    # ---------- processes ----------
    def start(self, t: dict, inst: int) -> None:
        key = (t["name"], inst)
        path = f"{LOG_DIR}/{t['name']}.{inst}.log"
        try:
            if os.path.getsize(path) > LOG_MAX:
                os.replace(path, path + ".1")
        except OSError:
            pass
        env = dict(os.environ, TASK_NAME=t["name"], TASK_INSTANCE=str(inst), **{k: str(v) for k, v in (t["env"] or {}).items()})
        out = open(path, "ab")
        out.write(f"\n==== {time.strftime('%F %T')} start {t['name']}.{inst} on {HOST}\n".encode())
        out.flush()
        nice = t["nice"] or 0
        try:
            p = subprocess.Popen(
                ["/bin/bash", "-c", t["command"]],
                cwd=os.path.expanduser(t["workdir"]) if t["workdir"] else None,
                env=env,
                stdout=out,
                stderr=out,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                preexec_fn=lambda: os.nice(nice),
            )
        except OSError as e:
            log(f"{t['name']}.{inst}: cannot start: {e}")
            self.fail(key)
            return
        finally:
            out.close()
        r = self.q("insert into task_runs(task, instance, pid) values(%s, %s, %s) returning id", (t["name"], inst, p.pid))
        self.changed.set()
        self.procs[key] = Proc(t["name"], inst, p, r[0][0] if r else None, t.get("restart_gen") or 0)
        if t["kind"] == "schedule":
            self.last_sched[t["name"]] = time.time()
        log(f"started {t['name']}.{inst} pid {p.pid}")

    def stop(self, pr: Proc, why: str) -> None:
        if pr.stopping is None:
            pr.stopping = time.time()
            log(f"stopping {pr.task}.{pr.inst} ({why})")
            try:
                os.killpg(pr.p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        elif time.time() - pr.stopping > (self.tasks.get(pr.task) or {}).get("stop_secs", 30):
            try:
                os.killpg(pr.p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def fail(self, key: tuple[str, int]) -> None:
        nxt, delay = self.backoff.get(key, (0, BACKOFF_MIN / 2))
        delay = min(BACKOFF_MAX, delay * 2)
        self.backoff[key] = (time.time() + delay, delay)

    def reap(self) -> None:
        for key, pr in list(self.procs.items()):
            rc = pr.p.poll()
            if rc is None:
                if time.time() - pr.t0 > HEALTHY_SECS:
                    self.backoff.pop(key, None)
                continue
            del self.procs[key]
            self.q("update task_runs set ended_at=now(), exit_code=%s where id=%s", (rc, pr.run_id))
            self.changed.set()
            t = self.tasks.get(pr.task) or {}
            if pr.stopping is not None:
                log(f"{pr.task}.{pr.inst} stopped")
            elif t.get("kind") == "continuous" or rc != 0:
                log(f"{pr.task}.{pr.inst} exited {rc}; restart back-off")
                self.fail(key)
            if t.get("kind") == "oneshot" and rc == 0:
                self.q(
                    "update tasks set enabled=false where name=%s and extract(epoch from updated_at) <= %s",
                    (pr.task, t.get("updated", 0)),
                )

    def tick(self) -> None:
        self.reap()
        if not self.load():
            return
        self.prune()
        now = time.time()
        power = self.need.get_power()
        want = {name: wanted_instances(t, power) for name, t in self.tasks.items()}
        for _key, pr in list(self.procs.items()):  # stop what is no longer wanted
            t = self.tasks.get(pr.task)
            if not t or not t["enabled"] or pr.inst >= want[pr.task]:
                self.stop(pr, "disabled or fewer instances")
            elif pr.gen < (t.get("restart_gen") or 0):
                self.stop(pr, "restart requested")  # started again by the loop below once it has exited
            elif pr.stopping is not None:
                self.stop(pr, "")
        for t in self.tasks.values():
            if not t["enabled"]:
                continue
            ok, why = self.need.get(t["name"])
            n = want[t["name"]]
            for inst in range(n):
                key = (t["name"], inst)
                if key in self.procs:
                    self.state[key] = "running"
                    continue
                if not ok:
                    self.state[key] = why
                    continue
                if now < self.backoff.get(key, (0, 0))[0]:
                    self.state[key] = f"restart in {int(self.backoff[key][0] - now)} s"
                    continue
                if t["kind"] == "schedule":
                    due, wait = schedule_due(t, self.last_sched.get(t["name"]), now)
                    if not due:
                        self.state[key] = wait
                        continue
                self.start(t, inst)
        for key in list(self.state):
            t = self.tasks.get(key[0])
            if not t or not t["enabled"] or key[1] >= want[key[0]]:
                self.state.pop(key, None)
        self.want = want

    def status(self) -> dict:
        return {
            "host": HOST,
            "at": int(time.time()),
            "tasks": [
                {
                    "name": t["name"],
                    "kind": t["kind"],
                    "origin": t.get("origin") or "local",
                    "enabled": t["enabled"],
                    "instances": t["instances"],
                    "wanted": self.want.get(t["name"], t["instances"]) if t["enabled"] else 0,
                    "running": sum(1 for k in self.procs if k[0] == t["name"]),
                    "state": {str(k[1]): v for k, v in sorted(self.state.items()) if k[0] == t["name"]},
                }
                for t in sorted(self.tasks.values(), key=lambda x: x["name"])
            ],
        }

    def shutdown(self, *_: object) -> None:
        log("shutting down: stopping every task")
        for pr in self.procs.values():
            self.stop(pr, "runner stopping")
        end = time.time() + 30
        while self.procs and time.time() < end:
            self.reap()
            time.sleep(1)
        for pr in self.procs.values():
            try:
                os.killpg(pr.p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        sys.exit(0)


ORDER_COLS = ("command", "workdir", "env", "kind", "instances", "instances_by_power", "every_secs", "at_times", "needs", "nice",
              "stop_secs", "enabled")  # fmt: skip
JSON_COLS = ("env", "instances_by_power", "needs")
JSON_EMPTY = {"env": "{}", "needs": "{}"}  # not null in the tasks table


def order_hash(spec: dict) -> str:
    return hashlib.sha256(json.dumps({k: spec.get(k) for k in ("name", *ORDER_COLS)}, sort_keys=True).encode()).hexdigest()


def apply_orders(db: psycopg.Connection, orders: dict) -> list[str]:
    """Copy the broker's complete row set into the local tasks table in one transaction. A row is only rewritten when its order
    changed (so a oneshot this runner disabled after success stays disabled), local rows are never touched, and broker rows
    missing from the orders are removed (the loop stops their copies). -> names skipped because a local row has that name."""
    names, skipped = [], []
    cols = ", ".join(ORDER_COLS)
    with db.transaction():
        for spec in orders.get("tasks") or []:
            names.append(spec["name"])
            vals = [
                (json.dumps(spec[c]) if spec.get(c) is not None else JSON_EMPTY.get(c)) if c in JSON_COLS else spec.get(c)
                for c in ORDER_COLS
            ]
            r = db.execute(
                f"insert into tasks(name, {cols}, origin, order_hash) values(%s, {', '.join(['%s'] * len(ORDER_COLS))}, 'broker', %s) "
                f"on conflict (name) do update set {', '.join(f'{c} = excluded.{c}' for c in ORDER_COLS)}, order_hash = excluded.order_hash "
                "where tasks.origin = 'broker' and tasks.order_hash is distinct from excluded.order_hash returning origin",
                (spec["name"], *vals, order_hash(spec)),
            ).fetchall()
            if not r and db.execute("select 1 from tasks where name = %s and origin <> 'broker'", (spec["name"],)).fetchall():
                skipped.append(spec["name"])
        db.execute("delete from tasks where origin = 'broker' and not (name = any(%s))", (names,))
        db.execute(
            "insert into agent_state(key, value) values('orders_version', %s) on conflict (key) do update set value = excluded.value",
            (json.dumps(orders.get("version", 0)),),
        )
    return skipped


class BrokerLink:
    """Talks to the hub's run broker (agent API): one thread long-polls orders, one reports. Each has its own connection.
    Never blocks the runner loop; with the broker unreachable it retries every 10 s, backing off to 60 s."""

    def __init__(self, runner: Runner, url: str = BROKER_URL, host: str = BROKER_HOST, token: str | None = None) -> None:
        self.runner, self.url, self.host = runner, url, host
        self.token = env("AGENT_TOKEN", "") if token is None else token

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 15) -> tuple[int, dict | None]:
        req = urllib.request.Request(
            f"{self.url}/servers/{self.host}/{path}",
            data=None if body is None else json.dumps(body, default=str).encode(),
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            return e.code, None

    @staticmethod
    def state(db: psycopg.Connection, key: str, default: object) -> object:
        r = db.execute("select value from agent_state where key = %s", (key,)).fetchall()
        return r[0][0] if r else default

    def orders_loop(self) -> None:
        db, delay = None, 10
        while True:
            try:
                db = db if db and not db.closed else psycopg.connect(DSN, autocommit=True)
                code, o = self.call("GET", f"orders?version={self.state(db, 'orders_version', 0)}&wait=25", timeout=40)
                if code == 200 and o is not None:
                    skipped = apply_orders(db, o)
                    n = len(o.get("tasks") or [])
                    log(
                        f"broker orders v{o.get('version')}: {n} rows"
                        + (f"; local rows keep these names: {skipped}" if skipped else "")
                    )
                elif code != 304:
                    raise OSError(f"orders: HTTP {code}")
                delay = 10
            except Exception as e:
                log(f"broker: {type(e).__name__}: {e}".splitlines()[0])
                if isinstance(e, psycopg.Error):
                    db = None
                time.sleep(delay)
                delay = min(60, delay * 2)

    def report_body(self, db: psycopg.Connection) -> tuple[dict, int]:
        cursor = int(self.state(db, "report_cursor", 0))
        rows = db.execute(
            "select id, task, instance, pid, extract(epoch from started_at)::float8, extract(epoch from ended_at)::float8, exit_code, "
            "change_seq from task_runs where change_seq > %s order by change_seq limit %s",
            (cursor, REPORT_BATCH),
        ).fetchall()
        runs = [dict(zip(("id", "task", "instance", "pid", "started_at", "ended_at", "exit_code"), r[:7])) for r in rows]
        tasks = [
            {"name": t["name"], "origin": t["origin"], "enabled": t["enabled"], "wanted": t["wanted"],
             "instances_running": t["running"], "state": t["state"]}
            for t in self.runner.status()["tasks"]
        ]  # fmt: skip
        body = {
            "at": time.time(),
            "orders_version": self.state(db, "orders_version", 0),
            "tasks": tasks,
            "runs": runs,
            "health": health(),
        }
        return body, max((r[7] for r in rows), default=cursor)

    def report_loop(self) -> None:
        db, delay = None, 10
        while True:
            self.runner.changed.wait(REPORT_SECS)
            self.runner.changed.clear()
            try:
                db = db if db and not db.closed else psycopg.connect(DSN, autocommit=True)
                while True:  # after an outage, catch up in batches
                    body, cursor = self.report_body(db)
                    code, _ = self.call("POST", "report", body)
                    if code != 200:
                        raise OSError(f"report: HTTP {code}")
                    db.execute(
                        "insert into agent_state(key, value) values('report_cursor', %s) on conflict (key) do update set value = excluded.value",
                        (json.dumps(cursor),),
                    )
                    if len(body["runs"]) < REPORT_BATCH:
                        break
                delay = 10
            except Exception as e:
                log(f"broker: {type(e).__name__}: {e}".splitlines()[0])
                if isinstance(e, psycopg.Error):
                    db = None
                time.sleep(delay)
                delay = min(60, delay * 2)

    def start(self) -> None:
        threading.Thread(target=self.orders_loop, daemon=True).start()
        threading.Thread(target=self.report_loop, daemon=True).start()


def health() -> dict:
    """The safety floor at a glance (agent API section 5): fan-control's state file, read only."""
    out = {}
    try:
        out["uptime_s"] = int(float(open("/proc/uptime").read().split()[0]))
    except (OSError, ValueError):
        pass
    for path in ("/run/fan-control.json", "/dev/shm/fan-control.json"):
        try:
            age = time.time() - os.path.getmtime(path)
            st = json.load(open(path))
        except (OSError, ValueError):
            continue
        out["fan_control"] = {"running": age < 30, "mode": st.get("mode"), "emergency": bool(st.get("emergency"))}
        break
    return out


def serve(runner: Runner) -> None:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: object) -> None:
            pass

        def do_GET(self) -> None:
            b = json.dumps(runner.status()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()


if __name__ == "__main__":
    r = Runner()
    signal.signal(signal.SIGTERM, r.shutdown)
    threading.Thread(target=serve, args=(r,), daemon=True).start()
    if BROKER_URL:
        BrokerLink(r).start()
    log(f"task runner on {HOST}, database '{DSN}', logs {LOG_DIR}, status :{PORT}, broker {BROKER_URL or 'off'}")
    while True:
        try:
            r.tick()
        except Exception as e:  # never die on one bad row; log and carry on
            log(f"tick error: {type(e).__name__}: {e}")
        time.sleep(TICK)
