# GPU worker

**Runs on:** each GPU's server, one per GPU, as a continuous task-runner row (not a unit of its own)

Takes single work items from the hub's run broker ([work queues](../../docs/work-queues.md)): the priority queue first, then standard, then backup (a running backup job gives way as soon as better work is waiting). It runs each as one Ollama call on this GPU and hands the result back. It never touches a database; claim, beat, done, failed and release are the broker's HTTP routes.

- **Claims** by long-poll. Each claim sends this GPU's models (own model first, then fallbacks) and what Ollama has loaded now (model + context, from `/api/ps`), so the broker can keep the loaded model busy.
- **Streams the call.** It beats every 10 s with the token count; the broker answers `cancel` for a paused or cancelled item, and the worker drops the call.
- **Reports Ollama's `load_duration`** with each result, so the broker learns this GPU's switch cost.
- **Low Power:** no new claims; a running call finishes. **Ollama unreachable** (GPU switched off): the item is released, not counted as a failure. **Stopped (SIGTERM):** running items are released at once.
- **Never changes** run mode, the power target, the GPU switch or the GPU power limit.

| | |
|---|---|
| Reads | the broker (`GPU_WORKER_BROKER_URL`), Ollama on this host (`GPU_WORKER_OLLAMA`), the power target (Low Power) |
| Writes | nothing locally except its log (the runner's) |
| Settings | `GPU_WORKER_MODELS` (required, own first), `GPU_WORKER_GPU`, `GPU_WORKER_HOST`, `GPU_WORKER_SLOTS`, `GPU_WORKER_KEEP_ALIVE`, `GPU_WORKER_BROKER_URL` (or `TASKRUNNER_BROKER_URL`), `<HOST>_AGENT_TOKEN` |

Run it as a runner row on the GPU's server:

```bash
taskctl add gpu-worker --cmd 'exec python3 -B <clone>/services/gpu-worker/gpu_worker.py' --kind continuous
```

Switch costs per GPU: [GPU desktop](../../docs/gpus/gpu-desktop.md), [GPU server](../../docs/gpus/gpu-server.md).
