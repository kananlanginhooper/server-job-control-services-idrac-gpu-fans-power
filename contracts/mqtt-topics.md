# MQTT topic contract (version 1)

**This file is the one source of truth** for the topics between `sjc-agent` (this repo, [`services/sjc-agent`](../services/sjc-agent/)) and the dashboard backend (repo `server-job-control-dashboard-idrac-gpu-fans-power`).

- The dashboard references this file by URL, pinned to a commit. It doesn't keep a copy.
- The machine-readable version is [`mqtt-topics.json`](mqtt-topics.json). Change both together.
- A breaking change bumps `version` and is announced to the dashboard thread before it's pushed.

## Basics

- Broker: Mosquitto, LAN only, anonymous, port 1883. It runs with the dashboard (its compose, `hub` profile). Keep 1883 off the internet.
- Topic root: `<prefix>/<host>/...`
  - `prefix` = `SJC_MQTT_PREFIX` (default `sjc`).
  - `host` = `SJC_HOST`, which defaults to the server's `HOST_KEY` (see `.env.example`).
- Payloads are JSON (UTF-8). Times are Unix seconds (float).
- Every state topic is **retained**, so a new subscriber gets the latest state at once.
- QoS 0 for state; QoS 1 for `status`, commands and results.
- Subscribers ignore fields they don't know. Producers add fields freely, but don't rename or remove them within a version.

## State topics (agent → everyone)

| Topic | Retain / QoS | Every | Payload |
|---|---|---|---|
| `<p>/<h>/status` | yes / 1 | on connect; last will | the plain strings `online` or `offline` (not JSON) |
| `<p>/<h>/idrac` | yes / 0 | `SJC_SENSOR_EVERY_S` (5) | `{at, fans: [{name, rpm, ok}], redundancy, temps: [{name, c, ok}], psu: {ps1: {v, a, status}, ps2: {...}}, power_w, error?}` |
| `<p>/<h>/power` | yes / 0 | with `idrac` | `null`, or `{t, w, peak_5s, max_60s, avg_60s, min_60s, max_5m, every_s}` from DCMI read every `SJC_POWER_EVERY_S` (1) |
| `<p>/<h>/gpu` | yes / 0 | `SJC_GPU_EVERY_S` (2), only where `SJC_NVIDIA_SMI` is set | `{at, cards: null \| [{index, name, temp, util, mem_used, mem_total, fan_pct, power_w, power_limit_w, power_min_w, power_max_w, sm_mhz, mem_util, pcie_gen, pcie_width}]}`. Memory in bytes. `cards: null` = no GPU tooling |
| `<p>/<h>/safety` | yes / 0 | `SJC_SAFETY_EVERY_S` (5) | `{at, fan_control: {ts, mode, profile, fan_pct, driven_by, quick_cool, emergency, emergency_detail, adaptive, gpu_paused, gpu_case_floor, cpus, nvme, drives, raid, exhaust, gpus, events: [...], unit, age_s, running, in_emergency}, emergency_c: {kind: celsius}, manual: {pct, until, by, released: {at, why} \| null}}` |
| `<p>/<h>/runlevel` | yes / 0 | with `safety` | `{at, profile_file: {...}, profile_active, mode, matches, throttle: {...} \| null, scaler: {unit, state} \| null}` |
| `<p>/<h>/system` | yes / 0 | `SJC_SYSTEM_EVERY_S` (5) | `{at, cpu_pct, cpu_count, load: [1, 5, 15], mem: {total, used, pct}, swap: {total, used, pct}, disk_io: {dev: {read_bps, write_bps, busy_pct}}, net_io: {nic: {rx_bps, tx_bps}}, hwmon_max_c: {chip: c}, uptime_s}` |

`fan_control` fields are whatever fan-control writes to its state file, filtered to the listed keys. `running` is true while that state is no older than `SJC_CONTROLLER_STALE_S` (30 s).

## Commands (backend → one agent) and results

> **Deprecated (2026-10-29, owner's decision):** no new users. Fan override and the GPU power limit go through the :8090 hub as standard operation, and other agent commands go through the REST agent API ([`agent-api.md`](agent-api.md)). MQTT is for live state only. These command topics are removed in topic contract v2.

- Request: publish to `<p>/<h>/cmd/<name>` (QoS 1, not retained) with `{"id": "<unique>", "by": "<who>", ...args}`.
- Reply: the agent publishes `{"id", "cmd", "at", "ok": true|false, "error"?, ...}` to `<p>/<h>/result/<id>` (QoS 1, not retained). There's no reply when the request has no `id`.

| Command | Args | Success reply | Refused when |
|---|---|---|---|
| `fans` | `{"pct": SJC_MANUAL_MIN_PCT..100}` | `{ok, pct, until}`: a manual hold, which hands back to Dell auto after `SJC_MANUAL_LEASE_S` | fan-control is running; pct out of range; any sensor at its Emergency temperature |
| `fans` | `{"auto": true}` | `{ok, mode: "dell-auto"}` | fan-control is running |
| `gpu_limit` | `{"w": watts, "index": 0}` | `{ok, index, w}` | fan-control is running; no such GPU; w outside the card's min..max |

**Safety rules (part of the contract):**
- Fan and GPU commands are refused while fan-control's state is fresh, because fan-control owns them.
- A manual fan hold ends by itself, with a hand-back to Dell auto, when any of these happens: the lease runs out, any sensor reaches its Emergency temperature, or fan-control comes back.

## Subscribers

- The dashboard backend subscribes to `<p>/+/+` (state, QoS 0) and `<p>/+/result/+` (QoS 1).
- Automated tests use a mock agent that publishes these same shapes. They never send commands to a real agent.
