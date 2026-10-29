# Proposal: which services would benefit from FastAPI

Not done; for the owner's decision. The template's back-end shape is FastAPI with routers, Pydantic schemas, pydantic-settings and a complete OpenAPI. Here's where that pays off in this repo, and where it doesn't.

## Worth it

| Service | Today | With FastAPI | Benefit | Risk |
|---|---|---|---|---|
| **dash-agent** (:8091, GPU server + GPU desktop) | `BaseHTTPRequestHandler`. `GET /` returns one big JSON (~40 fields, nested); `POST /runprofile` is IP-checked by hand | `GET /api/state` with a `response_model`, `POST /api/runprofile` with a schema, `/api/health`; the same JSON kept on `GET /` for the dashboard | A typed contract the dashboard can generate types from, request validation, and testing with httpx + mocks. The sensor loops stay threads | Medium: it's the dashboard's data source. Needs a field-by-field response comparison with the live agent before cutover |
| **whisper-server** (:8701+) | `BaseHTTPRequestHandler`, `POST /transcribe` (raw WAV body) | The same route as `UploadFile` or raw body, `response_model` with segments, `/health` | OpenAPI for the video pipeline client, cleaner Low Power waiting (async) | Low: stateless; one request at a time stays (a lock or a single worker) |
| **vision-server** (:8760) | `BaseHTTPRequestHandler`, binary batch `POST /embed`, `GET /search`, `/health` | Routers for embed/search/health, schemas for the JSON parts | OpenAPI + validation for the image pipeline | Low to medium: the binary framing stays as it is (custom body), so the gain is mostly on `/search` and `/health` |
| **taskrunner** status (:8098) | A small status JSON plus control | `GET /api/tasks`, `POST /api/tasks/{name}/{start,stop,pause}`, `PUT .../instances` | The control API the independence plan calls for, typed for the dashboard | Medium: it has to stay up when the DB is down. Keep the runner loop separate from the API process, or run the API in a thread |

## Not worth it (keep as plain services)

- **fan-control, power-guard, peak-power, imc-bw, gpu-switch, gpu-perf, nvme-watch:** these are control loops and sensors with no inbound API. They talk to the dashboard as clients. Wrapping them in a web framework adds a moving part to the most safety-critical code, with nothing gained.
- **sjc-agent:** its interface is MQTT (the topic contract), not HTTP. Its HTTP face is the dashboard backend, which is already FastAPI in the dashboard repo.
- **Watchdogs and ssd-write-cache:** shell oneshots and timers.

## Suggested order, if approved

1. **whisper-server** (stateless, easy to compare: send the same WAV to both and diff the JSON).
2. **dash-agent**, behind a parity test that diffs live `GET /` against the new `GET /` key by key, on the laptop against the live agent (read-only).
3. **taskrunner** control API, together with the dashboard's task view.
4. **vision-server**, if the image pipeline wants the typed `/search`.

Each in its own branch with a mock-backed test suite. Units and ports stay the same, so the cutover plan doesn't change.
