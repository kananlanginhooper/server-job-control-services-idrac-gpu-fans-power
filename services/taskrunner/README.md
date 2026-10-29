# Task runner

**Runs on:** every server (as the service user)

The one job service per server. It runs the task rows in a **local** Postgres `tasks` table (continuous workers, one-shots, schedules, instance counts) and restarts crashed tasks with back-off. It waits quietly when a task's need is missing, sizes instances to the power target, logs each run in `task_runs`, and serves status JSON on :8098. CLI: `taskctl`. It stays local on purpose: the runner starts and resumes its work with the other machines down.

| | |
|---|---|
| Reads | Local Postgres (`TASKRUNNER_DSN`, peer auth); `HUB_API_URL/api/powertarget`. |
| Writes | Child processes; logs in `~/taskrunner/logs`; :8098. |
| Settings | `TASKRUNNER_DSN`, `TASKRUNNER_PORT`, `TASKRUNNER_LOGS`, `TASKRUNNER_POWER_URL` (default `HUB_API_URL/api/powertarget`). Install: `install.sh`. |

Settings come from the repo's one `.env` (see [`.env.example`](../../.env.example)). The unit file(s) here are templates: `scripts/install-units.sh` renders `@REPO@`/`@USER@` and installs them. It doesn't enable or start them.
