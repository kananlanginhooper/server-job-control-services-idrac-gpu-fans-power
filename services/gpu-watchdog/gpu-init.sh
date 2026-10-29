#!/bin/bash
# Boot: load the NVIDIA driver (blacklisted in grub on purpose, so a dead or unseated card can't block the boot),
# turn on persistence mode and set the power cap GPU_POWER_W from the repo .env (per host: <HOST>_GPU_POWER_W).
. "$(dirname "$(readlink -f "$0")")/../../lib/svcenv.sh"
set -e
modprobe nvidia
modprobe nvidia_uvm
nvidia-smi -pm 1
w=$(svc_env GPU_POWER_W)
[ -n "$w" ] && nvidia-smi -pl "$w"
