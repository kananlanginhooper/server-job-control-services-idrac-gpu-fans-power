# Dashboard agent

**Runs on:** GPU server, GPU desktop (as the service user, sudo -n for a few reads)

HTTP agent on :8091 that the hub dashboard polls:

- **Readings:** CPU, frequencies, throttle counts, RAM and EDAC errors, disks and SMART, NICs, mounts, hwmon, RAPL package power, GPU (`nvidia-smi`), IPMI sensors and fans, peak power and RAM bandwidth (from the files above), watched processes.
- **Run profile:** pulls the run profile and Quick Cool from the dashboard and writes them to `/dev/shm` for fan-control. On a profile change it runs the **power hooks** (`/dev/shm/power-hooks/*` as `<hook> down|up <profile>`). `POST /runprofile` is accepted only from `DASH_CLIENT_IP`.
- **RAPL caps:** applies the CPU package caps from `/api/powercaps` (`AGENT_POWERCAPS=1`; set 0 where the dashboard doesn't manage the CPU caps).

| | |
|---|---|
| Reads | `HUB_API_URL` `/api/quickcool`, `/api/runprofile`, `/api/powercaps`; sysfs, `/proc`, `nvidia-smi`, `ipmitool`, `smartctl`; `/dev/shm/*.json` from the other services. |
| Writes | JSON on `GET /` (port arg, default 8091); `/dev/shm/run-profile.json`, `/dev/shm/quick-cool.json`, `/dev/shm/power-hooks/state.json`; the RAPL constraint files. |
| Settings | `HUB_API_URL`, `HOST_KEY`, `DASH_CLIENT_IP`, `AGENT_POWERCAPS`. Args: `dash-agent.py <port> <nic|all>`. |

Settings come from the repo's one `.env` (see [`.env.example`](../../.env.example)). The unit file(s) here are templates: `scripts/install-units.sh` renders `@REPO@`/`@USER@` and installs them. It doesn't enable or start them.
