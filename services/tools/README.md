# Tools

**Runs on:** anywhere

`task-report.py`: post one task (title, group, host, phase, done/total, rate, message...) to the dashboard's Tasks table. `task-report.py --help` lists every option.

| | |
|---|---|
| Reads | Arguments. |
| Writes | `POST HUB_API_URL/api/tasks`. |
| Settings | `HUB_API_URL`. |

Settings come from the repo's one `.env` (see [`.env.example`](../../.env.example)). The unit file(s) here are templates: `scripts/install-units.sh` renders `@REPO@`/`@USER@` and installs them. It doesn't enable or start them.
