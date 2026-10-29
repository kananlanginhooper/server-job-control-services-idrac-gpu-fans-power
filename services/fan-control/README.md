# Fan control

**Runs on:** hub, GPU server (root)

One thermal controller per server, so nothing fights over the fans. Fixed modes, no fan curves: **Quick Cool > Emergency > Run Mode > Normal**, each a fixed speed. Adaptive run modes step the case fans (and, on the GPU server, the GPU card fan) toward per-component aim temperatures.

- Server fans: Dell IPMI raw commands (in-band `ipmitool`).
- Hub: the iDRAC DCMI whole-server power cap for the run mode, lowered (never raised) by an unexpired power-guard throttle.
- GPU server: the GPU card fan and power limit (`nvidia-smi`), the GPU power limit lowered by the throttle (`/api/throttle` `<GPU_THROTTLE_KEY>`, floor 100 W). It pauses the listed GPU jobs in Low Power.
- **Fails safe:** on any error or exit it hands the fans back to Dell automatic, the card fan to automatic, and the power limit to the fixed value.
- **After a restart** an adaptive mode starts at the profile's case-fan RPM, then steps down to its equilibrium over a few minutes (louder fans briefly; not a fault).

| | |
|---|---|
| Reads | IPMI sensors; hwmon, NVMe and SMART temperatures; PERC ROC temperature (`perccli`); `component-limits.json` (`LIMITS_FILE`); the run profile and Quick Cool files the dashboard/agent write (`/run` on the hub, `/dev/shm` on the GPU server); `/dev/shm/power-throttle.json`; `HUB_API_URL/api/throttle`. |
| Writes | State JSON (`/run/fan-control.json` on the hub, `/dev/shm/fan-control.json` on the GPU server) that the dashboard and sjc-agent read; a log. |
| Settings | `ROLE` (`hub` / `gpu-server`), `HOST_KEY`, `HUB_API_URL`, `GPU_THROTTLE_KEY`, `LIMITS_FILE`, `PERCCLI`. Copy `component-limits.example.json` to `LIMITS_FILE`. |

Settings come from the repo's one `.env` (see [`.env.example`](../../.env.example)). The unit file(s) here are templates: `scripts/install-units.sh` renders `@REPO@`/`@USER@` and installs them. It doesn't enable or start them.
