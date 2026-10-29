# Peak power

**Runs on:** hub, GPU server (root)

Reads the BMC's own high-rate **System Peak Power** counter (`ipmitool delloem powermonitor`) every 30 s, then resets it. This catches sub-second spikes that 1/s DCMI polling misses.

| | |
|---|---|
| Reads | `ipmitool` (in-band). |
| Writes | `/dev/shm/peak-power.json`: `{ts, peak_w, peak_at, window_s}`. |
| Settings | `IPMITOOL_CMD` (per host; may carry an `env LD_LIBRARY_PATH=...` prefix). Args: `peak-power.py <out.json> [ipmitool command...]`. |

Settings come from the repo's one `.env` (see [`.env.example`](../../.env.example)). The unit file(s) here are templates: `scripts/install-units.sh` renders `@REPO@`/`@USER@` and installs them. It doesn't enable or start them.
