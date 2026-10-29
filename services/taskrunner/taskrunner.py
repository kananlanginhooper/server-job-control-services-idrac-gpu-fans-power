#!/usr/bin/python3
# One task runner per server (2026-10-28: "maybe one service for the server that monitors tasks... then entries using
# postgres or mysql" + "build this as a template, and use it on all three servers"). Same code on the hub, GPU server and GPU desktop.
#
# Reads the `tasks` table from the LOCAL Postgres database `taskrunner` (unix socket, peer auth as the runner's OS user, no
# passwords), so each server starts and resumes its own job work with the other machines down. Every few seconds it makes the
# running processes match the table:
#   continuous  keep `instances` copies running; a crashed copy is restarted with back-off (30 s doubling to 15 min, reset after
#               10 min of healthy running)
#   schedule    run once every `every_secs` (one copy at a time)
#   oneshot     run once after the row is enabled or changed (a successful run ends it)
# needs (jsonb): {"paths": [...]} every path must answer (checked off the main loop, so a hung mount never blocks the runner),
#                {"http": [...]} every URL must answer, {"gpu": true} = do not START while the power target says Low Power
#                (scale=down; running copies are left to finish their item). A missing need = wait quietly, never an error.
# Processes are started in their own process group (TASK_NAME / TASK_INSTANCE in the environment) and stopped with SIGTERM,
# then SIGKILL after `stop_secs`. Logs: <LOG_DIR>/<task>.<instance>.log, trimmed at 50 MB (one .1 kept).
# Read-only status: http://<host>:8098/  (JSON: tasks + live processes). Changes go through the table (taskctl).
import json, os, signal, socket, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import psycopg

HOST = socket.gethostname()
DSN = os.environ.get("TASKRUNNER_DSN", "dbname=taskrunner")
LOG_DIR = os.environ.get("TASKRUNNER_LOGS", os.path.expanduser("~/taskrunner/logs"))
POWER_URL = os.environ.get("TASKRUNNER_POWER_URL", os.environ.get("HUB_API_URL", "http://localhost:8090") + "/api/powertarget")
PORT = int(os.environ.get("TASKRUNNER_PORT", "8098"))
TICK, BACKOFF_MIN, BACKOFF_MAX, HEALTHY_SECS, LOG_MAX = 5, 30, 900, 600, 50_000_000

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
create or replace function tasks_touch() returns trigger language plpgsql as
  $$ begin new.updated_at := now(); return new; end $$;
drop trigger if exists tasks_touch on tasks;
create trigger tasks_touch before update on tasks for each row
  when (old.* is distinct from new.*) execute function tasks_touch();
"""


def log(msg):
    print(time.strftime("%F %T"), msg, flush=True)


class Need:
    """Need checks run in a background thread; the main loop only reads the last result (a hung NFS path can't stall it)."""
    def __init__(self):
        self.ok, self.why, self.lock, self.wanted = {}, {}, threading.Lock(), {}
        threading.Thread(target=self.loop, daemon=True).start()

    def want(self, name, needs):
        with self.lock:
            self.wanted[name] = needs

    def get(self, name):
        with self.lock:
            return self.ok.get(name, False), self.why.get(name, "checking")

    @staticmethod
    def path_ok(p):
        try:
            return subprocess.run(["timeout", "15", "stat", "-t", p + "/."], capture_output=True).returncode == 0
        except Exception:
            return False

    @staticmethod
    def http_ok(u):
        try:
            urllib.request.urlopen(u, timeout=5).read(64)
            return True
        except Exception:
            return False

    @staticmethod
    def low_power():
        try:
            return json.load(urllib.request.urlopen(POWER_URL, timeout=5)).get("scale") == "down"
        except Exception:
            return False                          # power API unreachable: don't hold work back (the guard still trims)

    def loop(self):
        while True:
            with self.lock:
                wanted = dict(self.wanted)
            lp = None
            for name, n in wanted.items():
                why = None
                for p in n.get("paths") or []:
                    if not self.path_ok(p):
                        why = f"waiting for {p}"; break
                if not why:
                    for u in n.get("http") or []:
                        if not self.http_ok(u):
                            why = f"waiting for {u}"; break
                if not why and n.get("gpu"):
                    lp = self.low_power() if lp is None else lp
                    if lp:
                        why = "Low Power: GPU work does not start"
                with self.lock:
                    self.ok[name], self.why[name] = why is None, why or "ok"
            time.sleep(10)


class Proc:
    def __init__(self, task, inst, popen, run_id):
        self.task, self.inst, self.p, self.run_id, self.t0, self.stopping = task, inst, popen, run_id, time.time(), None


class Runner:
    def __init__(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        self.db = None
        self.procs = {}                  # (task, inst) -> Proc
        self.backoff = {}                # (task, inst) -> (next_start_at, delay)
        self.last_sched = {}             # task -> last start time (schedule)
        self.tasks = {}
        self.need = Need()
        self.state = {}                  # (task, inst) -> short text for the status page

    # ---------- database ----------
    def conn(self):
        if self.db is None or self.db.closed:
            self.db = psycopg.connect(DSN, autocommit=True)
            self.db.execute(SCHEMA)
            log("connected to the task database")
        return self.db

    def q(self, sql, args=()):
        try:
            cur = self.conn().execute(sql, args)
            return cur.fetchall() if cur.description else None
        except psycopg.Error as e:
            log(f"database: {e}".splitlines()[0])
            self.db = None
            return None

    def load(self):
        rows = self.q("select name, command, workdir, env, kind, instances, every_secs, needs, nice, stop_secs, enabled, "
                      "extract(epoch from updated_at) from tasks")
        if rows is None:
            return False                 # keep running what we have; retry next tick
        self.tasks = {r[0]: dict(zip(("name", "command", "workdir", "env", "kind", "instances", "every_secs", "needs", "nice",
                                      "stop_secs", "enabled", "updated"), r)) for r in rows}
        for t in self.tasks.values():
            self.need.want(t["name"], t["needs"] or {})
        return True

    # ---------- processes ----------
    def start(self, t, inst):
        key = (t["name"], inst)
        path = f"{LOG_DIR}/{t['name']}.{inst}.log"
        try:
            if os.path.getsize(path) > LOG_MAX:
                os.replace(path, path + ".1")
        except OSError:
            pass
        env = dict(os.environ, TASK_NAME=t["name"], TASK_INSTANCE=str(inst), **{k: str(v) for k, v in (t["env"] or {}).items()})
        out = open(path, "ab")
        out.write(f"\n==== {time.strftime('%F %T')} start {t['name']}.{inst} on {HOST}\n".encode()); out.flush()
        nice = t["nice"] or 0
        try:
            p = subprocess.Popen(["/bin/bash", "-c", t["command"]], cwd=t["workdir"] or None, env=env, stdout=out, stderr=out,
                                 stdin=subprocess.DEVNULL, start_new_session=True, preexec_fn=lambda: os.nice(nice))
        except OSError as e:
            log(f"{t['name']}.{inst}: cannot start: {e}")
            self.fail(key)
            return
        finally:
            out.close()
        r = self.q("insert into task_runs(task, instance, pid) values(%s, %s, %s) returning id", (t["name"], inst, p.pid))
        self.procs[key] = Proc(t["name"], inst, p, r[0][0] if r else None)
        if t["kind"] == "schedule":
            self.last_sched[t["name"]] = time.time()
        log(f"started {t['name']}.{inst} pid {p.pid}")

    def stop(self, pr, why):
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

    def fail(self, key):
        nxt, delay = self.backoff.get(key, (0, BACKOFF_MIN / 2))
        delay = min(BACKOFF_MAX, delay * 2)
        self.backoff[key] = (time.time() + delay, delay)

    def reap(self):
        for key, pr in list(self.procs.items()):
            rc = pr.p.poll()
            if rc is None:
                if time.time() - pr.t0 > HEALTHY_SECS:
                    self.backoff.pop(key, None)
                continue
            del self.procs[key]
            self.q("update task_runs set ended_at=now(), exit_code=%s where id=%s", (rc, pr.run_id))
            t = self.tasks.get(pr.task) or {}
            if pr.stopping is not None:
                log(f"{pr.task}.{pr.inst} stopped")
            elif t.get("kind") == "continuous" or rc != 0:
                log(f"{pr.task}.{pr.inst} exited {rc}; restart back-off")
                self.fail(key)
            if t.get("kind") == "oneshot" and rc == 0:
                self.q("update tasks set enabled=false where name=%s and extract(epoch from updated_at) <= %s",
                       (pr.task, t.get("updated", 0)))

    def tick(self):
        self.reap()
        if not self.load():
            return
        now = time.time()
        for key, pr in list(self.procs.items()):          # stop what is no longer wanted
            t = self.tasks.get(pr.task)
            if not t or not t["enabled"] or pr.inst >= t["instances"]:
                self.stop(pr, "disabled or fewer instances")
            elif pr.stopping is not None:
                self.stop(pr, "")
        for t in self.tasks.values():
            if not t["enabled"]:
                continue
            ok, why = self.need.get(t["name"])
            n = 1 if t["kind"] in ("schedule", "oneshot") else max(0, t["instances"])
            for inst in range(n):
                key = (t["name"], inst)
                if key in self.procs:
                    self.state[key] = "running"; continue
                if not ok:
                    self.state[key] = why; continue
                if now < self.backoff.get(key, (0, 0))[0]:
                    self.state[key] = f"restart in {int(self.backoff[key][0] - now)} s"; continue
                if t["kind"] == "schedule" and now - self.last_sched.get(t["name"], 0) < (t["every_secs"] or 3600):
                    self.state[key] = "scheduled"; continue
                self.start(t, inst)
        for key in list(self.state):
            t = self.tasks.get(key[0])
            if not t or not t["enabled"] or key[1] >= (1 if t["kind"] != "continuous" else t["instances"]):
                self.state.pop(key, None)

    def status(self):
        return {"host": HOST, "at": int(time.time()),
                "tasks": [{"name": t["name"], "kind": t["kind"], "enabled": t["enabled"], "instances": t["instances"],
                           "running": sum(1 for k in self.procs if k[0] == t["name"]),
                           "state": {str(k[1]): v for k, v in sorted(self.state.items()) if k[0] == t["name"]}}
                          for t in sorted(self.tasks.values(), key=lambda x: x["name"])]}

    def shutdown(self, *_):
        log("shutting down: stopping every task")
        for pr in self.procs.values():
            self.stop(pr, "runner stopping")
        end = time.time() + 30
        while self.procs and time.time() < end:
            self.reap(); time.sleep(1)
        for pr in self.procs.values():
            try:
                os.killpg(pr.p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        sys.exit(0)


def serve(runner):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            b = json.dumps(runner.status()).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()


if __name__ == "__main__":
    r = Runner()
    signal.signal(signal.SIGTERM, r.shutdown)
    threading.Thread(target=serve, args=(r,), daemon=True).start()
    log(f"task runner on {HOST}, database '{DSN}', logs {LOG_DIR}, status :{PORT}")
    while True:
        try:
            r.tick()
        except Exception as e:                      # never die on one bad row; log and carry on
            log(f"tick error: {type(e).__name__}: {e}")
        time.sleep(TICK)
