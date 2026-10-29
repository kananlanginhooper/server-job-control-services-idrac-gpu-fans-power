#!/bin/bash
# Install systemd units from this repo on the current server: renders @REPO@ (this clone) and @USER@ (whoever runs it),
# then daemon-reload. It only installs; enabling/starting is a separate, deliberate step (the live services run as they
# are until a planned cutover).   usage: scripts/install-units.sh services/fan-control/fan-control.service [...]
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
me=$(id -un)
for u in "$@"; do
  sed -e "s#@REPO@#$repo#g" -e "s#@USER@#$me#g" "$repo/$u" | sudo -n tee "/etc/systemd/system/$(basename "$u")" >/dev/null
  echo "installed $(basename "$u")"
done
sudo -n systemctl daemon-reload
