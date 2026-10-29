# GPU switch

**Runs on:** GPU server (root)

Follows the dashboard's **Disable GPU** toggle (`/api/powertarget` field `gpu_off`), checked every 15 s.

- **Off:** holds the LLM job runner (its HOLD file, tagged `gpu-switch`), stops it gracefully through its power hook, and stops Ollama (`OLLAMA_UNIT`), freeing the VRAM.
- **On:** undoes only what it did (a HOLD set by someone else stays), starts Ollama again, and resumes the job only when the run mode isn't scaled down.

| | |
|---|---|
| Reads | `HUB_API_URL/api/powertarget`. |
| Writes | `/dev/shm/gpu-switch.json`, `/dev/shm/gpu-switch.log`; systemctl stop/start of `OLLAMA_UNIT`. |
| Settings | `HUB_API_URL`, `OLLAMA_UNIT`. |

Settings come from the repo's one `.env` (see [`.env.example`](../../.env.example)). The unit file(s) here are templates: `scripts/install-units.sh` renders `@REPO@`/`@USER@` and installs them. It doesn't enable or start them.
