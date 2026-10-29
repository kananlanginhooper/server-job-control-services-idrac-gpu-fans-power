#!/bin/bash
# /dev/shm/power-hooks/copilot.sh on the GPU server (staged by deploy/GPU server-runner-stage.sh). Owner: Copilot AI Analysis job
# (<thread>). Called by the dash-agent as `copilot.sh down <profile>` (Low Power) or `copilot.sh up <profile>`
# (Adaptive/Unattended); contract in <doc> Idempotent and returns at once: it wakes the
# gpu-watcher (SIGUSR1), which reads /dev/shm/power-hooks/state.json and, on down, closes the running job gracefully
# (the call in progress is finished and saved, the run stays open) and unloads llama-server (VRAM free ~25 s later);
# on up, reloads the model from /dev/shm and resumes the queue. Without a watcher, down does the stop and unload itself.
# manages: copilot_history\.cli
# manages: \./llama-server --model
case "$1" in down|up) ;; *) echo "usage: $0 down|up <profile>"; exit 2 ;; esac
W=$(ps -eo pid=,args= | awk '$2 == "python3" && $3 == "gpu-watcher.py" {print $1}')
if [ -n "$W" ]; then
  kill -USR1 $W
  echo "$(date '+%F %T') copilot $1 ${2:-}: woke gpu-watcher $W"
elif [ "$1" = down ]; then
  for p in $(ps -eo pid=,args= | awk '$2 == "python3" && $4 == "copilot_history.cli" {print $1}'); do kill -TERM $p; done
  ( sleep 20
    for p in $(ps -eo pid=,args= | awk '$2 == "python3" && $4 == "copilot_history.cli" {print $1}'); do kill -KILL $p; done
    for p in $(ps -eo pid=,args= | awk '$2 == "./llama-server" && $3 == "--model" {print $1}'); do kill $p; done
  ) > /dev/null 2>&1 < /dev/null &
  echo "$(date '+%F %T') copilot down ${2:-}: no watcher; graceful stop sent, unload in 20 s"
else
  echo "$(date '+%F %T') copilot up ${2:-}: no watcher running; start /dev/shm/copilot-runner/gpu-watcher.py to resume"
fi
exit 0
