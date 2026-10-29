# Vision server

**Runs on:** GPU desktop (as the service user)

Image signals on the GPU, listening on 127.0.0.1:8760 only (reach it through an ssh tunnel):

- `POST /embed`: a binary batch of images plus face landmarks. Returns, per image hash, a SigLIP embedding, top tags and ArcFace face embeddings. Every embedding is also kept in `store/` for search.
- `GET /search?q=...&k=50`: phrase search over every stored embedding (hashes and scores only).
- `GET /health`.

| | |
|---|---|
| Reads | Requests; models under `VISION_BASE`. |
| Writes | Responses; `VISION_BASE/store/clip-*.npz`. |
| Settings | `VISION_BASE`, `VISION_PYTHON` (a venv with torch, open_clip, onnxruntime). |

Settings come from the repo's one `.env` (see [`.env.example`](../../.env.example)). The unit file(s) here are templates: `scripts/install-units.sh` renders `@REPO@`/`@USER@` and installs them. It doesn't enable or start them.
