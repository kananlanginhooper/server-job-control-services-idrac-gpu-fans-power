#!/bin/bash
# Install / update the task runner template on this server (run as <user> with passwordless sudo; same on hub, GPU server, GPU desktop).
# Deploy from the laptop:  scp -r services/taskrunner <host>:/tmp/ && ssh <host> 'bash /tmp/taskrunner/install.sh'
set -euo pipefail
src=$(cd "$(dirname "$0")" && pwd)
me=$(id -un)   # the runner's OS user (the unit's User=)
export DEBIAN_FRONTEND=noninteractive
pkgs=""
command -v psql >/dev/null || pkgs="$pkgs postgresql"
python3 -c "import psycopg" 2>/dev/null || pkgs="$pkgs python3-psycopg"
[ -n "$pkgs" ] && sudo -n apt-get install -y -q $pkgs >/dev/null
sudo -n systemctl enable --now postgresql >/dev/null 2>&1 || true
# role + database for the runner's OS user (peer auth over the local socket: no password anywhere)
sudo -n -u postgres psql -qAt -c "select 1 from pg_roles where rolname='$me'" | grep -q 1 || sudo -n -u postgres createuser "$me"
sudo -n -u postgres psql -qAt -c "select 1 from pg_database where datname='taskrunner'" | grep -q 1 || sudo -n -u postgres createdb -O "$me" taskrunner
sudo -n install -d /opt/taskrunner
sudo -n install -m 755 "$src/taskrunner.py" "$src/taskctl" /opt/taskrunner/
sudo -n ln -sf /opt/taskrunner/taskctl /usr/local/bin/taskctl
sed "s/^User=.*/User=$me/" "$src/taskrunner.service" | sudo -n tee /etc/systemd/system/taskrunner.service >/dev/null
mkdir -p ~/taskrunner/logs
sudo -n systemctl daemon-reload
sudo -n systemctl enable taskrunner >/dev/null 2>&1
sudo -n systemctl restart taskrunner
sleep 3
systemctl is-active taskrunner
