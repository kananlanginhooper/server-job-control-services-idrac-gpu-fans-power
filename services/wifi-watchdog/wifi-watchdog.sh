#!/bin/bash
# GPU desktop Wi-Fi watchdog (2026-10-26): GPU desktop is headless and Wi-Fi only; the Intel 8265 firmware can crash
# and get stuck ("Failing on timeout while stopping DMA"). Runs every minute from wifi-watchdog.timer.
#   3 failed checks in a row  -> reload the Wi-Fi driver (rmmod/modprobe; `modprobe -r` trips Ubuntu's iwlwifi.conf hook)
#   3 reloads without success -> reboot, only after 20 min uptime and at most 2 reboots per 6 h
STATE=/run/wifi-watchdog; LOG=/var/lib/wifi-watchdog; mkdir -p $STATE $LOG
say() { logger -t wifi-watchdog "$*"; }
. "$(dirname "$(readlink -f "$0")")/../../lib/svcenv.sh"

# WIFI_GATEWAYS in the repo .env: "<connection name>=<gateway IP>" pairs, space-separated; the active one is pinged
GW=
for pair in $(svc_env WIFI_GATEWAYS); do
  nmcli -g NAME connection show --active 2>/dev/null | grep -qx "${pair%%=*}" && { GW=${pair#*=}; break; }
done
DEV=$(nmcli -t -f DEVICE,TYPE device 2>/dev/null | awk -F: '$2 == "wifi" {print $1; exit}')

if [ -n "$GW" ] && [ -n "$DEV" ] && ping -c 2 -W 3 -I "$DEV" "$GW" >/dev/null 2>&1; then
  [ -s $STATE/fails ] && [ "$(cat $STATE/fails)" != 0 ] && say "Wi-Fi back ($DEV via $GW)"
  echo 0 > $STATE/fails; echo 0 > $STATE/reloads; exit 0
fi

n=$(( $(cat $STATE/fails 2>/dev/null || echo 0) + 1 )); echo $n > $STATE/fails
say "check failed $n in a row (dev=${DEV:-none} gw=${GW:-no active Wi-Fi profile})"
[ $n -lt 3 ] && exit 0

r=$(( $(cat $STATE/reloads 2>/dev/null || echo 0) + 1 )); echo $r > $STATE/reloads; echo 0 > $STATE/fails
if [ $r -le 3 ]; then
  say "reloading the Wi-Fi driver (attempt $r)"
  rmmod iwlmvm iwlwifi 2>/dev/null; sleep 2; modprobe iwlwifi
  exit 0
fi

up=$(cut -d. -f1 /proc/uptime)
recent=$(awk -v now="$(date +%s)" 'now - $1 < 21600' $LOG/reboots 2>/dev/null | wc -l)
if [ "$up" -ge 1200 ] && [ "$recent" -lt 2 ]; then
  say "Wi-Fi still down after 3 driver reloads: rebooting"
  date +%s >> $LOG/reboots
  systemctl reboot
else
  say "Wi-Fi still down, not rebooting (uptime ${up}s, $recent reboots in 6 h); reloading again"
  echo 0 > $STATE/reloads
  rmmod iwlmvm iwlwifi 2>/dev/null; sleep 2; modprobe iwlwifi
fi
