#!/bin/bash
# Install / update the run broker on the hub (run as <user> with passwordless sudo, from the hub's repo clone):
#   bash <clone>/services/run-broker/install.sh
# Uses the task runner's database (services/taskrunner/install.sh makes it); its own tables go in schema `broker`.
# Installs and enables the unit but does NOT start it: start it when the owner says go (systemctl start run-broker).
set -euo pipefail
src=$(cd "$(dirname "$0")" && pwd)
me=$(id -un)
python3 -c "import psycopg" 2>/dev/null || sudo -n apt-get install -y -q python3-psycopg >/dev/null
sudo -n install -d /opt/run-broker
sudo -n install -m 755 "$src/run_broker.py" /opt/run-broker/
sudo -n install -m 644 "$src/workq.py" /opt/run-broker/
sudo -n install -m 644 "$src/../../lib/svcenv.py" /opt/run-broker/
envfile=$(cd "$src/../.." && pwd)/.env
[ -f "$envfile" ] || echo "warning: $envfile missing; the broker has no hosts to place on" >&2
sed -e "s/^User=.*/User=$me/" -e "s|^Environment=SERVICES_ENV_FILE=.*|Environment=SERVICES_ENV_FILE=$envfile|" "$src/run-broker.service" | sudo -n tee /etc/systemd/system/run-broker.service >/dev/null
sudo -n systemctl daemon-reload
sudo -n systemctl enable run-broker >/dev/null 2>&1
systemctl is-active run-broker && sudo -n systemctl restart run-broker || echo "installed; not started"
