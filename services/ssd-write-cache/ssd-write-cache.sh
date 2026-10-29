#!/bin/bash
# Enable the drive write cache on the hub data SSDs, matched by serial (sdX names can change).
# Serials come from SSD_WRITE_CACHE_SERIALS in the repo .env (space-separated; leave out the OS disk and any drive that
# misbehaves with the cache on: on 2026-09-24 one never-trimmed SSD stalled, reset and lost cached writes).
. "$(dirname "$(readlink -f "$0")")/../../lib/svcenv.sh"
for s in $(svc_env SSD_WRITE_CACHE_SERIALS); do
  d=$(lsblk -dno NAME,SERIAL | awk -v s=$s "\$2==s{print \$1}")
  [ -z "$d" ] && { echo "serial $s not present"; continue; }
  sdparm -q --set WCE=1 /dev/$d && echo 1 > /sys/block/$d/device/rescan
  echo "$s ($d): $(cat /sys/block/$d/queue/write_cache)"
done
