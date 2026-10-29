# Task runner, next steps: work queue, dashboard feeds, control API

Design for the three items still open after wall-clock schedules, power sizing and pruning. The owner decides; nothing here is built yet. Every step keeps the rule the runner was built on: **each server boots and runs its own work with the other machines down.**

## 1. Dashboard feeds (smallest; build first)

**Goal:** every task shows up on the dashboard with a graph.

- The runner already knows each task's state. Once a minute it posts one entry per enabled task to `HUB_API_URL/api/tasks`, in the same format `services/tools/task-report.py` uses:
  - `id = taskrunner/<host>/<task>`, `group = <host>`, `owner = taskrunner`;
  - `state`: `running`, `blocked` (a need is missing; its reason goes in `message`), `paused` (disabled), or `failed` (in back-off);
  - `done`/`total` = running/wanted copies, with `unit = items`;
  - `heartbeat_s = 180`, so a dead runner shows as stale.
- Jobs that report their own progress (`task-report.py`, `job-progress`) keep doing it. The runner's entry is the "is it alive and sized right" line next to them, and `parent` links the two.
- **Fire and forget:** a 2-s timeout in a background thread. If the hub is down, nothing is retried and nothing blocks.
- **Graphs:** the dashboard keeps a history of `/api/tasks` posts. The runner adds nothing for graphs beyond posting regularly. If the dashboard thread wants a time series per task, `task_runs` (starts, exits, exit codes) can be served read-only from the control API (section 3).
- **Needs the dashboard thread:** confirm `/api/tasks` keeps history (or say which endpoint does), and agree on the `id` scheme.

## 2. Work queue (the real "task queue")

**Problem today:** each job claims work straight from MySQL on the hub (`SELECT ... FOR UPDATE SKIP LOCKED`), and file-reading stages reach the GPU server's files over NFS. Remote workers therefore need hub database logins and shared paths, and they stop when the hub or the share is down.

**Proposal:** one queue per server, in that server's own `taskrunner` Postgres, plus a small claim/complete HTTP API the runner serves.

```sql
create table work_items(
  id bigserial primary key,
  queue text not null,                 -- e.g. 'video-vlm', 'catalog-hash'
  payload jsonb not null,              -- whatever the job needs: a path on THIS server, a row id, ...
  state text not null default 'ready' check (state in ('ready','claimed','done','failed')),
  priority int not null default 0,
  attempts int not null default 0,
  claimed_by text, claimed_at timestamptz, lease_secs int not null default 3600,
  result jsonb, error text,
  created_at timestamptz not null default now(), finished_at timestamptz
);
create index on work_items(queue, state, priority desc, id) where state = 'ready';
```

- **Claim:** `POST /queue/<queue>/claim {worker, n}` runs `update ... where id in (select id ... for update skip locked limit n) returning ...`. A claim is a lease: an item whose lease runs out goes back to `ready` (the runner sweeps every minute). That replaces each job's own "running > 6 h" logic.
- **Complete:** `POST /queue/item/<id>/done {result}` or `/failed {error}`. A failure retries up to N attempts, then stays `failed` for a person to look at.
- **Where the items live:** on the server that **has the files**, so file stages run where the files are, with no NFS. A GPU worker elsewhere gets the payload over HTTP and fetches only the bytes it needs, from the same API, by item id.
- **Who fills the queues:** a small `schedule` task per pipeline that selects new work from the catalogue (MySQL on the hub) and enqueues it on the right server. That's the only step that needs the hub, and if the hub is down, the queues already filled keep draining.
- **Results:** they go back to the hub's MySQL through a feeder task. If the hub is down, they wait in `work_items.result`.
- **Migration:** one pipeline stage at a time, starting with the video VLM stage (the bottleneck, and its claim logic is simple). The old MySQL claim path keeps working until a stage moves.

## 3. Control API + auth

The status page (:8098) stays read-only and open on the LAN, as now. The new queue and control routes need auth, because **writing tasks = running commands as the service user** (and as root through `sudo -n`).

- **Routes:**
  - `GET /api/tasks`, `GET /api/tasks/<name>/runs`;
  - `POST /api/tasks/<name>/{enable,disable}`, `PUT /api/tasks/<name>/instances`;
  - the queue routes from section 2.
- **No route changes `command`, `workdir` or `env`.** Those stay `taskctl`-only on the server itself, so a leaked token can pause work but can't run new code.
- **Auth:**
  - a per-server bearer token in the shared `.env` (`<HOST>_TASKRUNNER_TOKEN`), checked with `hmac.compare_digest`;
  - workers and the dashboard backend hold the token, and browsers never see it.
  - Later, the wrapper thread's OIDC provider can issue these tokens instead.
- **Server shape:** stdlib `ThreadingHTTPServer` as today, or FastAPI per [`refactor/fastapi-proposal.md`](refactor/fastapi-proposal.md) (taskrunner is on its "worth it" list). Either way the API runs in a thread of the runner and never blocks the loop.

## Order and risk

| Step | Size | Risk | Needs |
|---|---|---|---|
| 1. Dashboard feeds | small | low (fire and forget) | the dashboard thread's OK on `id`/history |
| 3a. Read-only API routes (`/api/tasks`, runs) | small | low | nothing |
| 3b. Token + control routes | medium | medium (control surface) | owner's OK; a token in `.env` |
| 2. Work queue, first stage (video VLM) | large | medium | owner's OK; the video pipeline's claim code |

Each step gets its own branch, a mock or Docker test like `scripts/taskrunner-e2e.sh`, and the normal cutover: GPU desktop first, then the GPU server, then the hub.
