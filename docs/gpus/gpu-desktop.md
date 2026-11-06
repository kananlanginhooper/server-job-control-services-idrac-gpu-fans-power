# GPU desktop (24 GB): model switch cost

Measured 2026-10-30 from three days of Ollama's own log: each load runs from "loading model via llama-server" to the model answering ("template selection"). The broker keeps learning live numbers from every call that had to load (Ollama's `load_duration`); see [Live numbers](#live-numbers).

**Setup:** 24 GB card, 54 GB RAM. Models are on local disk and stay in the page cache once read. Ollama runs one model at a time on :11434; a second instance on :11437 is left from a throughput test. `OLLAMA_KEEP_ALIVE` isn't set, so Ollama drops an idle model after **5 minutes**.

| Model | Size on GPU | Layers on GPU | Loads | Median | Fastest | Slowest |
|---|---|---|---|---|---|---|
| qwen3.6:27b (Q4_K_M) | 17.2 GB | 66/66 | 32 | **4.9 s** | 4.4 s | 11.1 s |
| qwen3:14b | ~10 GB | 41/41 | 8 | **2.5 s** | 2.5 s | 7.3 s |
| mistral-small3.1:24b | ~15 GB | 41/41 | 10 | 3.9 s | 3.7 s | 24.6 s (first read from disk) |

- **A warm switch costs about 3–5 s.** A model not read since the last reboot costs up to ~25 s, because it comes from disk.
- **Loads were frequent:** the median gap between loads was 3.7 minutes, and 17 of 49 loads came less than 2 minutes after the previous one. Each was a model change, a context change, or a reload after the 5-minute idle drop.
- Everything fits in VRAM: no partial offload at the contexts used so far.

## What it means for scheduling

- Items take 10–90 s, so one switch costs 5–50 % of one item. The broker keeps a loaded model busy while it has work at the same priority level. It switches at that level only after the loaded model has run for 10× the next model's load time (about 50 s here), so switching costs ≤ ~10 % of GPU time. A higher priority level switches at once.
- **A different `num_ctx` is a reload too.** Ollama restarts the model's runner when the context changes. The broker treats model + `num_ctx` as one thing to keep loaded, so use one `num_ctx` per model within a study where you can.
- The worker sends `keep_alive: 30m` on each call (`GPU_WORKER_KEEP_ALIVE`), so the 5-minute idle drop no longer forces reloads between bursts.

## Live numbers

```sql
select gpu, model, n, round(avg_s::numeric, 1) avg_s, round(max_s::numeric, 1) max_s, last_at from work.model_loads order by gpu, model;
```

`avg_s` is a running average of roughly the last 20 loads, and it's what the broker uses as this GPU's switch cost. A model never loaded on a GPU counts as 30 s.
