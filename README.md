# Server services: BMC (iDRAC), GPUs, fans, power

The helper services that run on a small home lab of three Linux machines. They report power, temperature, RAM and GPU statistics, and they control fans, power caps and GPU work:

- **hub**: a Dell rack server. It runs the dashboard API, the database and the power guard.
- **GPU server**: a Dell rack server with a GPU.
- **GPU desktop**: a workstation GPU box, on its own power circuit.

The dashboard UI and its API live in a separate repo, `server-job-control-dashboard-idrac-gpu-fans-power`. This repo holds the hardware side.

**Status:** the code here was collected from the running servers on 2026-10-28. Hosts, addresses and paths moved into one `.env`; nothing else changed. The live services still run their deployed copies until a planned cutover. The steps are in "Deploying" below.

## Services

| Service | Runs on | What it does |
|---|---|---|
| [`fan-control`](services/fan-control/) | hub, GPU server | One thermal controller per server, in fixed modes with no fan curves: Quick Cool > Emergency > Run Mode > Normal. Sets server fans through IPMI raw commands. Sets the hub's DCMI whole-server power cap. On the GPU server it also sets the GPU card fan and power limit, lowered by the power guard's throttle. On any failure or exit it falls back to Dell automatic fans and the default GPU limit. Temperature limits come from `component-limits.json` |
| [`power-guard`](services/power-guard/) | hub | Holds the combined power line. Every 10 s it reads the dashboard's power target and live readings. When a 5-s peak or 60-s average goes over the line, it steps caps down through `POST /api/throttle`: hub DCMI, GPU-server RAPL, then the GPU last. When readings stay under the line, it steps them back up, GPU first. Settings in `power-guard.json` (live-editable) |
| [`peak-power`](services/peak-power/) | hub, GPU server | Reads the BMC's own high-rate "System Peak Power" counter every 30 s, then resets it. This catches sub-second spikes that 1/s DCMI polling misses |
| [`imc-bw`](services/imc-bw/) | hub, GPU server | RAM read/write bandwidth from the Intel uncore memory-controller counters, every 5 s |
| [`dash-agent`](services/dash-agent/) | GPU server, GPU desktop | HTTP agent on :8091 for the dashboard: CPU, RAM, disks, NICs, RAPL package power, GPU, SMART and IPMI sensors. Applies the RAPL caps (`AGENT_POWERCAPS`). Runs the **power hooks** on every run-profile change |
| [`power-hooks`](services/power-hooks/) | GPU server | Executables called as `<hook> down\|up <profile>` when the run profile changes, so GPU jobs pause in Low Power and resume after. Each declares `# manages: <regex>` so fan-control's fallback freeze leaves those processes alone |
| [`gpu-switch`](services/gpu-switch/) | GPU server | Follows the dashboard's "Disable GPU" toggle. Off: holds GPU jobs and stops Ollama, freeing VRAM. On: undoes only what it did |
| [`gpu-perf`](services/gpu-perf/) | hub | Per-minute GPU table `gpu_perf` from the GPU server's agent: temperature, power draw and cap, clocks, utilization, card and case fans, VRAM used/total, memory-bandwidth %, PCIe rx/tx, host RAM bandwidth. Optional per-minute LLM token throughput |
| [`nvme-watch`](services/nvme-watch/) | hub | Every minute, per NVMe: state, temperature, SMART error counters, kernel timeouts and resets. Reports to the dashboard Tasks table |
| [`gpu-watchdog`](services/gpu-watchdog/) | GPU desktop | `gpu-init`: loads the NVIDIA driver (blacklisted at boot, so a dead card can't block the boot) and sets the power cap. The watchdog timer: card missing → PCI rescan → capped reboot; driver down → re-init → capped reboot |
| [`wifi-watchdog`](services/wifi-watchdog/) | GPU desktop | Wi-Fi-only box: 3 failed gateway pings → reload the driver → capped reboot. Plus the Intel 8265 driver options that kept its firmware from crashing |
| [`whisper-server`](services/whisper-server/) | GPU server | Stateless Whisper transcription workers (`POST /transcribe`). CPU by default, CUDA optional. Honors Low Power |
| [`vision-server`](services/vision-server/) | GPU desktop | SigLIP image tags and phrase search, plus ArcFace face embeddings, on the GPU (`/embed`, `/search`, `/health`) |
| [`ssd-write-cache`](services/ssd-write-cache/) | hub | Boot: turns on the drive write cache for the listed data SSDs, matched by serial |
| [`taskrunner`](services/taskrunner/) | every server | The one job service per server. Runs task rows from a local Postgres `tasks` table, sizes them to the power target, status on :8098, CLI `taskctl` |
| [`tools/task-report.py`](services/tools/) | anywhere | CLI: post one task to the dashboard's Tasks table |
| [`sjc-agent`](services/sjc-agent/) | every server (Docker) | Publishes the server's BMC sensors, DCMI power, GPU, fan-control and run-level state and system metrics to MQTT. Runs `fans` / `gpu_limit` commands, refused while fan-control owns them, with a manual-hold lease and a hand-back to Dell auto at Emergency temperature. Built on [`lib/sjc_hw`](lib/sjc_hw/) |

**MQTT topic contract:** [`contracts/mqtt-topics.md`](contracts/mqtt-topics.md) (+ `.json`) is the one source of truth for the topics between `sjc-agent` and the dashboard backend. The dashboard repo references it pinned to a commit and doesn't keep a copy.

**Not here** (lives elsewhere): the dashboard API and UI; Ollama itself (stock units); the ssh tunnels to the GPU desktop; and the LLM job queue, which is in the copilot-history project and is woken by `power-hooks/copilot.sh`.

## Configuration: one `.env`

Every service reads the **same** gitignored `.env` at the repo root. Copy [`.env.example`](.env.example) to `.env` and fill it in.
- Python reads it through [`lib/svcenv.py`](lib/svcenv.py), shell through [`lib/svcenv.sh`](lib/svcenv.sh).
- A key is looked up as `<HOST>_KEY` first (the machine's short hostname, upper-cased), then as `KEY`, so one file serves every server. Adding a server means adding its `<HOST>_` lines, not changing code.
- Secrets (database and BMC passwords) live only in `.env`. The repo has the public-repo guard installed as its pre-push hook.

## Deploying

Deploying is `git pull` in each server's clone, plus copying `.env` next to it. The units in `services/*/` use `@REPO@` and `@USER@` placeholders:

```bash
scripts/install-units.sh services/power-guard/power-guard.service
```

It installs the unit and reloads systemd, nothing more. Enabling and starting a unit is a separate, deliberate step, because these services drive real fans and power caps.

## Testing

Automated tests never touch live control. Mocks of the dashboard API (`/api/powertarget`, `/api/throttle`, `/api/state`), `ipmitool`, `nvidia-smi` and the RAPL sysfs files will sit next to the services they stand in for, switched by config.
