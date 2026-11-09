#!/bin/bash
# Runs on the VM. Usage: ./run_stack.sh [up|down|restart|rebuild|status|urls|traffic api|local [n]|logs <name>]
set -euo pipefail
cd "$(dirname "$0")"

set -a
source ./config.env
[ -f ./secrets.env ] && source ./secrets.env
: "${HF_TOKEN:=}" "${NGROK_AUTHTOKEN:=}" "${GRAFANA_ADMIN_PASSWORD:=admin}" "${SHARED_NETWORK:=}"
P() { echo $((PORT_BASE + $1)); }
API_UI_PORT=$(P 0);   API_METRICS_PORT=$(P 1);   API_NODE_PORT=$(P 2)
LOCAL_UI_PORT=$(P 3); LOCAL_METRICS_PORT=$(P 4); LOCAL_NODE_PORT=$(P 5)
PROMETHEUS_PORT=$(P 6); GRAFANA_PORT=$(P 7); NGROK_INSPECT_PORT=$(P 8); GATEWAY_PORT=$(P 9)
COMPOSE_HTTP_TIMEOUT=${COMPOSE_HTTP_TIMEOUT:-600}
DOCKER_CLIENT_TIMEOUT=${DOCKER_CLIENT_TIMEOUT:-600}
set +a

if [ -z "${DOCKER:-}" ]; then
  if docker info >/dev/null 2>&1; then DOCKER=docker; else DOCKER="sudo -E docker"; fi
fi
# docker-compose v1 is skipped on purpose: with Docker 29's containerd image
# store it can't find images it just built ("No such image: sha256:...").
if [ -z "${COMPOSE:-}" ]; then
  if $DOCKER compose version >/dev/null 2>&1; then COMPOSE="$DOCKER compose"; else COMPOSE=""; fi
fi
NET="${GROUP}-net"
if [ "$TUNNEL" = "ngrok" ] && [ -z "$NGROK_AUTHTOKEN" ]; then
  echo "[warn] TUNNEL=ngrok but NGROK_AUTHTOKEN is empty in secrets.env — starting without public URLs."
  TUNNEL=none
fi

rm_container() { $DOCKER rm -f "$1" >/dev/null 2>&1 || true; }

join_shared_network() {
  [ -n "$SHARED_NETWORK" ] || return 0
  if $DOCKER network inspect "$SHARED_NETWORK" >/dev/null 2>&1; then
    for c in api local; do
      $DOCKER network connect "$SHARED_NETWORK" "${GROUP}-$c" 2>/dev/null \
        && echo "[up] ${GROUP}-$c joined shared network '$SHARED_NETWORK'" || true
    done
  else
    echo "[info] shared network '$SHARED_NETWORK' not found on this VM — skipped"
  fi
}

compose() { $COMPOSE -p "$GROUP" --profile "$TUNNEL" "$@"; }

plain_up() {
  $DOCKER network inspect "$NET" >/dev/null 2>&1 || $DOCKER network create "$NET" >/dev/null
  for prod in api local; do
    echo "[build] ${GROUP}-$prod:latest"
    $DOCKER build --build-arg PRODUCT=$prod -t "${GROUP}-$prod:latest" .
  done
  run() {  # name alias args...
    local name=$1 alias=$2; shift 2
    rm_container "$name"
    $DOCKER run -d --name "$name" --network "$NET" --network-alias "$alias" \
      --restart unless-stopped "$@" >/dev/null && echo "[up] $name"
  }
  run "${GROUP}-api" api -e HF_TOKEN="$HF_TOKEN" -e GRADIO_ROOT_PATH=/api \
    -p "$API_UI_PORT:7860" -p "$API_METRICS_PORT:8000" -p "$API_NODE_PORT:9100" "${GROUP}-api:latest"
  run "${GROUP}-local" local -e HF_TOKEN="$HF_TOKEN" -e GRADIO_ROOT_PATH=/local \
    -p "$LOCAL_UI_PORT:7860" -p "$LOCAL_METRICS_PORT:8000" -p "$LOCAL_NODE_PORT:9100" "${GROUP}-local:latest"
  run "${GROUP}-prometheus" prometheus \
    -v "$PWD/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro" \
    -v "${GROUP}_prometheus-data:/prometheus" -p "$PROMETHEUS_PORT:9090" \
    prom/prometheus:latest --config.file=/etc/prometheus/prometheus.yml \
    --storage.tsdb.path=/prometheus --storage.tsdb.retention.time=15d
  run "${GROUP}-grafana" grafana \
    -e PROMETHEUS_URL=http://prometheus:9090 \
    -e GF_SECURITY_ADMIN_PASSWORD="$GRAFANA_ADMIN_PASSWORD" \
    -e GF_AUTH_ANONYMOUS_ENABLED=true -e GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer \
    -e GF_USERS_ALLOW_SIGN_UP=false \
    -e GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH=/var/lib/grafana/dashboards/blackjack.json \
    -e 'GF_SERVER_ROOT_URL=%(protocol)s://%(domain)s:%(http_port)s/grafana/' \
    -e GF_SERVER_SERVE_FROM_SUB_PATH=true \
    -v "$PWD/grafana/provisioning:/etc/grafana/provisioning:ro" \
    -v "$PWD/grafana/dashboards:/var/lib/grafana/dashboards:ro" \
    -v "${GROUP}_grafana-data:/var/lib/grafana" -p "$GRAFANA_PORT:3000" grafana/grafana:latest
  run "${GROUP}-gateway" gateway \
    -v "$PWD/gateway/nginx.conf:/etc/nginx/conf.d/default.conf:ro" -p "$GATEWAY_PORT:80" \
    nginx:1.27-alpine
  if [ "$TUNNEL" = "none" ]; then
    :
  elif [ "$TUNNEL" = "ngrok" ]; then
    run "${GROUP}-tunnel" ngrok -e NGROK_AUTHTOKEN="$NGROK_AUTHTOKEN" \
      -v "$PWD/ngrok/ngrok.yml:/etc/ngrok.yml:ro" -p "127.0.0.1:$NGROK_INSPECT_PORT:4040" \
      ngrok/ngrok:latest start --all --config /etc/ngrok.yml
  else
    run "${GROUP}-tunnel" cloudflared \
      cloudflare/cloudflared:latest tunnel --no-autoupdate --url http://gateway:80
  fi
}

plain_down() {
  for c in api local prometheus grafana gateway tunnel; do
    rm_container "${GROUP}-$c"
  done
}

show_urls() {
  echo "== Public URLs ($TUNNEL)"
  [ "$TUNNEL" = "none" ] && { echo "  (no tunnel: add NGROK_AUTHTOKEN to secrets.env, or set TUNNEL=cloudflared)"; return 0; }
  local base=""
  if [ "$TUNNEL" = "ngrok" ]; then
    base=$(curl -fsS "http://127.0.0.1:$NGROK_INSPECT_PORT/api/tunnels" 2>/dev/null \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["tunnels"][0]["public_url"])' 2>/dev/null || true)
  else
    base=$($DOCKER logs "${GROUP}-tunnel" 2>&1 | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1 || true)
  fi
  if [ -z "$base" ]; then echo "  (tunnel not ready yet — check: ./run_stack.sh logs tunnel)"; return 0; fi
  printf "  %-8s %s/api/\n  %-8s %s/local/\n  %-8s %s/grafana/\n" api "$base" local "$base" grafana "$base"
}

show_status() {
  echo "== Mode: ${COMPOSE:-plain docker}"
  echo "== Containers"
  $DOCKER ps -a --filter "name=^${GROUP}-" --format '  {{.Names}}\t{{.Status}}\t{{.Ports}}'
  echo "== Container IPs (for the class Google Sheet)"
  for c in api local; do
    $DOCKER inspect "${GROUP}-$c" \
      --format '  {{.Name}}{{range $n, $v := .NetworkSettings.Networks}}  {{$n}}={{$v.IPAddress}}{{end}}' \
      2>/dev/null | sed 's#^  /#  #' || true
  done
  echo "== VM: $(hostname)  IP $(hostname -I 2>/dev/null | awk '{print $1}')  ports $(P 0)-$(P 9)"
  echo "== Quick checks"
  for c in "API UI:$API_UI_PORT/" "API metrics:$API_METRICS_PORT/metrics" "API node:$API_NODE_PORT/metrics" \
           "Local UI:$LOCAL_UI_PORT/" "Local metrics:$LOCAL_METRICS_PORT/metrics" "Local node:$LOCAL_NODE_PORT/metrics" \
           "Prometheus:$PROMETHEUS_PORT/-/ready" "Grafana:$GRAFANA_PORT/grafana/api/health" \
           "Gateway api:$GATEWAY_PORT/api/" "Gateway local:$GATEWAY_PORT/local/" \
           "Gateway grafana:$GATEWAY_PORT/grafana/api/health"; do
    label=${c%%:*}; path=${c#*:}
    if curl -fsS -o /dev/null -m 5 "http://localhost:${path}"; then s=OK; else s=--; fi
    printf "  %-16s %-4s http://localhost:%s\n" "$label" "$s" "$path"
  done
  show_urls
}

up() {
  if [ -n "$COMPOSE" ]; then
    echo "[up] using: $COMPOSE (profile: $TUNNEL)"
    compose up -d --build
  else
    echo "[up] docker compose not found — using plain docker run"
    plain_up
  fi
  join_shared_network
  echo "[up] waiting for services..."; sleep 10
  show_status
}

down() {
  if [ -n "$COMPOSE" ]; then compose down --remove-orphans; else plain_down; fi
  echo "[down] containers removed (Prometheus/Grafana data volumes kept)"
}

case "${1:-up}" in
  up)       up ;;
  down)     down ;;
  restart)  down; up ;;
  rebuild)  if [ -n "$COMPOSE" ]; then compose up -d --build api local; join_shared_network; else plain_up; join_shared_network; fi ;;
  status)   show_status ;;
  urls)     show_urls ;;
  traffic)  $DOCKER exec -i "${GROUP}-${2:?usage: traffic api|local [n]}" python - "${3:-10}" < scripts/generate_traffic.py ;;
  logs)     $DOCKER logs --tail 100 -f "${GROUP}-${2:?usage: logs api|local|prometheus|grafana|gateway|tunnel}" ;;
  *)        echo "usage: $0 [up|down|restart|rebuild|status|urls|traffic api|local [n]|logs <name>]"; exit 1 ;;
esac
