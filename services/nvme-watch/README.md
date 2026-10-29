# NVMe watch

**Runs on:** hub (root)

Every minute, per NVMe controller: state, temperature (alert at 84 C composite), SMART error counters, and kernel timeout/reset/abort lines. Logs every change and reports each drive to the dashboard Tasks table.

| | |
|---|---|
| Reads | `/sys/class/nvme`, `nvme smart-log`, the kernel log. |
| Writes | `/var/log/nvme-watch.log`, `/run/nvme-watch.json`, `POST HUB_API_URL/api/tasks`. |
| Settings | `HUB_API_URL`, `HOST_KEY`. |

Settings come from the repo's one `.env` (see [`.env.example`](../../.env.example)). The unit file(s) here are templates: `scripts/install-units.sh` renders `@REPO@`/`@USER@` and installs them. It doesn't enable or start them.
