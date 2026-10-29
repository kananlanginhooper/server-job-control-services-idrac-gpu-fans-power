# Power guard

**Runs on:** hub (as the service user)

Holds the combined power line set in the dashboard (`/api/powertarget`). Every 10 s it reads the targets and the live readings (`/api/state`):

- **Over** (combined 5-s peak > line + margin, or 60-s average > line; or the GPU server alone over its own line): tighten one step through `POST /api/throttle`. Hub DCMI cap = its 5-s peak - `step_w`; GPU server RAPL package caps = actual package W - `step_w`/2; the GPU power limit last.
- **Under** for `up_hold_s`: step every cap back up, GPU first; a cap that reaches its normal value is cleared.
- Never touches jobs. Throttles are lower-only and self-expiring (`hold_s`).

| | |
|---|---|
| Reads | `HUB_API_URL` `/api/powertarget`, `/api/state`, `/api/throttle`; optional fallback RAPL read over ssh (`GPU_SERVER_SSH`, `SSH_KEY`). |
| Writes | `POST /api/throttle`; `state.json` and `guard.log` next to the script (the dashboard's throttle chart reads the log). |
| Settings | `HUB_API_URL`, `HUB_KEY`, `GPU_SERVER_KEY`, `GPU_THROTTLE_KEY`, `GPU_SERVER_SSH`, `SSH_KEY`. Tuning: `power-guard.json` next to the script (copy `power-guard.example.json`; live-editable). Commands: `power-guard.py run [--dry-run] | status | clear <host>|all`. |

Settings come from the repo's one `.env` (see [`.env.example`](../../.env.example)). The unit file(s) here are templates: `scripts/install-units.sh` renders `@REPO@`/`@USER@` and installs them. It doesn't enable or start them.
