# Work queues: how to get single units of work run on the GPUs

Owner's design (2026-10-30): the same broker and runner pattern for all work, pulled from three tables of the same shape.

| Table | For | Order |
|---|---|---|
| `work.priority_items` | immediate, human-in-the-loop work (research studies) | always first |
| `work.standard_items` | normal jobs: what all other work uses | when no priority item fits a GPU |
| `work.backup_items` | an endless list of low-return work: double-checking, photo fragments | only when nothing else fits; a running backup job gives way as soon as priority or standard work for that GPU is waiting (handed back, not counted as a failed attempt) |

Both live in the `taskrunner` database on the hub. An item is **one unit of work for one model**. Today that's one Ollama call, `kind = 'ollama'`. One GPU worker per GPU ([`services/gpu-worker`](../services/gpu-worker/README.md)) claims items through the run broker ([`services/run-broker`](../services/run-broker/README.md)), runs them, and hands back the result. Submitters only write rows; they never talk to a GPU.

## Add work

```sql
INSERT INTO work.priority_items (study, step, model, priority, payload, parent_id)
VALUES ('rt2', 'encode', 'qwen3.6:27b', 10,
        '{"api": "chat", "body": {"messages": [{"role": "user", "content": "..."}],
                                   "think": true, "format": "json",
                                   "options": {"num_ctx": 16384, "temperature": 0, "seed": 0}}}',
        NULL)
RETURNING id;
```

- **`dashboard`:** your dashboard's tab id in the wrapper (`copilot`, `media`, `sensitivity`, ...). **In the standard and backup queues, the dashboards' tab order ranks the work** (owner, 2026-10-30): an earlier tab's items always go first, then each item's own `priority`. Reordering the tabs reorders the GPU work. A missing or unknown dashboard goes last. The priority queue is GlyphLang's only.
- **`study`:** your grouping. Studies at the same priority level take turns, so a big one can't starve a small one. You pause and cancel by study too.
- **`model`:** the exact Ollama tag, which decides which GPUs can take the item. A GPU takes its own models first, then the fallbacks it can also load.
- **`priority`:** the level, highest first, and strict. Change it any time: the next free GPU reads the current value.
- **`payload.body`:** an Ollama `/api/chat` (or `"api": "generate"`) body without `model` and `stream`, which the worker sets. `keep_alive` defaults to 30 minutes. A model that rejects `think` is retried without it.
- **`num_ctx`:** use one per model in a study where you can. Ollama reloads the model when the context changes, and the broker counts model + context as one thing to keep loaded.
- **`parent_id`:** the item that led to this one, so you can pause or cancel a whole tree. `step` is a free label.
- **`max_attempts`** defaults to 3. A failed or abandoned call goes back to `ready` until then.

## Chain the next step

When an item is `done`, read its result and insert the follow-up with `parent_id` set, in whichever table and at whatever priority it belongs. Whichever GPU serves the next model takes it as soon as it's free. There's no direct hand-off between hosts.

```sql
SELECT id, step, parent_id, result, stats FROM work.done
WHERE queue = 'priority' AND study = 'rt2' AND finished_at > $last_seen ORDER BY finished_at;
```

- **`result`** is Ollama's answer in its non-streamed shape: `message.content`, `message.thinking`, `eval_count`, ... (for generate: `response`, `thinking`). It's kept in `work.results`, out of the hot table.
- **`stats`** (small) holds `load_s` (how long the call waited for a model load), `total_s`, `prompt_tokens`, `output_tokens`, `ctx` and `gpu`.

## Change your mind

```sql
SELECT work.pause('priority', study => 'rt2');     -- ready and running items -> paused (a running call is aborted)
SELECT work.resume('priority', study => 'rt2');    -- paused -> ready
SELECT work.cancel('priority', study => 'rt2');    -- ready, running and paused -> cancelled
SELECT work.cancel('priority', root => 1234);      -- an item and everything queued from it (parent_id tree)
UPDATE work.priority_items SET priority = 20 WHERE study = 'rt2' AND state = 'ready';   -- reprioritize
```

Each returns how many items it changed. A running call that gets paused or cancelled stops within about 10 s. Ollama stops generating when the worker drops the connection.

## Watch it

- **`work.studies`:** one row per queue and study, with ready, running, paused, done, failed and cancelled counts, the oldest ready item, the last finish, the average call time, output tokens, models and top priority. This is the dashboard's view.
- **`work.items`:** both tables as one, with a `queue` column (no results).
- **`work.done`:** finished items with their `result`. Read answers back from here instead of joining yourself.
- **`work.depth`:** per queue, model, priority level and state, how many items there are and the oldest one's age. This is the per-GPU depth.
- **`GET http://<hub>:8099/status`:** the same, plus what each GPU has loaded since when, and its measured load times.
- **`progress`** on a running item: tokens so far, updated every 10 s.

## How the broker picks the next item for a GPU

1. The tables in order (priority, standard, backup); a later table only when nothing in an earlier one fits this GPU.
2. In the standard and backup queues, the owning dashboard's tab order first (read live from the wrapper), then the highest priority level, even if that means loading another model.
3. At the same level, the loaded model + context keeps going while it has work. Another model at that level takes over once the loaded one has run for 10× the new model's load time and the other's oldest item has waited as long, so switching costs at most ~10 % of the GPU's time. Load times per GPU: [gpu-desktop.md](gpus/gpu-desktop.md), [gpu-server.md](gpus/gpu-server.md), and live in `work.model_loads`.
4. The GPU's own models before its fallbacks.
5. The study served least recently first, then the oldest item.
6. With a call already running on a GPU (more than one slot), only the same model is handed out until the GPU drains.

Low Power (the power target says `scale=down`) stops new claims; a running call finishes. The workers never change run mode, the power target or the GPU switch.
