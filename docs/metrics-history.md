# Metrics history: what is stored, how often, and for how long

MQTT carries only the latest value of each statistic. The **metrics recorder** on the hub ([`services/metrics-recorder`](../services/metrics-recorder/)) subscribes to every agent's state topics and stores the history the dashboard graphs are drawn from. Everything below is set in one file, [`metrics-rules.json`](../services/metrics-recorder/metrics-rules.json): change it there, then restart the recorder.

These rules carry over the ones the classic hub (`:8090`) has applied to its own `stats` table since 2026-09-28: one point per minute, kept 24 hours, rolled up to hourly points kept forever, power, temperature and fan as maxima, voltage as a minimum.

## Resolution: three layers

| Layer | Resolution | Where | Survives a page refresh? |
|---|---|---|---|
| Live | every message (2–5 s per topic) | the page's own MQTT subscription | **No.** After a refresh the page shows the stored minute points and builds finer detail again from new messages |
| `1m` tier | one point per metric per minute | Postgres `server_metrics.metrics` | Yes, for **24 hours** |
| `1h` tier | one point per metric per hour | the same table | Yes, **forever** |

## How a bucket's samples become one value

Every sample that arrives within a bucket is kept until the bucket closes. The bucket then stores one value per metric, chosen by the metric's `agg`:

| `agg` | Used for | Why |
|---|---|---|
| `max` | power, temperatures, fan speeds, GPU power and memory, stuck-process counts, disk-full % | a short spike or a hot moment must stay visible in the graph |
| `min` | PSU voltage | a dip is what matters |
| `avg` (default) | CPU %, RAM %, network and disk rates, GPU utilization and clocks | rates and usage |

The hourly tier is rolled up from the minute tier with the same `agg`, so an hourly power point is that hour's highest minute.

## Tiers and pruning

```json
"tiers": [
  {"name": "1m", "seconds": 60,   "keep_s": 86400, "from": null},
  {"name": "1h", "seconds": 3600, "keep_s": null,  "from": "1m"}
]
```

- A tier with `from` is rebuilt every minute from its source tier, for its last three buckets.
- `keep_s` is how long a tier's points are kept; `null` keeps them forever.
- Pruning runs every `prune_every_s` (600). Points in a tier that is no longer listed are deleted.
- To keep minute points for a week instead of a day, set `"keep_s": 604800` on `1m`. To add a 5-minute tier kept for 30 days: `{"name": "5m", "seconds": 300, "keep_s": 2592000, "from": "1m"}`.

## Graph buckets per range

A read picks the finest tier that still covers the whole range, then groups points into the range's bucket (`views`):

| Range up to | Bucket | Tier |
|---|---|---|
| 1 hour | 1 min | `1m` |
| 6 hours | 2 min | `1m` |
| 24 hours | 5 min | `1m` |
| longer | 1 hour | `1h` |

## What is recorded (as of 2026-11-01)

Hosts are the agents' short names (the `<HOST>` in their settings), e.g. `srv1`, `srv2`, `laptop`. A metric is stored only where the host publishes it.

| Metric | From topic, field | `agg` |
|---|---|---|
| `power_w`, `power_peak_w` | `power`: `w`, `peak_5s` (BMC wall watts) | max |
| `power_true_peak_w` | `system`: `peak_power.peak_w` (iDRAC 30-s true peak) | max |
| `cpu_pct`, `mem_pct`, `mem_used_b`, `swap_pct` | `system` | avg |
| `net_rx_bps`, `net_tx_bps` | `system`: `net_io.*`, summed over NICs | avg |
| `disk_read_bps`, `disk_write_bps` | `system`: `disk_io.*`, summed over drives | avg |
| `cpu_pkg_w` | `system`: `cpu_pkg_w.*` summed over sockets (laptop: `power.cpu_pkg_w`) | max |
| `power_w` (laptop) | derived per reading: battery draw when discharging, else CPU package + GPU power (the laptop has no wall meter; the hub's laptop power line) | max |
| `dstate` | `system`: `dstate.count` (processes stuck in uninterruptible I/O) | max |
| `temp_max_c`, `fan_max_rpm` | `sensors`: hottest sensor, fastest fan | max |
| `bmc_temp_max_c`, `bmc_fan_max_rpm` | `idrac`: BMC temperatures and fans | max |
| `psu_volt_min` | `idrac`: `psu.*.v`, lowest PSU | min |
| `psu_volt:<psu>` | `idrac`: each PSU's voltage (`psu_volt:ps1`, `psu_volt:ps2`) | min |
| `hottest_c` | derived: the hottest of BMC temperatures except Inlet, CPU `Package id N`, NVMe `Composite` and SMART drive temperatures (laptop: its Lenovo sensors). The hub's "temp" line | max |
| `fan_control_pct` | `safety`: `fan_control.fan_pct` | max |
| `gpu<N>_temp_c`, `_power_w`, `_mem_used_b`, `_fan_rpm` | `gpu`: per card | max |
| `gpu<N>_util_pct`, `_sm_mhz`, `_power_limit_w` | `gpu`: per card | avg |
| `disk_pct:<mount>` | `disks`: per mount, skipping NFS mounts and bind-mount duplicates | max |
| `drive_temp_max_c` | `health`: hottest drive by SMART (read every 10 min, held up to 15 min) | max |

Adding a metric is one line in `metrics` (keys below). Removing one stops new points; old ones age out with their tier.

| Key | Meaning |
|---|---|
| `topic` | the agent topic's last part (`system`, `gpu`, `sensors`, ...) |
| `path` | where the value is: `a.b` = nested key, `a.*.b` = every value of the dict `a`, `a[].b` = every item of the list `a` |
| `reduce` | when the path yields several values: `sum`, `max`, `min` or `avg` |
| `per_item` | one metric per list item, named from that item's field (`gpu{index}_temp_c`, `disk_pct:{mount}`) |
| `skip_if` | item fields that, when set, skip the item (e.g. `remote`, `same_as`) |
| `match` / `exclude` | filters on list items: `{"field": "regex"}`; an item is used when every `match` matches and no `exclude` does |
| `per_item: "@key"` | one metric per key of a dict (`psu.*.v` → `psu_volt:{key}`) |
| `hold_s` | carry a slow metric's last value into the minutes between its readings, for up to this long |
| `agg` | how a bucket's samples combine (above); default `avg` |

A metric whose name starts with `_` only feeds derived metrics and isn't stored.

`derived` metrics combine other metrics of the same host:

| Key | Meaning |
|---|---|
| `max: [inputs]` | per bucket: the largest of the inputs present |
| `sum: [inputs]` | per bucket: their sum, only when all are present |
| `per_sample: true`, `prefer`, `sum`, `fresh_s` | per reading, from each input's latest value: `prefer` when it is fresh, else the sum of the inputs when all are fresh. Then combined per bucket by `agg` like any metric |
| `hosts` | limit to these hosts |

## Reading the history

The recorder serves it read-only on the hub, port 8108:

- `GET /series?host=srv2&metric=power_w&range=86400` → `{host, metric, agg, tier, bucket_s, points: [[unix_s, value], ...]}`
- `GET /metrics`: every host and metric with its latest stored minute
- `GET /rules`: the rules in force
- `GET /status`: messages seen, points written, errors

Or query Postgres directly: database `server_metrics`, table `metrics(tier, ts, host, metric, value)`; `metric_defs(metric, agg)` gives each metric's combining rule.

## Not covered yet

- **Backfilled 2026-11-01** from the classic hub's MySQL `stats` (`backfill_hub_stats.py`, one-time): hourly points back to 2026-09-23, minute points for the hub's last 24 hours, 37 series (power, true peak, GPU power / temperature / fan, hottest temperature, BMC fans, PSU voltages, CPU, network, disk). The hub's transfer metrics (copy speeds, bytes remaining) aren't host statistics and stay in its table.
- The 15-second "every sensor" and per-NIC / per-drive graphs the hub holds in memory for two hours aren't stored here. The minute tier keeps each host's hottest sensor and summed rates; adding per-sensor or per-NIC series is a rules change (`per_item`), at the cost of more rows.
