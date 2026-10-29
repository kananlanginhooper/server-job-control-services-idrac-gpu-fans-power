# Agent API contract: control container ⇄ per-server agent (version 2.1)

**This file is the one source of truth** for the API between the control container (owner: the Recovery page thread; `hub/`, FastAPI) and the per-server agent (owner: Server Services; the task runner growing into the agent).

- The container repo pins this file by commit and keeps no copy.
- A runnable reference of the container side is [`mocks/agent_api.py`](../mocks/agent_api.py), with tests in [`tests/test_agent_api.py`](../tests/test_agent_api.py). Both sides test against it.
- A breaking change bumps the version and is announced to the other side before it's pushed. Fields are added freely, never renamed or removed within a version. Unknown fields are ignored.
- History: v1 agreed 2026-10-29 (tag `agent-api-v1`). **v2** the same day (tag `agent-api-v2`), on the owner's decisions:
  - `fan.override` and `gpu.limit` are removed from the agent commands: fan override and the GPU power limit go through :8090, as standard operation;
  - emergencies stay purely local on each server;
  - the orders database is named `server_job_control_orders`.
  - **v2.1** (tag `agent-api-v2.1`, additive): per-GPU hold / release (section 10), agreed with the container's owner on the owner's requirement.

**Direction (owner, 2026-10-29):**
- ONE control container holds the REST API, the database access and the queue for every server.
- Each server runs as few system services as possible, ideally ONE queue-driven agent. The agent asks the container what should run, applies it on the host, and reports back.
- A server keeps running its **last known orders** when the container is unreachable: depend, never block.

## 1. Who does what

| | Control container (Recovery) | Per-server agent (Server Services) |
|---|---|---|
| Runs as | Docker, one instance | bare OS (systemd), one per server: the **task runner grows into the agent** |
| Holds | desired state for every server (Postgres database `server_job_control_orders`, own role), the command queue, the work queue, history | a local copy of its own orders (local Postgres `taskrunner`), so it boots and runs without the container |
| Talks | answers HTTP | **pulls** (the container never connects to a server); long-poll keeps commands fast |
| Hardware | none | everything host-level: systemd, ipmitool/DCMI, RAPL, `nvidia-smi`, jobs |

### The core safety layer (owner, 2026-10-29: permanent)

**power-guard, fan-control and gpu-switch are separate system services on every server where they run, permanently.** So is wherever the run mode and power target live, if not inside one of those three.
- They are never folded into the task runner or the agent, and they never depend on the control container.
- power-guard is the safety mechanism, and it sometimes operates fan-control.
- The agent only *reports* their state. It never commands or replaces them, and they keep working when the agent or the container is down.
- **Their controls stay on :8090** (owner: "standard operation"):
  - fan override and the GPU power limit;
  - the global modes: Quick Cool, meeting mode and the adaptive run profile;
  - power target and the GPU switch.
  The container may show them and forward a request to :8090, never send it to an agent.
- **Emergencies are purely local** in fan-control on each server, with every container down: "The server is in charge of maintaining itself within the specifications of temperature."

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
  - These are per-server pre-shared keys for now. Later the IdP replaces them (refresh tokens); the owner wants the server services and the container both behind the IdP eventually. That will be a v2 of this section.
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
- **Never in the orders:** run mode, power target and the GPU switch. They belong to the core safety layer (section 1): they stay on today's :8090 endpoints (`/api/powertarget`, `/api/runprofile`, `/api/gpu`), read by fan-control, power-guard, gpu-switch and dash-agent. Orders never change them, as a side effect or otherwise.

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
- **Expiry, set by the container at queue time:** 10 min. There are no hardware commands on this path (section 1).
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
| `agent.resync` | `{}` | re-pulls orders with `version=0` | never |

These four are the whole command set. Fan override and the GPU power limit are not agent commands: they go through :8090 (section 1).

MQTT ([`mqtt-topics.md`](mqtt-topics.md)) is for **live state only**. Its `cmd/*` topics are deprecated (no new users) and are removed in MQTT contract v2.

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
3. Fewer units: reporting helper loops that need no early boot (peak-power, imc-bw, nvme-watch, gpu-perf) may become runner tasks, one at a time, each with its own announced window.
   **Never moved: the core safety layer** (section 1). power-guard, fan-control and gpu-switch stay their own units permanently, and so does wherever the run mode and power target live. fan-control starts at `basic.target` with nothing else running.
4. dash-agent's state collection moves into the agent's report, and its HTTP :8091 is retired once the container reads reports instead.

## 10. GPU hold / release (v2.1)

**Requirement (owner):** per GPU, *hold* = stop new work, finish or requeue the current item, save progress, unload the models, so the GPU is free within ~90 s; *release* = start its workers again. Who, since when and why are visible, the hold survives restarts, and it never fights run mode or Low Power. The full requirement is in the task runner's operating notes (sections 11-12 of the current-state write-up); until this is built, the manual procedure there is the way.

**GPU ids** are `<host>-<model>`, as in the image pipeline's `gpu` values (e.g. `desk-gpu0`). The part before the first `-` is the **owning host** (the one whose memory the GPU is), matching its `HOST_KEY`.

### 10.1 Desired state, in every host's orders

```json
"settings": {"gpu_holds": [{"gpu": "desk-gpu0", "phase": "drain", "by": "owner", "since": 1793300000.0, "reason": "manual job"}]}
```

- The list is global: every host gets every hold, because a GPU's consumers can run on other hosts (the desktop GPU's VLM tasks run on the hub, through tunnels). Any change bumps **every** host's `version`.
- `phase`: `drain` → `free` → (release) `resume` → removed. The container moves it (10.3); agents act on it.

### 10.2 Which tasks use which GPU

- A task lists its GPUs in **`needs.gpus`**: a list of GPU ids. This is new and separate from `needs.gpu: true`, which stays the Low Power start gate, unchanged.
- A task is **on hold** while any of its GPUs has a hold in phase `drain`, `free` or `resume`. The runner stops it like a disable (SIGTERM, then SIGKILL after `stop_secs`), but **`enabled` is not changed**. When the hold is removed, the task starts again by itself, still subject to Low Power and its other needs.
- **Gates combine, never override:** a task runs only if it isn't on hold **and** Low Power / run mode allow it. A release during Low Power leaves the task paused; Low Power ending doesn't undo a hold. The GPU switch (`gpu_off` on :8090) is core safety and separate (section 1).

### 10.3 Phases (the container moves them from reports)

| Phase | Consumer hosts (tasks with the GPU in `needs.gpus`) | Owning host | Container moves on when |
|---|---|---|---|
| `drain` | stop those tasks | nothing yet (unloading now would just make a still-running consumer reload the model) | every consumer task reports `instances_running: 0`, **or** 60 s after `since`. At the timeout, a consumer still running stays in the owning host's `blockers`, and its orders stay "on hold"; nothing is killed harder |
| `free` | keep them stopped | unload the models from every Ollama instance of that GPU (`keep_alive: 0` for each model `/api/ps` lists), stop that GPU's units, report memory | - (stays until release) |
| `resume` | keep them stopped | start that GPU's units; report `hold: "active"` once they're up | the owning host reports `hold: "active"` → the hold is **removed** → consumers start by themselves |

When every consumer is on the owning host itself, it may go `drain` → `free` locally without waiting for the container's round trip.

**90 s budget:** a drain of ≤ 60 s (GPU tasks keep `stop_secs` ≤ 60), plus one report and long-poll round trip (about 1-2 s, since reports go out at once on change), plus an unload of about 10-20 s. If it isn't done by 90 s, the report's `blockers` say what still holds the GPU and the dashboard shows it. Nothing is escalated automatically.

### 10.4 Host-local GPU map (`.env`, not orders)

```
DESK_GPUS={"desk-gpu0": {"ollama": ["http://127.0.0.1:11434", "http://127.0.0.1:11437"], "units": ["tug-vision"]}}
```

`<HOST>_GPUS` names, per GPU the host owns, its Ollama instances and its units. The agent stops and starts only units in that list.

### 10.5 Local holds (with the container down)

- `taskctl gpu hold <gpu> --reason ...` / `taskctl gpu release <gpu>` on the owning host place a **local** hold. It's stored in the local database, acted on the same way (local consumers drain, then free), and reported with `origin: "local"`.
- Container and local holds combine (OR). A release only clears its own origin.
  - A dashboard release never clears a local hold. The container shows "held locally on `<host>`; release it there (`taskctl gpu release <gpu>`)" and never fakes a release.
  - A local hold only drains consumers on the owning host itself; other hosts learn of it when the container relays it.

### 10.6 Report

`health.gpus` lists every GPU the host owns:

```json
"gpus": [{"gpu": "desk-gpu0", "hold": "held", "since": 1793300000.0, "mem_used_mb": 310,
          "blockers": [], "holds": [{"origin": "container", "by": "owner", "since": 1793300000.0, "reason": "manual job"}]}]
```

- `hold` is `active`, `draining`, `held` or `resuming`. `held` with empty `blockers` means the memory is actually free.
- `blockers`: names of tasks (`<host>/<task>`) or units still holding the GPU.
- Consumer hosts report their consumer tasks as usual (`tasks[].instances_running`); that's what the container watches in `drain`.

### 10.7 Container side (owned by the Recovery page thread; listed here for reference)

`GET /api/v1/gpus` (holds plus reported state), `POST /api/v1/gpus/{gpu}/hold {reason}` and `POST /api/v1/gpus/{gpu}/release`. LAN only; `by` = the caller (the login, once the IdP is in). Stored in `server_job_control_orders`.

`GET /api/v1/gpus` returns a list with one entry per GPU that any host reports:

```json
[{"gpu": "desk-gpu0", "host": "desk", "hold": "held", "phase": "free",
  "holds": [{"origin": "lock", "by": "owner", "since": 1793300000.0, "reason": "offline run", "lock": "<lock name>", "releasable": false}],
  "blockers": [], "idle": false, "foreign": [], "mem_used_mb": 310, "reported_at": 1793300100.0}]
```

- `hold` is the owning host's reported state (10.6), and `phase` is the container's own (10.3), or `null` with no hold.
- `releasable` is `true` only for `origin: "container"`, so the UI needs no origin logic.
- `POST .../hold` when another origin already holds the GPU returns **200** and adds the container hold (OR).
- `POST .../release` with no container hold, but other holds left, returns **409** with `{"holds": [...]}`. With no hold at all it returns **404**.

### 10.8 Exclusive claim and backfill (design, v2.2 draft; not in the mock yet)

The owner's latest requirement is "a defined locking scheme". One process at a time keeps a GPU busy, and queue work only backfills it while it's idle. This builds on 10.1–10.7 and adds nothing parallel to them: **a hold *is* the exclusive claim.** Every hold names an owner (`by`), a `reason` and a `since`, and backfill is just the consumer tasks of 10.2.

- **A third origin: `lock`.** The project's job locks can name a GPU. They are kept by the lock script on the hub, one directory per job, and each has an `owner` file with `owner:`, `claimed:` and `what:` lines.
  - **Who reads the locks:** the agent on the host that has the lock directory (the hub). The owning host never reads it, so it needs no share or ssh (server independence). That agent reports every lock as `health.locks: [{"name", "owner", "claimed", "what"}]`, with `claimed` as a float epoch. The lock file stores `claimed:` as ISO 8601 with offset (`date -Is`), and the reporting agent converts it, so the container stores floats only.
  - **Name → GPU:** the owning host's `<HOST>_GPUS` entry gets `"lock": "<lock name>"`, and it reports that in `health.gpus[].lock`. While a reported lock matches, the container puts a hold with `origin: "lock"`, `lock: <name>`, `by` = owner, `since` = claimed and `reason` = what into `settings.gpu_holds` for every host. The owning host then drains and frees as for any hold (10.3).
  - **Release:** only by the lock's own owner, through the lock script. A dashboard release never clears it; the container shows "held by lock `<name>` (owner …)".
  - **With the container down:** a lock hold takes effect only on the lock's own host. That host's consumers of the GPU (the desk GPU's VLM tasks run on the hub) treat a matching lock as a local hold. The owning host doesn't learn of it until the container relays it, the same limit as 10.5 in reverse.
- **Origins combine (OR):** container, local and lock. Any one of them makes the GPU held, and each is cleared only by its own origin.
- **Backfill start gate (consumers).** A task with the GPU in `needs.gpus` starts an instance only if:
  1. no hold of any origin is present, **and**
  2. the GPU is idle of *foreign* work: VRAM used by processes outside this GPU's map (its listed units, its Ollama instances) is under `GPU_IDLE_MAX_MB` (default 2048), and no Ollama instance of this GPU has a model loaded that none of its consumers asked for. Each consumer task lists its models in `needs.models`.
  This gate is checked by the owning host. It is reported per GPU as `idle: true|false` with a `foreign` list (pids/models), and the container passes it to the consumer hosts in `settings.gpu_idle` with each report. A consumer host with no fresh `gpu_idle` (older than 2 report intervals) does not start new instances; already running ones keep going.
  - **Expected, not a bug:** when the owning host misses two reports (a Wi-Fi blip on the desk box), the hub's consumers of its GPU stop *starting* instances until reports resume. No GPU in view means no new work.
  - **While any hold is present,** the owning host reports `idle: false, foreign: []`, so a stale `true` is never relayed. The hold already blocks starts, and `foreign` lists only work outside a hold's own claimant.
- **Yield:** a hold appearing (any origin) puts the consumers in `drain` exactly as in 10.3, within the 90 s budget. Foreign VRAM appearing while consumers run doesn't stop them, because the claim is the contract. A process that needs the GPU takes the claim first.
- **Who takes a claim:** anything outside the queue that wants the GPU exclusively (a manual job, an offline batch run, the dashboard itself when it's the primary user). It claims through the dashboard (container origin), through `taskctl gpu hold` on the owning host (local), or through a job lock named in the map. It should wait for `hold: "held"` with empty `blockers` before loading its own model.
- **Not changed:** Low Power / run mode gates (10.2), the GPU switch (core safety), and `enabled`.
- **Mock/tests (to add with the implementation):**
  - a lock-origin hold relayed from `health.locks`;
  - release refused (409) when only lock or local holds are left;
  - a stale `gpu_idle` blocking starts;
  - a foreign process making `idle` false;
  - hold on top of another origin returns 200.

### 10.9 Dependency (job code, outside this repo)

"Finish or requeue the current item, save progress" is the **job's** responsibility. The runner only sends SIGTERM and waits `stop_secs`. Every GPU job (video stages, the VLM runner) must, on SIGTERM, requeue or finish its current item within `stop_secs` (≤ 60). That's the Home-server single thread's job code.
