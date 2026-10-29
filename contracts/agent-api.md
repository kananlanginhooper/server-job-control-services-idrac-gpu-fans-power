# Agent API contract: control container ⇄ per-server agent (DRAFT v0, for agreement)

**Status: proposal.** Server Services wrote it on 2026-10-29 for the Recovery page thread, which owns the control container (`hub/`, FastAPI, taking over :8090). Nothing implements it yet. When both sides agree, it becomes v1 and **this file is the one source of truth**, like [`mqtt-topics.md`](mqtt-topics.md). The container repo pins it by commit.

**Direction (owner, 2026-10-29):**
- ONE control container holds the REST API, the database access and the queue for every server.
- Each server runs as few system services as possible, ideally ONE queue-driven agent. The agent asks the container what should run, applies it on the host, and reports back.
- A server keeps running its **last known orders** when the container is unreachable: depend, never block.

## 1. Who does what

| | Control container (Recovery) | Per-server agent (Server Services) |
|---|---|---|
| Runs as | Docker, one instance | bare OS (systemd), one per server: the **task runner grows into the agent** |
| Holds | desired state for every server, the command queue, the work queue, history | a local copy of its own orders (local Postgres `taskrunner`), so it boots and runs without the container |
| Talks | answers HTTP | **pulls** (the container never connects to a server); long-poll keeps commands fast |
| Hardware | none | everything host-level: systemd, ipmitool/DCMI, RAPL, `nvidia-smi`, jobs |

Why pull and not push:
- Servers behind NAT (Tug) work unchanged.
- No inbound port is needed on any server.
- The container can restart freely: agents just retry.

## 2. Endpoints (base: `HUB_API_URL` + `/api/v1/agent`; host = the agent's `HOST_KEY`)

Every request carries `Authorization: Bearer <token>`. Every server has its own token: `<HOST>_AGENT_TOKEN` in the shared `.env`, kept as a hash in the container. A token can only read and write its own `{host}`.

| Method + path | Purpose | Body → reply |
|---|---|---|
| `GET /servers/{host}/orders?version=N&wait=25` | Desired state. Long-poll: answers at once if the container's version > N, else after `wait` s with `304` | → `{version, at, tasks: [...], settings: {...}}` (section 3) |
| `POST /servers/{host}/report` | State, every 10 s, plus at once after a change | `{at, orders_version, runner: <the :8098 status JSON>, runs: [new task_runs rows], health: {...}}` → `{ok}` |
| `GET /servers/{host}/commands?wait=25` | Claim one-off commands (oldest first), long-poll | → `[{id, cmd, args, by, at, expires_at}]` (claimed = leased to this agent) |
| `POST /servers/{host}/commands/{id}/result` | Finish a command | `{ok, error?, at, ...detail}` → `{ok}` |
| `POST /queues/{queue}/claim` | Work items (section 5) | `{worker, n, lease_s}` → `[{id, payload, attempt}]` |
| `POST /queues/items/{id}/done` / `.../failed` | Finish a work item | `{result}` / `{error, retry: bool}` |

Rules:
- Times are Unix seconds (float). Unknown fields are ignored on both sides; fields are added freely, never renamed within a version.
- `orders.version` only goes up. The agent stores the last applied version with the orders (local Postgres), so after a reboot it can ask `version=N` and get `304` if nothing changed.
- Commands are **at-most-once**: a claimed command that isn't finished before `expires_at` is marked `expired`, never handed out again. Hardware commands must be safe to drop, never to repeat.

## 3. Orders (desired state)

```json
{
  "version": 412,
  "at": 1793300000.0,
  "tasks": [
    {"name": "video-vlm", "command": "exec ionice -c3 ...", "workdir": "...", "env": {}, "kind": "continuous",
     "instances": 3, "instances_by_power": {"1000": 1, "1500": 3}, "every_secs": null, "at_times": null,
     "needs": {"paths": ["/b"], "gpu": true}, "nice": 10, "stop_secs": 30, "enabled": true}
  ],
  "settings": {"power_url": "...", "keep_days": 30}
}
```

- `tasks` uses the same fields as the runner's `tasks` table (see [`services/taskrunner/README.md`](../services/taskrunner/README.md)). The agent writes them into its local table **in one transaction**; the runner loop then applies them exactly as it does today.
- **Only tasks named in the orders are managed**, and a server-local row is never deleted by a sync: a row created with `taskctl` on the server stays local (it's marked `origin='local'`), so hand-made emergency changes still work while the container is down.
- **Run mode, power target and GPU switch are NOT in the orders.** They stay where they are today (`/api/powertarget`, `/api/runprofile`, `/api/gpu` on :8090, read by fan-control, power-guard, gpu-switch and dash-agent). Moving them is a later, separate step, with the single thread. Orders never change them as a side effect.

## 4. Commands (one-off)

| `cmd` | Args | What the agent does | Refused when |
|---|---|---|---|
| `task.restart` | `{name, instance?}` | stops the copies; the loop restarts them | unknown task |
| `task.run_now` | `{name}` | runs a `schedule` task once now | not a schedule task |
| `unit.restart` | `{unit}` | `systemctl restart <unit>` | unit not in this server's allow-list (`<HOST>_AGENT_UNITS`) |
| `fan.override` | `{pct, minutes, by}` | same as sjc-agent's `fan_override` today | outside fan-control's limits, or an Emergency is active |
| `gpu.limit` | `{watts, minutes, by}` | same as sjc-agent's `gpu_limit` today | outside the card's min/max |
| `agent.resync` | `{}` | re-pulls orders with `version=0` | never |

`fan.override` and `gpu.limit` behave exactly like the MQTT commands of [`mqtt-topics.md`](mqtt-topics.md), same limits, same refusals. See section 7 for how the two paths relate.

## 5. Work queue (lives in the container's database)

- `POST /queues/{queue}/claim` leases up to `n` items for `lease_s` seconds. An item whose lease runs out goes back to `ready`.
- `done` stores the result. `failed` retries up to the queue's `max_attempts`, then stays `failed` for a person.
- **Independence:** a worker claims a batch (e.g. 10 minutes of work). If the container goes away, it finishes the batch and waits, while continuous tasks and anything not queue-fed keep running on local orders.
- Queues are filled by the container (from the catalogue in MySQL), not by agents.

## 6. What the agent does when the container is unreachable

- Keeps running the last applied orders (local Postgres) and keeps the runner loop and the :8098 status page working.
- Retries orders and commands every 10 s, then backs off to 60 s. Nothing blocks: no boot step waits for the container.
- Queues new reports in local Postgres, capped by count and age, and sends them in order when the container is back.
- `taskctl` keeps working on the server, as the local override.

## 7. Open questions for Recovery

1. **Commands over REST or MQTT?**
   - Today the dashboard publishes `fan_override` / `gpu_limit` to the sjc-agent over MQTT (contract v1).
   - Proposal: REST commands (section 4) become the one durable path: queued, auditable, and they work through NAT.
   - MQTT stays for **live state** (fast, retained), and the sjc-agent becomes a publisher only. The MQTT commands are retired once the container sends REST commands. A v2 of the topic contract, announced first.
2. **Long-poll vs. a shorter poll:** long-poll (25 s) keeps a command's latency to about a second without websockets. Fine with uvicorn's async workers?
3. **Tokens:** a per-server bearer token in `.env` now, OIDC client credentials from the wrapper's IdP later?
4. **Orders storage in the container:** your schema. The agent only needs `version` to only go up and per-task rows with the fields above.

## 8. How the transition goes (Server Services side)

Each step is its own branch, with tests against a mock of this API (as `mocks/dashboard_api.py` does for :8090). Each host step goes through the single thread.

1. The runner gets an orders-sync thread plus `origin` on task rows. Without `HUB_AGENT` set, it behaves exactly as today.
2. Reports, then commands.
3. Fewer units: helper loops that need no early boot (peak-power, imc-bw, nvme-watch, gpu-perf, gpu-switch) become runner tasks, one at a time, each with its own announced window.
   **fan-control stays its own unit on every server**: it must start at `basic.target` with nothing else running, and it's the temperature safety floor. power-guard stays a unit until the container can take its decision logic.
4. dash-agent's state collection moves into the agent's report, and its HTTP :8091 is retired once the container reads reports instead.
