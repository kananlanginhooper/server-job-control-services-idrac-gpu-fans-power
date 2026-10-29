# Shared settings for the shell services: same ONE repo .env and lookup order as lib/svcenv.py
# (<HOST>_NAME, then NAME; process environment first, then .env). Usage: . lib/svcenv.sh; X=$(svc_env NAME default)
SVC_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SVC_ENV_FILE=${SERVICES_ENV_FILE:-$SVC_ROOT/.env}
SVC_HOST=$(hostname | cut -d. -f1 | tr '[:lower:]' '[:upper:]' | sed 's/[^A-Z0-9]/_/g')
svc_env() {
  local k v
  for k in "${SVC_HOST}_$1" "$1"; do
    v=${!k:-}
    [ -z "$v" ] && [ -r "$SVC_ENV_FILE" ] && v=$(grep -E "^$k=" "$SVC_ENV_FILE" | tail -1 | cut -d= -f2- | sed -E "s/^[\"']//; s/[\"']\$//")
    [ -n "$v" ] && { printf '%s\n' "$v"; return; }
  done
  printf '%s\n' "${2:-}"
}
