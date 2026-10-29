#!/bin/bash
# Install systemd units from this repo on the current server: renders @REPO@ (this clone) and @USER@ (whoever runs it),
# backs up a unit file it replaces (<name>.pre-repo-<date>), then daemon-reload. It only installs; enabling, starting and
# restarting are separate, deliberate steps (these services drive real fans and power caps).
#   usage: scripts/install-units.sh <repo unit>[=<installed name>] ...
#   e.g.   scripts/install-units.sh services/dash-agent/dash-agent.service=gpu-dash-agent.service
#          (keeps a live unit's existing name, so the dashboard, hooks and other units that refer to it keep working)
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
me=$(id -un)
stamp=$(date +%Y%m%d-%H%M)
for arg in "$@"; do
  src=${arg%%=*}; name=$(basename "$src"); [ "$arg" != "$src" ] && name=${arg#*=}
  dst=/etc/systemd/system/$name
  [ -e "$dst" ] && sudo -n cp -a "$dst" "$dst.pre-repo-$stamp" && echo "backed up $dst -> $dst.pre-repo-$stamp"
  sed -e "s#@REPO@#$repo#g" -e "s#@USER@#$me#g" "$repo/$src" | sudo -n tee "$dst" >/dev/null
  echo "installed $name (from $src)"
done
sudo -n systemctl daemon-reload
