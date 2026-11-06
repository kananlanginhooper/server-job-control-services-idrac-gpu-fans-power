#!/bin/bash
# Install / update the metrics recorder on the hub (run as <user> with passwordless sudo, from the hub's repo clone):
#   bash <clone>/services/metrics-recorder/install.sh
# Creates the Postgres database server_metrics (owned by <user>, peer auth over the socket), installs and (re)starts the unit.
set -euo pipefail
src=$(cd "$(dirname "$0")" && pwd)
me=$(id -un)
python3 -c "import psycopg" 2>/dev/null || sudo -n apt-get install -y -q python3-psycopg >/dev/null
python3 -c "import paho.mqtt" 2>/dev/null || sudo -n apt-get install -y -q python3-paho-mqtt >/dev/null
sudo -n -u postgres psql -qAt -c "select 1 from pg_database where datname='server_metrics'" | grep -q 1 || sudo -n -u postgres createdb -O "$me" server_metrics
sudo -n install -d /opt/metrics-recorder
sudo -n install -m 755 "$src/metrics_recorder.py" /opt/metrics-recorder/
sudo -n install -m 644 "$src/metrics-rules.json" "$src/../../lib/svcenv.py" /opt/metrics-recorder/
envfile=$(cd "$src/../.." && pwd)/.env
sed -e "s/^User=.*/User=$me/" -e "s|^Environment=SERVICES_ENV_FILE=.*|Environment=SERVICES_ENV_FILE=$envfile|" "$src/metrics-recorder.service" | sudo -n tee /etc/systemd/system/metrics-recorder.service >/dev/null
sudo -n systemctl daemon-reload
sudo -n systemctl enable metrics-recorder >/dev/null 2>&1
sudo -n systemctl restart metrics-recorder
sleep 3
systemctl is-active metrics-recorder
