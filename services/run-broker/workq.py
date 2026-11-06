# Work queues for the run broker (owner, 2026-10-30): the same broker/runner pattern for every kind of work, pulled from three
# tables of the same shape, always in this order:
#   work.priority_items  immediate, human-in-the-loop work (the GLIRS research). Always drained first.
#   work.standard_items  normal jobs: what everything else uses. Taken when no priority item fits the GPU.
#   work.backup_items    an unending list of low-return work (double-checking, photo fragments). Only when the GPU has nothing
#                        else, and a running backup item gives way: once priority or standard work for that GPU is ready, its
#                        next beat hands it back (not counted as an attempt) and the GPU takes the better work.
# An item is one unit of work for one model (kind 'ollama' = one Ollama call). Submitters INSERT rows and may change
# `priority` (a level: higher first), pause / resume / cancel rows, a study or a parent tree at any time; claims always read the
# current rows, so a reprioritization takes effect at the next free slot. GPU workers never touch the database: they claim,
# beat, finish and release through the broker's HTTP routes, and the broker writes everything.
#
# Choosing the next item for a GPU (claim):
#   1. the tables in order: priority, standard, backup; a later table only when nothing in the earlier ones fits
#   2. within a table, the owning dashboard's tab order (owner 2026-10-30: "build your priority based on the dashboard
#      columns"): items carry `dashboard` = the wrapper tab id, and the broker reads the live tab order, so reordering the
#      tabs reorders GPU work. Then the item's own priority level (strict). An unknown or missing dashboard goes last.
#   3. within a level: the model already loaded (same model AND same num_ctx: Ollama reloads on a context change) stays loaded
#      while it has work, because loading a model takes time (the measured load cost per GPU and model is kept in
#      work.model_loads). Another model at the SAME level takes over when the loaded one has nothing left at that level, or when
#      the loaded model has run for SWITCH_AMORTIZE x the new model's load time and the other model's oldest item has waited
#      longer than that too (so a switch costs at most ~1/SWITCH_AMORTIZE of the time). A higher level always wins.
#   4. the GPU's own models first, then its fallbacks (the worker's `models` list is in preference order)
#   5. round robin between studies (the study served least recently first), then oldest first
#   With work in flight on a GPU (slots > 1), only that model is handed out until the GPU drains.
from __future__ import annotations

import time

QUEUES = ("priority", "standard", "backup")  # claim order
YIELDING = ("backup",)  # a running item here gives way to ready work in an earlier table
STATES = ("ready", "claimed", "paused", "done", "failed", "cancelled")
SWITCH_AMORTIZE = 10  # stay on a loaded model for 10x the next model's load time before switching at the same level
DEFAULT_LOAD_S = 30.0  # a model never loaded on this GPU yet


def _items(q: str) -> str:
    return f"""
create table if not exists work.{q}_items(
  id bigserial primary key,
  study text not null,                        -- the submitter's grouping: fairness, pause, cancel
  parent_id bigint,                           -- the item that queued this one (cancel/pause a whole tree)
  step text,                                  -- free label, e.g. 'encode', 'decode', 'judge'
  dashboard text,                             -- the owning dashboard (wrapper tab id): ranks standard/backup work
  kind text not null default 'ollama',
  model text not null,                        -- exact model tag; the affinity
  priority int not null default 0,            -- level: higher first; may change any time
  payload jsonb not null,                     -- ollama: {{"api": "chat"|"generate", "body": {{...without model/stream}}}}
  state text not null default 'ready' check (state in ({", ".join(f"'{s}'" for s in STATES)})),
  attempt int not null default 0,
  max_attempts int not null default 3,
  claimed_by text,                            -- the GPU
  claimed_at timestamptz,
  lease_until timestamptz,
  progress text,                              -- the worker's note on a long item (tokens so far)
  stats jsonb,                                -- small: durations, token counts, load time
  error text,
  created_at timestamptz not null default now(),
  finished_at timestamptz
);
create index if not exists {q}_ready on work.{q}_items(model, priority desc, created_at) where state = 'ready';
create index if not exists {q}_study on work.{q}_items(study, state);
create index if not exists {q}_parent on work.{q}_items(parent_id) where parent_id is not null;
create index if not exists {q}_claimed on work.{q}_items(claimed_by) where state = 'claimed';
"""


SCHEMA = (
    "create schema if not exists work;\n"
    + "".join(_items(q) for q in QUEUES)
    + """
create table if not exists work.results(          -- large outputs (answer + reasoning) kept out of the hot tables
  queue text not null,
  item_id bigint not null,
  result jsonb not null,
  at timestamptz not null default now(),
  primary key (queue, item_id)
);
create table if not exists work.study_turns(      -- round robin between studies
  queue text not null, study text not null, last_at timestamptz not null, primary key (queue, study)
);
create table if not exists work.gpus(             -- what each GPU has loaded and serves, as the broker last heard
  gpu text primary key, host text, model text, ctx int, model_since timestamptz, last_seen timestamptz
);
alter table work.gpus add column if not exists models text[];
alter table work.priority_items add column if not exists dashboard text;
alter table work.standard_items add column if not exists dashboard text;
alter table work.backup_items add column if not exists dashboard text;
alter table work.gpus add column if not exists queues text[];   -- the queues this GPU takes (a worker may serve only some)
alter table work.priority_items add column if not exists pin_gpu text;   -- set: only this GPU may claim it (cross-GPU comparisons)
alter table work.standard_items add column if not exists pin_gpu text;   -- set: only this GPU may claim it (cross-GPU comparisons)
alter table work.backup_items add column if not exists pin_gpu text;   -- set: only this GPU may claim it (cross-GPU comparisons)
create table if not exists work.model_loads(      -- measured load cost: Ollama's load_duration when a call had to load
  gpu text not null, model text not null, n int not null default 0, avg_s float8 not null, max_s float8 not null,
  last_s float8 not null, last_at timestamptz not null default now(), primary key (gpu, model)
);
create or replace view work.items as             -- every table as one, with the queue it's in (no results: see work.done)
  """
    + "\n  union all ".join(f"select '{q}'::text queue, i.* from work.{q}_items i" for q in QUEUES)
    + """;
create or replace view work.done as              -- finished items with their result, for reading answers back
  select i.queue, i.id, i.study, i.parent_id, i.step, i.model, i.priority, i.stats, i.finished_at, r.result
  from work.items i join work.results r on r.queue = i.queue and r.item_id = i.id where i.state = 'done';
create or replace view work.studies as           -- one row per queue and study: progress at a glance
  select queue, study,
         count(*) filter (where state = 'ready') ready, count(*) filter (where state = 'claimed') running,
         count(*) filter (where state = 'paused') paused, count(*) filter (where state = 'done') done,
         count(*) filter (where state = 'failed') failed, count(*) filter (where state = 'cancelled') cancelled,
         min(created_at) filter (where state = 'ready') oldest_ready, max(finished_at) filter (where state = 'done') last_done,
         round(avg((stats->>'total_s')::float8) filter (where state = 'done')::numeric, 1) avg_call_s,
         sum((stats->>'output_tokens')::bigint) filter (where state = 'done') output_tokens,
         array_agg(distinct model) models, max(priority) top_priority
  from work.items group by queue, study;
create or replace view work.depth as             -- per queue, model and level: what is waiting, running and paused
  select queue, model, priority, state, count(*) n, min(created_at) oldest from work.items
  where state in ('ready', 'claimed', 'paused') group by 1, 2, 3, 4;
"""
    + "".join(
        f"""
create or replace function work.set_state_{q}(p_study text, p_root bigint, p_from text[], p_to text) returns int language sql as $$
  with recursive tree(id) as (
    select id from work.{q}_items where (p_study is not null and study = p_study) or id = p_root
    union select c.id from work.{q}_items c join tree t on c.parent_id = t.id where p_root is not null
  ), upd as (
    update work.{q}_items i set state = p_to, finished_at = case when p_to = 'cancelled' then now() else i.finished_at end
    from tree t where i.id = t.id and i.state = any(p_from) returning 1
  ) select count(*)::int from upd $$;
"""
        for q in QUEUES
    )
    + """
-- for submitters: work.cancel('standard', study => 'x')  /  work.pause('priority', root => 1234)  /  work.resume(...)
create or replace function work.set_state(q text, study text, root bigint, p_from text[], p_to text) returns int
  language plpgsql as $$
declare n int;
begin
  if q not in ('priority', 'standard', 'backup') then raise exception 'no queue %', q; end if;
  execute format('select work.set_state_%s($1, $2, $3, $4)', q) into n using study, root, p_from, p_to;
  return n;
end $$;
drop function if exists work.cancel(text, text, bigint);
drop function if exists work.pause(text, text, bigint);
drop function if exists work.resume(text, text, bigint);
create function work.cancel(q text, study text default null, root bigint default null) returns int language sql as
  $$ select work.set_state(q, study, root, array['ready','claimed','paused'], 'cancelled') $$;
create function work.pause(q text, study text default null, root bigint default null) returns int language sql as
  $$ select work.set_state(q, study, root, array['ready','claimed'], 'paused') $$;
create function work.resume(q text, study text default null, root bigint default null) returns int language sql as
  $$ select work.set_state(q, study, root, array['paused'], 'ready') $$;
"""
)


def key(c: dict) -> tuple:
    """What has to be loaded for an item: Ollama reloads the model when num_ctx changes, so the context is part of it."""
    return (c["model"], c.get("ctx") or 0)


def pick(
    cands: list[dict], loaded: tuple | None, loaded_since: float | None, load_s: dict, now: float, models: list[str]
) -> dict | None:
    """Choose one item from ready candidates of ONE table (pure; the SQL side only collects them).
    cands: [{id, model, ctx, priority, study_last (epoch, 0 = never), created (epoch)}]; loaded: (model, ctx) or None;
    load_s: {model: seconds to load on this GPU}."""
    if not cands:
        return None
    top = max(c["priority"] for c in cands)
    level = [c for c in cands if c["priority"] == top]
    pref = {m: i for i, m in enumerate(models)}

    def order(c: dict) -> tuple:
        return (pref.get(c["model"], len(pref)), c.get("study_last") or 0, c["created"], c["id"])

    mine = [c for c in level if key(c) == loaded]
    rank = pref.get(loaded[0], len(pref)) if loaded else len(pref)
    # a fallback (a model later in this GPU's list) never takes the GPU from loaded work; only an equal or better model may
    others = [c for c in level if key(c) != loaded and pref.get(c["model"], len(pref)) <= rank]
    if mine and others:
        # switch at the same level only once the loaded model has had its share and the others have waited as long
        best = min(others, key=order)
        dwell = SWITCH_AMORTIZE * load_s.get(best["model"], DEFAULT_LOAD_S)
        waited = now - min(c["created"] for c in others if key(c) == key(best))
        if loaded_since is not None and now - loaded_since >= dwell and waited >= dwell:
            return best
        return min(mine, key=order)
    return min(mine or level, key=order)  # nothing loaded here worth keeping: the most preferred model first


def rank(cands: list[dict], order: dict) -> list[dict]:
    """Fold the dashboard's tab order into the level: an earlier tab always outranks a later one, then the item's priority.
    order: {dashboard id: tab order (lower = first)}; unknown dashboards go last."""
    last = max(order.values(), default=0) + 1
    for c in cands:
        c["priority"] = (10_000 - order.get(c.get("dashboard") or "", last)) * 100_000 + int(c["priority"])
    return cands


def busy_filter(cands: list[dict], q: str, busy: list[dict], order: dict, same_ok: bool = True) -> list[dict]:
    """While a GPU holds claimed work (running, or in its local queue), it keeps to that model and context, except for
    work that outranks everything it holds: a later table never preempts an earlier one; an earlier table always may; in
    the same table a higher level (dashboard order folded in, as in rank()) may. The worker runs its local queue in that
    order, so the switch costs one model load. (2026-11-02: with local queues a GPU is always busy, and the old
    same-model-only rule kept it on a fallback model while higher-level work for its own model waited.)
    same_ok=False: only the outranking work (the GPU already holds its queue's worth of time)."""
    qi = QUEUES.index(q)
    if any(QUEUES.index(b["queue"]) < qi for b in busy):
        held_top = None  # it holds work from an earlier table: nothing here outranks that
    else:
        same = rank([dict(b) for b in busy if b["queue"] == q], order)
        held_top = max((b["priority"] for b in same), default=float("-inf"))
    keys = {(b["model"], b["ctx"] or 0) for b in busy}
    return [c for c in cands if (same_ok and key(c) in keys) or (held_top is not None and c["priority"] > held_top)]


def claim_sql(q: str) -> str:
    """Ready candidates of table q for the given models and this GPU (at most a few per model/level/study: enough to
    choose from). Params: (models, gpu). An item with pin_gpu set goes only to that GPU."""
    return f"""
select i.id, i.model, i.ctx, i.priority, i.dashboard, extract(epoch from coalesce(t.last_at, 'epoch'))::float8 study_last,
       extract(epoch from i.created_at)::float8 created
from (select id, model, priority, dashboard, study, created_at, (payload #>> '{{body,options,num_ctx}}')::int ctx,
             row_number() over (partition by model, priority, study order by created_at, id) rn
      from work.{q}_items where state = 'ready' and model = any(%s) and (attempt < max_attempts)
            and (pin_gpu is null or pin_gpu = %s)) i
left join work.study_turns t on t.queue = '{q}' and t.study = i.study
where i.rn <= 3
"""


def now() -> float:
    return time.time()
