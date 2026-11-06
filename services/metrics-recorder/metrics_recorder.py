#!/usr/bin/python3
# Metrics recorder (owner, 2026-11-01): MQTT keeps only the latest value, so this records the history the dashboard graphs
# need. It subscribes to the agents' state topics (sjc/<host>/<part>), turns each payload into named metrics with the rules
# in metrics-rules.json, and stores one point per metric per minute in Postgres. Every minute it rolls the minute points up
# into hourly ones; every 10 minutes it prunes each tier by its keep time. A small read API serves the stored series.
#
#   rules       metrics-rules.json (docs/metrics-history.md): which topic field becomes which metric, how a bucket's samples
#               combine (max / min / avg), the tiers and how long each is kept, and the graph bucket per range.
#   storage     database server_metrics on the hub: metrics(tier, ts, host, metric, value) + metric_defs(metric, agg).
#   read API    :8108  GET /series?host=&metric=&range=<s>  ·  GET /metrics  ·  GET /rules  ·  GET /status   (LAN, read-only)
#
# Anything finer than a minute stays live-only: the page draws it from MQTT and it's gone on refresh.
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    import paho.mqtt.client as mqtt
    import psycopg
except ImportError:  # the pure rule helpers below are testable without them
    mqtt = psycopg = None

_here = os.path.dirname(os.path.realpath(__file__))
sys.path[:0] = [_here, os.path.join(_here, "..", "..", "lib")]
from svcenv import env  # noqa: E402

DSN = env("METRICS_DSN", "dbname=server_metrics")
PORT = int(env("METRICS_PORT", "8108"))
RULES_FILE = env("METRICS_RULES", os.path.join(_here, "metrics-rules.json"))
MQTT_HOST, MQTT_PORT = env("SJC_MQTT_HOST", "127.0.0.1"), int(env("SJC_MQTT_PORT", "1883"))
PREFIX = env("SJC_MQTT_PREFIX", "sjc")

SCHEMA = """
create table if not exists metrics(
  tier text not null,
  ts timestamptz not null,          -- the bucket's start
  host text not null,
  metric text not null,
  value double precision not null,
  primary key (tier, host, metric, ts)
);
create index if not exists metrics_tier_ts on metrics(tier, ts);
create table if not exists metric_defs(metric text primary key, agg text not null check (agg in ('avg', 'max', 'min')));
"""


def log(msg: str) -> None:
    print(time.strftime("%F %T"), msg, flush=True)


# ---------- rules (pure; tests/test_metrics_recorder.py) ----------
def load_rules(path: str = RULES_FILE) -> dict:
    with open(path, encoding="utf-8") as f:
        r = json.load(f)
    for m in r["metrics"]:
        m.setdefault("agg", r.get("aggregate", {}).get("default", "avg"))
    return r


def _num(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _keep(item: object, rule: dict | None) -> bool:
    """A list item passes the rule's filters: every "match" field regex matches, no "exclude" field regex matches."""
    if not rule or not isinstance(item, dict):
        return True
    for f, rx in (rule.get("match") or {}).items():
        if not re.search(rx, str(item.get(f) or "")):
            return False
    return not any(re.search(rx, str(item.get(f) or "")) for f, rx in (rule.get("exclude") or {}).items())


def _walk(obj: object, tokens: list[str], rule: dict | None = None) -> list:
    """Follow a path: 'a' = key, '*' = every dict value, 'a[]' = every item of the list under key a (filtered by the
    rule's match / exclude)."""
    if not tokens:
        return [obj]
    t, rest = tokens[0], tokens[1:]
    if t == "*":
        return [x for v in (obj.values() if isinstance(obj, dict) else []) for x in _walk(v, rest)]
    if t.endswith("[]"):
        items = obj.get(t[:-2]) if isinstance(obj, dict) else None
        return [x for it in (items if isinstance(items, list) else []) if _keep(it, rule) for x in _walk(it, rest)]
    return _walk(obj.get(t), rest, rule) if isinstance(obj, dict) and t in obj else []


def extract(payload: object, rule: dict) -> list[tuple[str, float]]:
    """One rule applied to one payload -> [(metric name, value)]."""
    path = rule["path"]
    if rule.get("per_item") == "@key":  # one metric per dict key: "psu.*.v" -> psu_volt:ps1, psu_volt:ps2
        head, _, tail = path.partition(".*.")
        d = _walk(payload, head.split("."))
        out = []
        for k, sub in d[0].items() if d and isinstance(d[0], dict) else []:
            vals = [v for v in map(_num, _walk(sub, tail.split("."))) if v is not None]
            if vals:
                out.append((rule["metric"].replace("{key}", str(k)), vals[0]))
        return out
    if rule.get("per_item"):  # one metric per list item, named from an item field ("gpu{index}_temp_c", "disk_pct:{mount}")
        head, _, tail = path.partition("[].")
        items = _walk(payload, head.split(".")[:-1] + [head.split(".")[-1] + "[]"]) if head else []
        out = []
        for it in items:
            if not isinstance(it, dict) or any(it.get(k) for k in rule.get("skip_if", [])):
                continue
            key = it.get(rule["per_item"])
            key = int(key) if isinstance(key, float) and key.is_integer() else key
            vals = [v for v in map(_num, _walk(it, tail.split("."))) if v is not None]
            if vals and key is not None:
                out.append((rule["metric"].replace("{" + rule["per_item"] + "}", str(key)), vals[0]))
        return out
    vals = [v for v in map(_num, _walk(payload, path.split("."), rule)) if v is not None]
    if not vals:
        return []
    red = rule.get("reduce")
    if red == "sum":
        v = sum(vals)
    elif red == "max":
        v = max(vals)
    elif red == "min":
        v = min(vals)
    elif red == "avg":
        v = sum(vals) / len(vals)
    else:
        v = vals[0]
    return [(rule["metric"], v)]


def derive(rules: dict, vals: dict[tuple, float]) -> dict[tuple, float]:
    """Bucket-level derived metrics from one bucket's combined values (per-sample ones are made in ingest):
    {"max": [inputs]} = the largest of the inputs present; {"sum": [inputs]} = their sum, only when all are present."""
    out = {}
    for d in rules.get("derived", []):
        if d.get("per_sample"):
            continue
        hosts = {(b, h) for b, h, _m in vals if not d.get("hosts") or h in d["hosts"]}
        for b, h in hosts:
            if "max" in d:
                parts = [vals[(b, h, m)] for m in d["max"] if (b, h, m) in vals]
                if parts:
                    out[(b, h, d["metric"])] = max(parts)
            elif "sum" in d:
                parts = [vals.get((b, h, m)) for m in d["sum"]]
                if all(x is not None for x in parts):
                    out[(b, h, d["metric"])] = sum(parts)
    return out


def per_sample(rule: dict, latest: dict, host: str, now: float) -> float | None:
    """A per-sample derived value from each input's latest reading: "prefer" when it is fresh, else the "sum" of the
    inputs when all are fresh (e.g. the laptop's battery draw on battery, else CPU package + GPU power)."""
    fresh = rule.get("fresh_s", 15)
    p = latest.get((host, rule.get("prefer")))
    if p and now - p[1] <= fresh:
        return p[0]
    parts = [latest.get((host, m)) for m in rule.get("sum", [])]
    if parts and all(x and now - x[1] <= fresh for x in parts):
        return sum(x[0] for x in parts)
    return None


def combine(agg: str, vals: list[float]) -> float:
    return max(vals) if agg == "max" else min(vals) if agg == "min" else sum(vals) / len(vals)


def view_bucket(rules: dict, range_s: int) -> int:
    for v in rules.get("views", []):
        if range_s <= v["max_range_s"]:
            return v["bucket_s"]
    return rules["tiers"][-1]["seconds"]


def tier_for(rules: dict, range_s: int) -> str:
    """The finest tier that still holds the whole range."""
    for t in rules["tiers"]:
        if t["keep_s"] is None or range_s <= t["keep_s"]:
            return t["name"]
    return rules["tiers"][-1]["name"]


# ---------- the recorder ----------
class Recorder:
    def __init__(self, rules: dict, dsn: str = DSN) -> None:
        self.rules, self.dsn = rules, dsn
        self.by_topic: dict[str, list[dict]] = {}
        for m in rules["metrics"]:
            self.by_topic.setdefault(m["topic"], []).append(m)
        self.base = rules["tiers"][0]
        self.acc: dict[tuple, list[float]] = {}  # (bucket start, host, metric) -> samples
        self.aggs: dict[str, str] = {}  # metric -> agg (concrete names, e.g. gpu0_temp_c)
        self.latest: dict[tuple, tuple] = {}  # (host, metric) -> (value, time): per-sample derived inputs, held values
        self.hold = {m["metric"]: m["hold_s"] for m in rules["metrics"] if m.get("hold_s")}
        self.sampled = [d for d in rules.get("derived", []) if d.get("per_sample")]
        self.lock = threading.Lock()
        self.seen = {"messages": 0, "points": 0, "last_flush": 0.0, "errors": 0}
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute(SCHEMA)

    def ingest(self, topic: str, payload: bytes, now: float | None = None) -> None:
        parts = topic.split("/")
        if len(parts) != 3 or parts[0] != PREFIX or parts[2] not in self.by_topic:
            return
        try:
            body = json.loads(payload or b"null")
        except ValueError:
            return
        if not isinstance(body, dict):
            return
        now = time.time() if now is None else now
        b = now - now % self.base["seconds"]
        with self.lock:
            self.seen["messages"] += 1
            host, changed = parts[1], set()
            for rule in self.by_topic[parts[2]]:
                for name, v in extract(body, rule):
                    self.acc.setdefault((b, host, name), []).append(v)
                    self.aggs.setdefault(name, rule["agg"])
                    self.latest[(host, name)] = (v, now)
                    changed.add(name)
            for d in self.sampled:
                ins = set(d.get("sum", [])) | {d.get("prefer")}
                if (not d.get("hosts") or host in d["hosts"]) and changed & ins:
                    v = per_sample(d, self.latest, host, now)
                    if v is not None:
                        self.acc.setdefault((b, host, d["metric"]), []).append(v)
                        self.aggs.setdefault(d["metric"], d.get("agg", "avg"))

    def flush(self, now: float | None = None) -> int:
        """Write every finished base bucket (one value per metric, combined by its agg)."""
        now = time.time() if now is None else now
        cur = now - now % self.base["seconds"]
        with self.lock:
            done = {k: v for k, v in self.acc.items() if k[0] < cur}
            for k in done:
                del self.acc[k]
            aggs = dict(self.aggs)
        if not done:
            return 0
        vals = {k: combine(aggs[k[2]], v) for k, v in done.items()}
        with self.lock:  # a slow metric (e.g. SMART every 10 min) is carried into the minutes between its readings
            for b, h in {(k[0], k[1]) for k in vals}:
                for m, hold in self.hold.items():
                    last = self.latest.get((h, m))
                    if (b, h, m) not in vals and last and 0 <= b - last[1] <= hold:
                        vals[(b, h, m)] = last[0]
        for k, v in derive(self.rules, vals).items():  # e.g. the hub's "hottest" temperature
            vals[k] = v
            aggs.setdefault(k[2], next(d["agg"] for d in self.rules.get("derived", []) if d["metric"] == k[2]))
        # inputs named with a leading underscore only feed derived metrics: they aren't stored
        rows = [(self.base["name"], k[0], k[1], k[2], v) for k, v in vals.items() if not k[2].startswith("_")]
        aggs = {m: a for m, a in aggs.items() if not m.startswith("_")}
        with psycopg.connect(self.dsn, autocommit=True) as c:
            with c.cursor() as cur_:
                cur_.executemany(
                    "insert into metric_defs(metric, agg) values(%s, %s) on conflict (metric) do update set agg = excluded.agg",
                    sorted(aggs.items()),
                )
                cur_.executemany(
                    "insert into metrics(tier, ts, host, metric, value) values(%s, to_timestamp(%s), %s, %s, %s) "
                    "on conflict (tier, host, metric, ts) do update set value = excluded.value",
                    rows,
                )
        with self.lock:
            self.seen["points"] += len(rows)
            self.seen["last_flush"] = now
        return len(rows)

    def rollup_and_prune(self, prune: bool) -> None:
        with psycopg.connect(self.dsn, autocommit=True) as c:
            for t in self.rules["tiers"]:
                if t.get("from"):  # recompute the last 3 buckets of this tier from its source tier
                    c.execute(
                        "insert into metrics(tier, ts, host, metric, value) "
                        "select %(t)s, to_timestamp(floor(extract(epoch from m.ts) / %(s)s) * %(s)s) b, m.host, m.metric, "
                        "case d.agg when 'max' then max(m.value) when 'min' then min(m.value) else avg(m.value) end "
                        "from metrics m join metric_defs d using (metric) "
                        "where m.tier = %(f)s and m.ts >= now() - make_interval(secs => %(w)s) "
                        "and m.ts < to_timestamp(floor(extract(epoch from now()) / %(s)s) * %(s)s) "
                        "group by b, m.host, m.metric, d.agg "
                        "on conflict (tier, host, metric, ts) do update set value = excluded.value",
                        {"t": t["name"], "s": t["seconds"], "f": t["from"], "w": 3 * t["seconds"]},
                    )
            if prune:
                names = [t["name"] for t in self.rules["tiers"]]
                for t in self.rules["tiers"]:
                    if t["keep_s"]:
                        c.execute(
                            "delete from metrics where tier = %s and ts < now() - make_interval(secs => %s)",
                            (t["name"], t["keep_s"]),
                        )
                c.execute("delete from metrics where not (tier = any(%s))", (names,))

    def series(self, host: str, metric: str, range_s: int) -> dict:
        tier, bucket = tier_for(self.rules, range_s), view_bucket(self.rules, range_s)
        with psycopg.connect(self.dsn, autocommit=True) as c:
            r = c.execute("select agg from metric_defs where metric = %s", (metric,)).fetchone()
            agg = r[0] if r else "avg"
            fn = {"max": "max", "min": "min"}.get(agg, "avg")
            pts = c.execute(
                f"select floor(extract(epoch from ts) / %(b)s) * %(b)s t, {fn}(value) from metrics "
                "where tier = %(tier)s and host = %(h)s and metric = %(m)s and ts > now() - make_interval(secs => %(r)s) "
                "group by 1 order by 1",
                {"b": bucket, "tier": tier, "h": host, "m": metric, "r": range_s},
            ).fetchall()
        return {
            "host": host,
            "metric": metric,
            "agg": agg,
            "tier": tier,
            "bucket_s": bucket,
            "points": [[int(t), v] for t, v in pts],
        }

    def catalog(self) -> list[dict]:
        with psycopg.connect(self.dsn, autocommit=True) as c:
            rows = c.execute(
                "select host, metric, max(ts), count(*) from metrics where tier = %s group by 1, 2 order by 1, 2",
                (self.base["name"],),
            ).fetchall()
        return [{"host": h, "metric": m, "last": t.timestamp(), "points": n} for h, m, t, n in rows]


def serve(rec: Recorder, port: int = PORT) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: object) -> None:
            pass

        def reply(self, code: int, body: object) -> None:
            b = json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self) -> None:
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/series":
                    if not q.get("host") or not q.get("metric"):
                        return self.reply(400, {"error": "host and metric are required"})
                    rng = int(q.get("range") or 3600)
                    return self.reply(200, rec.series(q["host"], q["metric"], max(60, min(rng, 400 * 86400))))
                if u.path == "/metrics":
                    return self.reply(200, rec.catalog())
                if u.path == "/rules":
                    return self.reply(200, rec.rules)
                if u.path in ("/", "/status"):
                    with rec.lock:
                        return self.reply(200, dict(rec.seen, pending=len(rec.acc), metrics=len(rec.aggs)))
                self.reply(404, {"error": "not found"})
            except Exception as e:
                self.reply(500, {"error": f"{type(e).__name__}: {e}"[:300]})

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    srv.daemon_threads = True
    return srv


def main() -> None:
    rules = load_rules()
    rec = Recorder(rules)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="metrics-recorder")
    client.reconnect_delay_set(1, 30)
    client.on_connect = lambda c, *_: (c.subscribe(f"{PREFIX}/+/+", qos=0), log("mqtt connected"))
    client.on_message = lambda c, u, msg: rec.ingest(msg.topic, msg.payload)
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()
    threading.Thread(target=serve(rec).serve_forever, daemon=True).start()
    log(
        f"metrics recorder: {len(rules['metrics'])} rules, tiers {[t['name'] for t in rules['tiers']]}, {DSN}, :{PORT}, mqtt {MQTT_HOST}"
    )
    last_rollup = last_prune = 0.0
    while True:
        try:
            rec.flush()
            now = time.time()
            if now - last_rollup >= 60:
                prune = now - last_prune >= rules.get("prune_every_s", 600)
                rec.rollup_and_prune(prune)
                last_rollup, last_prune = now, (now if prune else last_prune)
        except Exception as e:
            rec.seen["errors"] += 1
            log(f"{type(e).__name__}: {e}".splitlines()[0])
        time.sleep(5)


if __name__ == "__main__":
    main()
