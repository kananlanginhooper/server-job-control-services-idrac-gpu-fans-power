#!/usr/bin/env bash
# Run the task runner's end-to-end test in throwaway containers: Postgres 18 + Python 3.12 on a private Docker network.
# Nothing touches a server. Everything is removed afterwards.   Usage: scripts/taskrunner-e2e.sh
set -euo pipefail
cd "$(dirname "$0")/.."
net=taskrunner-e2e-$$
docker network create "$net" >/dev/null
trap 'docker rm -f "$net-db" >/dev/null 2>&1; docker network rm "$net" >/dev/null 2>&1' EXIT
docker run -d --rm --name "$net-db" --network "$net" -e POSTGRES_PASSWORD=test -e POSTGRES_DB=taskrunner postgres:18 >/dev/null
MSYS_NO_PATHCONV=1 docker run --rm --network "$net" -v "$PWD:/repo:ro" -w /repo \
  -e TASKRUNNER_TEST_DSN="host=$net-db dbname=taskrunner user=postgres password=test" \
  -e PYTHONDONTWRITEBYTECODE=1 python:3.12-slim bash -c '
    pip install -q --root-user-action=ignore "psycopg[binary]" pytest >/dev/null 2>&1
    for i in $(seq 30); do python -c "import psycopg,os; psycopg.connect(os.environ[\"TASKRUNNER_TEST_DSN\"])" 2>/dev/null && break; sleep 1; done
    python -m pytest -q -p no:cacheprovider -o addopts="" tests/test_taskrunner_e2e.py tests/test_taskrunner.py'
