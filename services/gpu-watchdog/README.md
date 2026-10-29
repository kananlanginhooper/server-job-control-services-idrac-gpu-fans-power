# GPU init + watchdog

**Runs on:** GPU desktop (root)

- **`gpu-init`** (boot, oneshot): loads the NVIDIA driver, turns on persistence mode, sets the power cap `GPU_POWER_W`. The driver is blacklisted at boot on purpose, so a dead or unseated card can't block the boot. Ordered before Ollama.
- **`gpu-watchdog.timer`** (90 s after boot, then every 5 min). Card missing from PCI: rescan the bus, and if it's still missing, reboot. Card present but the driver down: re-run gpu-init, and if it's still down, reboot. At most 2 reboots in 6 h, so a card that's really gone can't cause a boot loop.

| | |
|---|---|
| Reads | `lspci`, `nvidia-smi -L`. |
| Writes | `journalctl -t gpu-watchdog`; reboot counter in `/var/lib/gpu-watchdog`. |
| Settings | `GPU_POWER_W`, `GPU_WATCHDOG_MATCH` (text `nvidia-smi -L` shows for the card). |

Settings come from the repo's one `.env` (see [`.env.example`](../../.env.example)). The unit file(s) here are templates: `scripts/install-units.sh` renders `@REPO@`/`@USER@` and installs them. It doesn't enable or start them.
