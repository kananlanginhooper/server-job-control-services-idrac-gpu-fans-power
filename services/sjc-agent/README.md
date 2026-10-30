# sjc-agent (MQTT)

**Runs on:** every server (Docker)

Publishes this server's state to MQTT and runs the commands sent to it. **Topics and payloads: [`contracts/mqtt-topics.md`](../../contracts/mqtt-topics.md)**, the one source of truth shared with the dashboard backend.

- **State (retained):** BMC sensors (fans, temperatures, PSUs), DCMI power stats, GPUs, fan-control state, run level, system metrics.
- **Commands:** `fans` (`pct` or `auto`) and `gpu_limit`. Refused while fan-control's state is fresh. A manual fan hold hands back to Dell auto when its lease runs out, when a sensor reaches its Emergency temperature, or when fan-control comes back.
- Hardware access through [`lib/sjc_hw`](../../lib/sjc_hw/). Read-only mounts of `/proc`, `/run`, `/dev/shm`, `/usr/local/etc`; in-band IPMI via `/dev/ipmi0`.

| | |
|---|---|
| Reads | BMC (`ipmitool`), `nvidia-smi`, the controller state files, `/proc`. |
| Writes | MQTT `<SJC_MQTT_PREFIX>/<SJC_HOST>/...`. |
| Settings | The `SJC_*` keys in `.env.example`. Run: `docker compose up -d --build` in this folder; add `-f compose.bmc.yaml` on servers with a BMC (`/dev/ipmi0`), `-f compose.gpu.yaml` on GPU hosts (set `<HOST>_SJC_GPU_READONLY=1` while the GPU is lent out: `gpu_limit` is then refused), and `-f compose.hub.yaml` on the hub (mounts the power scaler's state folder; export `SJC_SCALER_DIR` first, see that file). |

Settings come from the repo's one `.env` (see [`.env.example`](../../.env.example)). The unit file(s) here are templates: `scripts/install-units.sh` renders `@REPO@`/`@USER@` and installs them. It doesn't enable or start them.
