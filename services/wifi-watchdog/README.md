# Wi-Fi watchdog

**Runs on:** GPU desktop (root)

For a headless, Wi-Fi-only box. Every minute after 5 min of uptime it pings the gateway of the active connection. After 3 failures in a row it reloads the Wi-Fi driver (rmmod/modprobe by hand: `modprobe -r` trips Ubuntu's iwlwifi hook). After 3 reloads without success it reboots, only past 20 min of uptime and at most 2 reboots per 6 h.

`iwlwifi.conf.example`: the Intel 8265 options that kept its firmware from crashing (`/etc/modprobe.d/`). Remove it after a card swap.

| | |
|---|---|
| Reads | `nmcli`, ping. |
| Writes | `journalctl -t wifi-watchdog`; state in `/run/wifi-watchdog`. |
| Settings | `WIFI_GATEWAYS` (`<connection>=<gateway ip>` pairs). |

Settings come from the repo's one `.env` (see [`.env.example`](../../.env.example)). The unit file(s) here are templates: `scripts/install-units.sh` renders `@REPO@`/`@USER@` and installs them. It doesn't enable or start them.
