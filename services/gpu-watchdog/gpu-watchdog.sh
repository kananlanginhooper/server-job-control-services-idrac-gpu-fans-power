#!/bin/bash
# GPU desktop GPU watchdog (2026-10-27): the desktop GPU sits loosely in its slot and sometimes isn't detected at boot;
# a plain restart brings it back. Runs from gpu-watchdog.timer (90 s after boot, then every 5 min).
#   card missing from PCI      -> rescan the bus; still missing -> reboot
#   card present, driver down  -> restart gpu-init; still down  -> reboot
# Reboots are capped (MAX_REBOOTS in WINDOW_H hours) so a card that is really gone can't cause a boot loop.
# Log: journalctl -t gpu-watchdog
STATE=/var/lib/gpu-watchdog; mkdir -p $STATE
MAX_REBOOTS=2; WINDOW_H=6   # the owner: "try a restart or two before calling it failed"
log() { logger -t gpu-watchdog "$*"; echo "$*"; }
. "$(dirname "$(readlink -f "$0")")/../../lib/svcenv.sh"
GPU=$(svc_env GPU_WATCHDOG_MATCH "NVIDIA")   # text nvidia-smi -L shows for the watched card

card() { lspci -d 10de: | grep -q "VGA\|3D"; }
driver_ok() { timeout 20 nvidia-smi -L 2>/dev/null | grep -q "$GPU"; }

reboot_capped() {
  now=$(date +%s); cut=$((now - WINDOW_H * 3600))
  touch $STATE/reboots; awk -v c=$cut '$1 >= c' $STATE/reboots > $STATE/reboots.tmp; mv $STATE/reboots.tmp $STATE/reboots
  n=$(wc -l < $STATE/reboots)
  if [ "$n" -ge $MAX_REBOOTS ]; then
    log "FAILED: $1, and already $n reboots in the last ${WINDOW_H} h - no reboot, the card needs reseating"
    exit 1
  fi
  echo "$now" >> $STATE/reboots
  log "REBOOT ($((n + 1))/$MAX_REBOOTS in ${WINDOW_H} h): $1"
  sync; systemctl reboot
  exit 0
}

if ! card; then
  log "$GPU not on the PCI bus, rescanning"
  echo 1 > /sys/bus/pci/rescan; sleep 5
  card || reboot_capped "$GPU still missing after a PCI rescan"
  log "rescan found the card, loading the driver"
  systemctl restart gpu-init; sleep 5
fi

if ! driver_ok; then
  log "$GPU is on the bus but the driver isn't answering, restarting gpu-init"
  systemctl restart gpu-init; sleep 5
  driver_ok || reboot_capped "driver still not answering after restarting gpu-init"
  systemctl restart ollama   # ollama started without the GPU, so it would stay on CPU
  log "driver back after restarting gpu-init (ollama restarted)"
fi
exit 0
