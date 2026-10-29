# RAM bandwidth

**Runs on:** hub, GPU server (root)

RAM read/write bandwidth from the Intel uncore memory-controller (IMC) CAS counters through `perf_event_open` (1 CAS = 64 bytes), every 5 s. Intel only.

| | |
|---|---|
| Reads | `/sys/bus/event_source/devices/uncore_imc_*`. |
| Writes | `<out.json>` (default `/dev/shm/imc-bw.json`): `{ts, read_bps, write_bps, sockets}`. |
| Settings | None. Arg: the output path. |

Settings come from the repo's one `.env` (see [`.env.example`](../../.env.example)). The unit file(s) here are templates: `scripts/install-units.sh` renders `@REPO@`/`@USER@` and installs them. It doesn't enable or start them.
