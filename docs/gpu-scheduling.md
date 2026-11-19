# GPU scheduling: priority work first, everything else fills the gaps

Owner's design (2026-11-18). GlyphLang runs more or less continuously and holds the priority slot; every other GPU workload
waits in the same queues at a lower level and runs whenever a GPU has nothing better to do. No Claude session has to pause
or resume a worker by hand: each GPU's worker decides locally, from the queue, the claim files and the GPU itself.

## The pieces

| Piece | Where | What it does |
|---|---|---|
| Work queues | `work.priority_items`, `work.standard_items`, `work.backup_items` on the hub ([work-queues.md](work-queues.md)) | The funnel. Every unit of GPU work is a row with its model (or job name), context, level and payload. |
| GPU worker | one per GPU ([services/gpu-worker](../services/gpu-worker/README.md)) | Claims the best work for its GPU, keeps the loaded model busy, runs Ollama calls and jobs, gives way when it should. |
| Job definitions | `GPU_WORKER_JOBS_DIR` on each server (`<name>.json`) | The non-Ollama workloads this server may run. Queue rows name a job; only defined jobs run. |
| Claim files | `GPU_WORKER_CLAIMS_DIR/<gpu>/` on each server | Direct GPU use outside the queue (training, a llama-server). The worker steps aside while one is live. |
| `gpuq` | [scripts/gpuq.py](../scripts/gpuq.py) | Writes and removes claim files: `gpuq hold ... -- <command>`. |

## Order

1. **A live claim file**: the GPU is the holder's. The worker finishes or hands back what it holds, unloads Ollama and claims nothing.
2. **Priority queue**: the reservation holder's work (GlyphLang). Always first.
3. **Standard, then backup**: everyone's other work, including the holder's own lower-priority work. Within a queue: dashboard
   tab order, then the item's level, then the loaded model (see [work-queues.md](work-queues.md)).

## A reservation

`RUN_BROKER_RESERVE_UNTIL` / `RUN_BROKER_RESERVE_FOR` give one owner the priority slot until a time (owner, 2026-11-18: a
reservation is for that work to take the priority slot, not for all work to be priority). With
`RUN_BROKER_RESERVE_MODE=fill` (the default) during a reservation:

- standard and backup work starts on a GPU only after that GPU has gone `RUN_BROKER_FILL_IDLE_S` (30 s) with no priority work,
  no live claim and no GPU use the worker didn't start (the worker measures this and sends it with each claim);
- standard work gives way to priority work the same way backup work always does (below).

`RUN_BROKER_RESERVE_MODE=exclusive` is the old behavior: priority work only, the GPUs idle otherwise.

## Giving way without thrashing

A model or job switch costs minutes, so changeover is allowed to take minutes (owner, 2026-11-18):

- **Lower-priority work with under `RUN_BROKER_YIELD_FINISH_S` (300 s) of estimated run time left finishes first.** An Ollama
  call always counts as under that (they are seconds to a few minutes). A job's time left comes from its own status file, or
  else its `est_s` minus the time it has run.
- Longer work gives way at the worker's next sync: a job gets SIGTERM, saves its progress and exits; the item goes back to
  `ready` without using up an attempt and resumes later.
- The broker keeps a GPU on its loaded model while that model has work at the top level (`SWITCH_AMORTIZE`), so fill work
  runs on whatever is already loaded before anything else is loaded.

## Jobs: non-Ollama GPU work

A **job definition** lives on the server that may run it, in `GPU_WORKER_JOBS_DIR/<name>.json`. The worker offers every
defined job to the broker as the model `job:<name>`, after its Ollama models.

```json
{
  "cmd": ["/opt/image-signals/run_batch.sh"],
  "args": true,
  "workdir": "~/image-signals",
  "env": {"BATCH_SIZE": "64"},
  "vram_gb": 6,
  "grace_s": 120
}
```

- `cmd`: the fixed command (argv, no shell). `args: true` lets each queue row append its own `payload.args` (strings).
- `grace_s`: seconds between SIGTERM and SIGKILL when the job has to give way or is cancelled.
- The worker unloads Ollama before a job starts and sets `CUDA_VISIBLE_DEVICES`, `GPU_JOB_ID`, `GPU_JOB_GPU` and
  `GPU_JOB_STATUS` (a file path) in its environment. Output goes to `GPU_WORKER_JOBS_DIR/logs/<queue>-<id>.log`.
- **Exit codes**: 0 = done (the last 40 log lines are the result); **75 = stopped at a checkpoint, run me again** (back to
  `ready`, attempt not counted); anything else = failed, retried up to `max_attempts`.
- **Time left**: a job may write `{"remaining_s": 900, "note": "batch 12 of 40"}` to `$GPU_JOB_STATUS`; the note shows as
  the item's progress. Without it, the row's `payload.est_s` minus elapsed time is used.
- A job must be safe to stop with SIGTERM and start again (keep its own checkpoint). Run long work as chunks that exit 75.

Queue a job like any other item:

```sql
INSERT INTO work.backup_items (study, dashboard, model, priority, payload)
VALUES ('image-signals-2026-11', 'media', 'job:image-signals', 5,
        '{"args": ["--batch", "/data/photos/2019"], "est_s": 3600}');
```

## Claim files: direct GPU use

For work that has to own a GPU outside the queue (LoRA training in a container, a llama-server):

```bash
gpuq hold --for "glyph-lora run 5" -- sudo -n docker run --rm --gpus all ...   # claim, wait for the GPU, run, release
gpuq hold --for "235B blind decode" --pid 2434159                              # until that process exits
gpuq hold --for "maintenance" --until 2026-11-20T18:00-05:00                   # until a time
gpuq release --id <claim id>          gpuq status
```

A claim is a JSON file in `GPU_WORKER_CLAIMS_DIR/<gpu>/`: `{"id", "for", "pid", "until", "at"}`. It is live while its `pid`
(if any) is alive and its `until` (if any) is in the future. The worker checks every 2 s; within a few seconds of a live
claim it hands back its queue, stops or finishes its running work (the same 300-s rule), unloads Ollama and writes
`<gpu>/.state` = `{"held_by": <claim id>, "free": true}`. `gpuq hold` waits for that before running the command.

## What the worker counts as busy

The fill clock (`idle_s` in each claim) restarts whenever the GPU has priority work held or running, a live claim, or — with
nothing of the worker's own running — GPU utilization at or above `GPU_WORKER_FOREIGN_UTIL` (10 %) from `nvidia-smi`
(someone else is using it: a llama-server, a training run without a claim file).
