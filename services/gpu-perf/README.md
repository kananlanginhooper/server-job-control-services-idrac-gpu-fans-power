# GPU performance table

**Runs on:** hub (as the service user)

Per-minute GPU table `gpu_perf` (MySQL, created if missing). Polls the GPU server's dash-agent every 5 s. Per minute it records: mode, temperature avg/max, power draw avg/max, power cap, utilization, SM clock, card fan, case fan RPM, Emergency, **VRAM used/total, memory-bandwidth %, PCIe rx/tx, host RAM read/write bandwidth**.

Optionally it adds LLM throughput (calls, prompt/output tokens, tokens/s) from `GPU_PERF_CALLS_TABLE`. That's recomputed for the last 15 minutes each minute, because a call is recorded when it finishes.

| | |
|---|---|
| Reads | `GPU_PERF_AGENT_URL`; `GPU_PERF_CALLS_TABLE` (optional). |
| Writes | Rows in `<RECOVERY_DB_NAME>.gpu_perf`. |
| Settings | `GPU_PERF_AGENT_URL`, `RECOVERY_DB_HOST/PORT/NAME/USER/PASSWORD`, `GPU_PERF_CALLS_TABLE`. `gpu-perf.py --backfill <trace.csv>` loads an old trace. |

Settings come from the repo's one `.env` (see [`.env.example`](../../.env.example)). The unit file(s) here are templates: `scripts/install-units.sh` renders `@REPO@`/`@USER@` and installs them. It doesn't enable or start them.
