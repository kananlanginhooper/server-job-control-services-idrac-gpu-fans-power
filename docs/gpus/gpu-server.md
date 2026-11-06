# GPU server (11 GB): model switch cost

Measured 2026-10-30 from 36 hours of Ollama's own log: each load runs from "loading model via llama-server" to the model answering ("template selection"). The broker keeps learning live numbers from every call that had to load (Ollama's `load_duration`); see [Live numbers](#live-numbers).

**Setup:** 11 GB card, 751 GB RAM. Ollama runs as its own unit on :11434 with `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_NUM_PARALLEL=1` and `OLLAMA_KEEP_ALIVE=30m`. Its model store is copied to RAM (`/dev/shm`) at boot, so a load never waits for a disk.

| Model | Size on GPU | Layers on GPU | Loads | Median | Fastest | Slowest |
|---|---|---|---|---|---|---|
| qwen3:14b (Q4_K_M) | ~10 GB | **34–41 of 41** (depends on `num_ctx`) | 18 | **7.7 s** | 5.7 s | 62.4 s |
| exaone3.5:7.8b | | 33/33 | 1 | 10.1 s | | |
| llama3.1:8b, granite3.3:8b, command-r7b | | all | 4 | 35–44 s | | |
| phi4:14b, falcon3:10b, olmo2:13b, deepseek-v2:16b, gemma3:12b | | all | 6 | 19–112 s | | |

The slow rows are first loads during a model survey: those models weren't in the RAM copy yet.

- **A warm switch costs about 6–8 s here,** slower than on the GPU desktop. A model outside the RAM copy costs up to ~2 minutes.
- **The 27B model doesn't fit** (17 GB). Work for it waits for the GPU desktop. This GPU's own model is qwen3:14b.
- **Context changes caused most reloads:** the same qwen3:14b loaded 5 times in 8 minutes, with 34, 36, 38 and 41 of 41 layers on the GPU. A larger `num_ctx` pushes layers to the CPU, which also makes every token slower.
- The median gap between loads was 4.5 minutes, and 7 of 30 loads came less than 2 minutes after the previous one.

## What it means for scheduling

- The broker keeps model + `num_ctx` loaded while it has work at the same priority level. It switches at that level only after 10× the next load time (about 80 s here). A higher priority level switches at once.
- **Use one `num_ctx` per model where you can.** On this card it also decides how many layers fit on the GPU. The latest load (`num_ctx` 12288) had all 41 layers on the GPU; the loads with fewer were at other contexts.
- Keep the models this GPU serves in the RAM copy, so a switch stays at seconds rather than a minute.

## Live numbers

```sql
select gpu, model, n, round(avg_s::numeric, 1) avg_s, round(max_s::numeric, 1) max_s, last_at from work.model_loads order by gpu, model;
```

`avg_s` is a running average of roughly the last 20 loads, and it's what the broker uses as this GPU's switch cost. A model never loaded on a GPU counts as 30 s.
