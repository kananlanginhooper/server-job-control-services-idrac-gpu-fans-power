# Task runner

**Runs on:** every server (as the service user)

The one job service per server. It keeps the task rows in a **local** Postgres `tasks` table running (continuous workers, one-shots, schedules, instance counts), restarts crashed tasks with back-off, waits quietly when a task's need is missing, logs each run in `task_runs`, and serves status JSON on :8098. CLI: `taskctl`. It stays local on purpose: each server starts and resumes its own work with the other machines down.

It is **not** a work queue: it keeps job *processes* running, and each job claims its own work (for example with `SELECT ... FOR UPDATE SKIP LOCKED`). Infrastructure services (dashboards, fan control, power guard, model servers, databases) stay plain systemd units on purpose.

| | |
|---|---|
| Reads | Local Postgres (`TASKRUNNER_DSN`, peer auth); `HUB_API_URL/api/powertarget`. |
| Writes | Child processes; logs in `~/taskrunner/logs`; :8098. |
| Settings | `TASKRUNNER_DSN`, `TASKRUNNER_PORT`, `TASKRUNNER_LOGS`, `TASKRUNNER_POWER_URL` (default `HUB_API_URL/api/powertarget`). Install: `install.sh`. |

Settings come from the repo's one `.env` (see [`.env.example`](../../.env.example)). The unit file(s) here are templates: `scripts/install-units.sh` renders `@REPO@`/`@USER@` and installs them. It doesn't enable or start them.

## Database (local Postgres, db `taskrunner`)

Peer auth over the local Unix socket as the service user: no passwords anywhere. The runner creates the schema on start (`SCHEMA` in `taskrunner.py`):

```sql
create table tasks(
  name text primary key,
  description text,
  command text not null,                 -- run with /bin/bash -c; code must be local to this server
  workdir text,
  env jsonb not null default '{}',
  kind text not null default 'continuous' check (kind in ('continuous','schedule','oneshot')),
  instances int not null default 1,
  every_secs int,                        -- schedule only
  needs jsonb not null default '{}',
  nice int not null default 10,
  stop_secs int not null default 30,
  enabled boolean not null default true,
  updated_at timestamptz not null default now()   -- trigger tasks_touch keeps it current
);
create table task_runs(
  id bigserial primary key, task text not null, instance int not null, pid int,
  started_at timestamptz not null default now(), ended_at timestamptz, exit_code int, note text);
```

`task_runs` is append-only and never pruned.

## How the runner works

Main loop every **5 s**:

1. **Reap** finished processes: record `ended_at` / `exit_code` in `task_runs`.
2. **Load** all rows from `tasks`. If the DB is unreachable, keep running what is running and retry next tick.
3. **Stop** processes whose task is gone, disabled, or above its `instances`: `SIGTERM` to the process group, `SIGKILL` after `stop_secs`.
4. **Start** what is missing:
   - `continuous`: keep `instances` copies (instance numbers 0..n-1) running.
   - `schedule`: one copy, started when `every_secs` have passed since the last start **by this runner process**.
   - `oneshot`: one copy; a clean exit (code 0) sets `enabled=false` unless the row was edited meanwhile.
5. A task starts only when its **needs** are met. They're checked in a background thread every 10 s, so a hung network path can't stall the loop:
   - `paths`: each path must answer `stat` within 15 s;
   - `http`: each URL must answer within 5 s;
   - `gpu: true`: don't START while the power target says Low Power (`scale=down`). If the power API is unreachable, the need counts as met. Running copies are not stopped.

   A missing need means the task waits quietly ("waiting for /path"), never an error.
6. **Back-off:** a `continuous` task that exits (any code), or any task that exits non-zero, restarts after 30 s, doubling up to 15 min. The delay resets after 10 min of healthy running.

Each task runs as `/bin/bash -c <command>` in its own process group, as the service user, with `nice` applied, cwd = `workdir`, and env = the runner's env + `TASK_NAME`, `TASK_INSTANCE` + the row's `env`. Root jobs put `sudo -n` in the command; I/O priority goes in the command (`ionice -c3`). Logs: `~/taskrunner/logs/<task>.<instance>.log`, appended, with a `==== start` line per start, trimmed at 50 MB (one `.1` kept). The runner's own log: `journalctl -u taskrunner`.

Status page (read-only, no auth, LAN): `http://<host>:8098/` returns `{host, at, tasks:[{name, kind, enabled, instances, running, state:{instance: text}}]}`.

## taskctl

```bash
taskctl list                                   # name, kind, enabled, running/wanted, state (from the status page)
taskctl add NAME --cmd 'CMD' [--workdir D] [--env K=V ...] [--kind continuous|schedule|oneshot] [--instances N]
            [--every SECS] [--need-path P ...] [--need-http URL ...] [--gpu] [--nice N] [--desc TEXT] [--disabled]
taskctl set NAME [--instances N] [--cmd ...] [--every SECS] [--nice N] [--desc TEXT]     # also --env / --need-*
taskctl enable NAME | disable NAME | rm NAME
taskctl runs NAME [N]                          # last N task_runs rows
taskctl log NAME [INSTANCE] [LINES]            # tail of the task's log
```

Changes take effect within ~5 s. Editing the table with `psql -d taskrunner` directly is equivalent.

## Security model

- No passwords: Postgres peer auth. Jobs use their own option files.
- The runner runs as the service user; anything needing root uses `sudo -n` inside the task command.
- The :8098 status page is unauthenticated and read-only. Nothing on it changes state.
- Anyone who can write the `tasks` table can run commands as the service user (and as root through `sudo -n`): **treat DB access as shell access.**

## Gotchas: read before changing anything

1. **Schedules are relative, not wall-clock.** `every_secs` counts from the last start by *this runner process*, held in memory. A runner restart (or `install.sh`, or a reboot) runs every `schedule` task again immediately, heavy ones included.
2. **Restarting the runner restarts every task** (`KillMode=mixed`: TERM, wait up to 30 s, KILL). Work a job had claimed as `running` is released by that job's own stale-claim logic, or by hand.
3. **Continuous tasks restart even after a clean exit.** STOP-file conventions don't stop a task permanently. **Stop a task with `taskctl disable`.**
4. **No per-task resource limits.** All tasks share the runner's cgroup; there are no CPU, memory or I/O caps beyond `nice`/`ionice` in the command. **Instance counts are set by hand**: the runner does not size them to the power target. The `gpu` need only gates *starts* in Low Power.
5. **`task_runs` grows forever.**
6. **`taskctl set --env` / `--need-*` replace the whole JSON field** (no merge).
7. Editing a task row changes `updated_at`; for a `oneshot` task that means "run again".
8. Server clocks can differ (UTC vs local), so log and `task_runs` timestamps may not line up across servers.
9. Never `pkill -f` over ssh with a pattern that appears in your own command line (it kills your ssh); kill by PID.

## Making changes safely

1. Change code here, never on a server only.
2. For a task change: `taskctl set/disable/enable`, then check `taskctl list` and `taskctl log NAME`.
3. For runner code: test on the GPU desktop first (fewest tasks), then the GPU server, then the hub. `install.sh` restarts the runner, which restarts all its tasks and re-runs schedules (gotchas 1 and 2), so pick a quiet moment on the hub.
4. Afterwards: `taskctl list` shows every enabled task `running` or `scheduled`, and `journalctl -u taskrunner --since -10min | grep "restart back-off"` is quiet.

## Not built yet (toward a real task queue)

- Wall-clock schedules (`at` times, persisted last run) instead of in-memory relative ones.
- A work-queue table plus a small authenticated claim/complete API, so remote workers need neither database logins nor shared paths.
- Power-aware instance sizing from `/api/powertarget`.
- Per-task dashboard feeds from the runner.
- `task_runs` pruning; auth on the status/control API.
