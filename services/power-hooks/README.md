# Power hooks

**Runs on:** GPU server

Executables that the dash-agent runs on every run-profile change: `<hook> down <profile>` (Low Power / Meeting) or `<hook> up <profile>` (Adaptive / Unattended).

- Each hook must be idempotent and return at once.
- Each declares `# manages: <regex>` for the processes it handles, so fan-control's fallback freeze leaves them alone.
- `copilot.sh` wakes the LLM job watcher (SIGUSR1), which finishes and saves the call in progress and unloads the model. Without a watcher it stops the job itself.
- `whisper.sh` STOP/CONTs the GPU transcription job.

Install: copy or link the executables into `/dev/shm/power-hooks/` (the agent's `HOOK_DIR`).

| | |
|---|---|
| Reads | `/dev/shm/power-hooks/state.json` (written by the agent). |
| Writes | Signals to the managed processes; lines in `hooks.log`. |
| Settings | None. |

Settings come from the repo's one `.env` (see [`.env.example`](../../.env.example)). The unit file(s) here are templates: `scripts/install-units.sh` renders `@REPO@`/`@USER@` and installs them. It doesn't enable or start them.
