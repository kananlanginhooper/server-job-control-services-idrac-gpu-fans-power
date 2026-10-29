# Whisper server

**Runs on:** GPU server (as the service user)

Stateless transcription worker. `POST /transcribe` takes a 16 kHz mono WAV and returns `{language, lang_prob, duration, segments: [{s, e, t}]}`. No database and no files kept: the caller claims the work and stores the results. One model per process, one request at a time; run several instances (`whisper-server@<port>`). It never logs transcript text. On CUDA, requests wait (rather than fail) while the run mode is Low Power.

| | |
|---|---|
| Reads | Requests; `/dev/shm/power-hooks/state.json`. |
| Writes | Responses; a GPU job feed `/dev/shm/gpu-job.whisper-gpu-<port>.json`. |
| Settings | `WHISPER_BIND`, `WHISPER_DEVICE` (`cpu`/`cuda`), `WHISPER_MODELS`, `WHISPER_PYTHON` (a venv with faster-whisper). |

Settings come from the repo's one `.env` (see [`.env.example`](../../.env.example)). The unit file(s) here are templates: `scripts/install-units.sh` renders `@REPO@`/`@USER@` and installs them. It doesn't enable or start them.
