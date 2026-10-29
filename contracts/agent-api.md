# Agent API contract: control container ⇄ per-server agent (version 1)

**This file is the one source of truth** for the API between the control container (owner: the Recovery page thread; `hub/`, FastAPI) and the per-server agent (owner: Server Services; the task runner growing into the agent).

- The container repo pins this file by commit and keeps no copy.
- A runnable reference of the container side is [`mocks/agent_api.py`](../mocks/agent_api.py), with tests in [`tests/test_agent_api.py`](../tests/test_agent_api.py). Both sides test against it.
- A breaking change bumps the version and is announced to the other side before it's pushed. Fields are added freely, never renamed or removed within a version. Unknown fields are ignored.
- Agreed 2026-10-29 by both owners (git tag `agent-api-v1`). Nothing implements it on a live server yet; go-lives are the owner's call.

**Direction (owner, 2026-10-29):**
- ONE control container holds the REST API, the database access and the queue for every server.
- Each server runs as few system services as possible, ideally ONE queue-driven agent. The agent asks the container what should run, applies it on the host, and reports back.
- A server keeps running its **last known orders** when the container is unreachable: depend, never block.

## 1. Who does what

| | Control container (Recovery) | Per-server agent (Server Services) |
|---|---|---|
| Runs as | Docker, one instance | bare OS (systemd), one per server: the **task runner grows into the agent** |
| Holds | desired state for every server (Postgres database `sjc`, own role), the command queue, the work queue, history | a local copy of its own orders (local Postgres `taskrunner`), so it boots and runs without the container |
| Talks | answers HTTP | **pulls** (the container never connects to a server); long-poll keeps commands fast |
| Hardware | none | everything host-level: systemd, ipmitool/DCMI, RAPL, `nvidia-smi`, jobs |

Why pull and not push:
- Servers behind NAT (Tug) work unchanged.
- No inbound port is needed on any server.
- The container can restart freely: agents just retry.

## 2. Address, auth, clocks

- **Base URL:** `AGENT_API_URL` + `/api/v1/agent`. During the transition that's the Recovery stack's nginx (`http://<hub>:8100`), which proxies to the backend with a 40 s read timeout and no buffering. When the container takes over :8090, it's that address. `AGENT_API_URL` defaults to `HUB_API_URL`.
- **Auth:**
  - Every request carries `Authorization: Bearer <token>`, one token per server: `<HOST>_AGENT_TOKEN` in this repo's `.env`.
  - The container stores only its SHA-256: `AGENT_TOKEN_SHA256_<HOST>` in the container's `.env`.
  - A bad or missing token gets `401`; a valid token used for another server's `{host}` gets `403`.
  - The home-LAN gate applies as well (Tug arrives as a 10.x address; the router is excluded).
  - Later: OIDC client credentials from the IdP instead of static tokens.
- **Clocks:** expiry, lease and order times use **the container's clock only**. Every `at` an agent sends is for information.
- Times are Unix seconds (float). Bodies are JSON (UTF-8).

## 3. Endpoints

| Method + path | Purpose | Body → reply |
|---|---|---|
| `GET /servers/{host}/orders?version=N&wait=25` | Desired state, long-poll: `200` at once if the host's version > N, else `304` with an **empty body** after `wait` s (max 25) | → `{version, at, tasks: [...], settings: {...}}` (section 4) |
| `POST /servers/{host}/report` | State, every 10 s, plus at once after a change | `{at, orders_version, tasks: [...], runs: [...], health: {...}}` (section 5) → `{ok: true}` |
| `GET /servers/{host}/commands?wait=25` | Claim one-off commands, oldest first, long-poll; an empty list after `wait` s | → `[{id, cmd, args, by, queued_at, expires_at}]` |
| `POST /servers/{host}/commands/{id}/result` | Finish a command (section 6) | `{ok: true, ...detail}` or `{ok: false, refused: "<reason>"}` or `{ok: false, error: "<text>"}` → `{ok: true}`; `409` if the command already expired or finished |
| `POST /queues/{queue}/claim` | Work items (section 7) | `{worker, n, lease_s}` → `[{id, payload, attempt, lease_until}]` |
| `POST /queues/items/{id}/done` | Finish a work item | `{result}` → `{ok: true}`; `409` if the lease ran out |
| `POST /queues/items/{id}/failed` | Fail a work item | `{error, retry: bool}` → `{ok: true}`; `409` if the lease ran out |

## 4. Orders (desired state)

```json
{
  "version": 412,
  "at": 1793300000.0,
  "tasks": [
    {"name": "video-vlm", "command": "exec ionice -c3 ...", "workdir": "...", "env": {}, "kind": "continuous",
     "instances": 3, "instances_by_power": {"1000": 1, "1500": 3}, "every_secs": null, "at_times": null,
     "needs": {"paths": ["/b"], "gpu": true}, "nice": 10, "stop_secs": 30, "enabled": true}
  ],
  "settings": {"keep_days": 30}
}
```

- **`version`:**
  - one counter per host, bumped on any change to that host's tasks or settings;
  - it only goes up and survives container restarts;
  - the agent stores the last applied version with its local orders, so after a reboot it asks `version=N` and gets `304` if nothing changed.
- **`tasks`:**
  - The fields of the runner's `tasks` table (see [`services/taskrunner/README.md`](../services/taskrunner/README.md)).
  - The agent writes them into its local table in **one transaction**; the runner loop then applies them as it does today.
  - The list is the **complete** managed set: a container-managed row missing from new orders is disabled and then removed.
  - **Server-local rows** (made with `taskctl`, marked `origin='local'`) are never touched by a sync, so hand-made changes still work while the container is down.
- **Never in the orders:** run mode, power target and the GPU switch. They stay on today's :8090 endpoints (`/api/powertarget`, `/api/runprofile`, `/api/gpu`), read by fan-control, power-guard, gpu-switch and dash-agent. Orders never change them as a side effect. fan-control and power-guard stay their own units.

## 5. Report

```json
{
  "at": 1793300010.0,
  "orders_version": 412,
  "tasks": [{"name": "video-vlm", "origin": "container", "enabled": true, "wanted": 3, "instances_running": 3,
             "state": {"0": "running", "1": "running", "2": "running"}, "last_start": 1793299000.0, "last_exit": 0}],
  "runs": [{"task": "video-vlm", "instance": 1, "pid": 4242, "started_at": 1793299000.0, "ended_at": null, "exit_code": null}],
  "health": {"fan_control": {"running": true, "mode": "run-adaptive", "emergency": false}, "uptime_s": 86400}
}
```

- `tasks[]`: per-task actual state, so the container can show drift between orders and reality.
  - `wanted` = copies after `instances_by_power`.
  - `last_exit` = the exit code of the most recent finished run (`null` if none).
- `runs[]`: `task_runs` rows new or changed since the last accepted report.
- `health.fan_control`: the safety floor at a glance, from fan-control's state file.
  - `running` = the state is fresher than 30 s.
  - `emergency` = any sensor in Emergency.
  - `mode` = its current mode.
- While the container is unreachable, reports queue locally (capped by count and age) and go in order when it's back.

## 6. Commands (one-off)

- **At most once:** a command is handed to one claim only. If it's not finished by `expires_at`, the container marks it `expired`, never hands it out again, and answers a late result with `409`.
- **Expiry, set by the container at queue time:** hardware commands (`fan.override`, `gpu.limit`) expire **30 s after they're queued**, so a stale fan command never runs late. The others expire after 10 min.
- The agent checks `expires_at` against the container's time (the `Date` header of the claim reply) and drops anything already expired.
- **Results:**
  - `{ok: true, ...}` = done;
  - `{ok: false, refused: "<reason>"}` = deliberately not done (a rule said no);
  - `{ok: false, error: "<text>"}` = tried and failed.

| `cmd` | Args | What the agent does | Refused when |
|---|---|---|---|
| `task.restart` | `{name, instance?}` | stops the copies; the loop restarts them | unknown task |
| `task.run_now` | `{name}` | runs a `schedule` task once now | not a schedule task |
| `unit.restart` | `{unit}` | `systemctl restart <unit>` | unit not in this server's allow-list (`<HOST>_AGENT_UNITS`) |
| `fan.override` | `{pct, minutes, by}` | same as the sjc-agent's `fan_override` | outside fan-control's limits, or an Emergency is active |
| `gpu.limit` | `{watts, minutes, by}` | same as the sjc-agent's `gpu_limit` | outside the card's min/max |
| `agent.resync` | `{}` | re-pulls orders with `version=0` | never |

**REST is the one durable command path:**
- The container's `/api/v1/servers/{host}/fans` and `/gpu-limit` routes enqueue a command and wait up to about 15 s for its result; otherwise they answer `202 {id}`, and `GET /api/v1/commands/{id}` gives the outcome later.
- MQTT ([`mqtt-topics.md`](mqtt-topics.md)) stays for **live state** only. Its `cmd/*` topics retire with MQTT contract v2, announced first; until then the container may still publish them.

## 7. Work queue (in the container's database)

- `claim` leases up to `n` items for `lease_s` seconds (container clock). An item whose lease runs out goes back to `ready`, with `attempt` + 1.
- `done` stores the result.
- `failed` with `retry: true` returns the item to `ready` until the queue's `max_attempts`; after that, or with `retry: false`, it stays `failed` for a person.
- **Independence:** a worker claims a batch (about 10 minutes of work). If the container goes away, it finishes the batch and waits, while continuous tasks and anything not queue-fed keep running on local orders.
- Queues are filled by the container (from the catalogue in MySQL), never by agents.

## 8. When the container is unreachable

- The agent keeps running the last applied orders (local Postgres); the runner loop and the :8098 status page keep working.
- It retries orders and commands every 10 s, backing off to 60 s. No boot step waits for the container.
- `taskctl` keeps working on the server, as the local override.

## 9. Transition (Server Services side)

Each step is its own branch, tested against the mock. Each host step goes through the single thread.

1. The runner gets an orders-sync thread plus `origin` on task rows. Without `AGENT_API_URL` and a token set, it behaves exactly as today.
2. Reports, then commands.
3. Fewer units: helper loops that need no early boot (peak-power, imc-bw, nvme-watch, gpu-perf, gpu-switch) become runner tasks, one at a time, each with its own announced window.
   **fan-control stays its own unit on every server**: it must start at `basic.target` with nothing else running, and it's the temperature safety floor. power-guard stays a unit until the container can take its decision logic.
4. dash-agent's state collection moves into the agent's report, and its HTTP :8091 is retired once the container reads reports instead.
