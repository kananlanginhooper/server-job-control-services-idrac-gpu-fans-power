# Run broker

**Runs on:** hub only

One decider for every server's task runner. It reads the accepted task requests, picks a server and a lane (CPU or GPU) for each, and serves each server its orders. The runners report every start and exit back, and the broker turns those into the request's status. It is the only writer of its tables and of the requests' status and placement fields; runners never write the hub's database.

- **Talks:** the agent API ([`contracts/agent-api.md`](../../contracts/agent-api.md)) sections 4 and 5, under `/api/v1/agent`: `GET /servers/{host}/orders?version=N&wait=25` (long-poll) and `POST /servers/{host}/report`. Runners only make outbound calls. One bearer token per server (`<HOST>_AGENT_TOKEN`); a token for another server gets `403`.
- **Independence:** runners keep their last orders in their local table, so with the broker or the hub down every server keeps running what it has. Only new placements wait.
- **Never touches** run mode, the power target, the GPU switch or any core-safety service. Runners keep their own Low Power gate.
- **Work queues** ([`docs/work-queues.md`](../../docs/work-queues.md), `workq.py`): the same pattern for single units of work. Tables, in claim order: `work.priority_items` (immediate, human in the loop), `work.standard_items` (normal jobs) and `work.backup_items` (an endless list of low-return work; a running backup job gives way when better work is waiting). GPU workers ([`services/gpu-worker`](../gpu-worker/README.md)) claim, beat, finish and release through `POST /api/v1/agent/servers/{host}/work/...`. The broker picks each item by queue, level, loaded model + context (weighing each GPU's measured load time, `work.model_loads`), fallbacks and study turns.
- **Request placement** (task_requests onto runners) only runs with `RUN_BROKER_PLACE_REQUESTS=1`.
- Design and status table: [`docs/run-broker-design.md`](../../docs/run-broker-design.md).

| | |
|---|---|
| Reads | `task_requests` (accepted rows), runner reports |
| Writes | schema `work` (items, results, study turns, GPUs, model loads); schema `broker` (`hosts`, `orders`, `runs`) in the hub's `taskrunner` database; `task_requests` status, placement and run fields |
| Serves | `:8099` orders/report for runners; `GET /status` (read-only JSON, open on the LAN) |
| Settings | `RUN_BROKER_DSN`, `RUN_BROKER_PORT`, `RUN_BROKER_HOSTS`, `RUN_BROKER_GPU_HELD`, `RUN_BROKER_AUTO_ACCEPT`, `RUN_BROKER_MAX_FAILS`, `RUN_BROKER_CODE_ROOT`, `RUN_BROKER_PLACE_REQUESTS`, `RUN_BROKER_LEASE_S`, `<HOST>_AGENT_TOKEN` per server |

Settings come from the repo's one `.env` (see [`.env.example`](../../.env.example)). `install.sh` installs and enables the unit but doesn't start it.
