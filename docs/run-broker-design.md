# Run broker: one decider on the hub, one runner per server

Owner's design (2026-10-30). Built (`services/run-broker`, plus the broker link in `services/taskrunner`) and tested. **Not switched on:** the owner says when; until then no runner has `TASKRUNNER_BROKER_URL` set and the broker unit isn't started.

## The shape

```
 requests ──► task_requests ─┐
 priorities ─────────────────┤
                             ▼
                      ┌─────────────┐   orders (pull)    ┌──────────────┐
                      │  run broker │ ◄────────────────► │ runner: hub   │
                      │  (hub)      │                    └──────────────┘
                      │             │ ◄────────────────► ┌──────────────┐
                      │ only writer │                    │ runner: GPU   │
                      │ of the DB   │                    │ server        │
                      └─────┬───────┘ ◄────────────────► ┌──────────────┐
                            │                            │ runner: GPU   │
                            ▼                            │ desktop       │
                   taskrunner Postgres (hub)             └──────────────┘
          tasks · task_runs · task_requests · events
```

- **Broker (hub only).** Reads the queue: `task_requests`, task priorities, each server's last report (power target allowance, Low Power, GPU holds, what's already running). Decides what runs where and how many copies, and issues it as that server's orders. It is the **only writer** of the central database: task rows, run history, request status.
- **Runners (every server, the hub included).** Start, stop and size what their orders say, inside the local gates (power target, Low Power, holds, needs). Report back: what's running, every start and exit with its exit code, health. A runner never writes the central database.
- **Request status comes free.** The broker turns runner reports into `running` / `done` / `failed` on the `task_requests` row it placed. Nobody marks them by hand.

## What stays as it is

- **Core safety layer:** fan-control, power-guard and gpu-switch, plus run mode and power target, stay separate system services. The broker never changes them; runners only read them as gates.
- **Servers stay independent.** Each runner keeps its last orders in its local database. With the broker or the hub down, every server keeps running what it last had, and the GPU server and GPU desktop keep working. Reports wait locally (capped) and go when the broker is back. Only new placements wait.
- **Local overrides:** `taskctl` on a server still works there (local holds, a local task) and is reported with `origin: local`. The broker never fights a local row.

## How they talk

The agent API contract (`contracts/agent-api.md`, v2.1) already has the two calls this needs:

- `GET /servers/{host}/orders?version=N&wait=25`: the runner long-polls for its orders.
- `POST /servers/{host}/report`: state every 10 s, plus right after any start or exit.

The broker is what produces the orders and consumes the reports. Runners only make outbound calls, so the GPU desktop behind NAT needs no open port. The MQTT broker already on the hub could carry the same messages later. It isn't needed to start.

## What the broker places

- **Accepted requests only:** `status = 'queued'` with no `placed_task` yet. Setting `queued` is the acceptance; the broker then picks the host, writes `placed_host`, `placed_task` (`req-<id>-<short>`) and `placed_at`, and notes `placed by the run broker (<lane>)`. With `RUN_BROKER_AUTO_ACCEPT=1` it takes `requested` rows too.
- A `placed_host` set by hand is kept. Rows placed by hand with `taskctl` (a `placed_task` already set) are left alone.
- **Line order** is the queue view's: priority, then `wanted_by`, then first come.
- **Host choice:** GPU work (`needs.gpu` required or preferred) takes the smallest free GPU with enough VRAM, never a host in `RUN_BROKER_GPU_HELD`; preferred work falls back to CPU. CPU work goes to the host with the most free CPU slots. Slots per host and lane come from `RUN_BROKER_HOSTS`. Requests that can't be placed stay `queued` with the reason in `status_note`.

## Request status, from the runs

| Runner report | Request |
|---|---|
| first run starts | `running`, `started_at` |
| oneshot exits 0 | `done`, `finished_at`, `exit_code`; order withdrawn |
| a run fails | stays `running`, note `exit N; the runner retries (k of 3)` |
| `RUN_BROKER_MAX_FAILS` failed runs in a row | `failed`; order withdrawn |
| a person sets `paused` | the row is disabled (copies stop); back to `queued`/`running` enables it |
| `cancelled` or `declined` | order withdrawn; the runner stops and removes the row |

## Build state

1. **Broker v0 (built):** placement, orders, reports, request status, `/status` page on :8099.
2. **Runner link (built):** with `TASKRUNNER_BROKER_URL` set, the runner long-polls orders into `origin='broker'` rows (rewritten only when the order changes) and reports starts and exits from `task_runs` by a change counter, so nothing is lost while the broker is down. Without it the runner behaves exactly as before.
3. **Next:** count hand-placed work toward a host's slots, size slots from the live power target and each server's report, backfill idle servers.

Tests: `tests/test_run_broker.py` (placement, rows, status) and `tests/test_run_broker_e2e.py` (broker plus a real runner on a throwaway Postgres; `scripts/taskrunner-e2e.sh`).

## Switching it on (when the owner says)

1. Hub: `bash services/run-broker/install.sh` (installs and enables, doesn't start), set `RUN_BROKER_HOSTS` and `RUN_BROKER_GPU_HELD` in `.env`, then `sudo systemctl start run-broker`.
2. Each server, the GPU desktop first: set `<HOST>_TASKRUNNER_BROKER_URL` and `<HOST>_AGENT_TOKEN`, then `bash services/taskrunner/install.sh` (restarts the runner).
3. The requests standard then says the single thread accepts (`queued`) and the broker places and tracks.
