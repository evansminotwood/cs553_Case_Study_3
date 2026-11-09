#!/bin/bash
# Runs on your laptop: ./deploy.sh [up|ssh|tunnel|<any run_stack.sh command>]
set -euo pipefail
cd "$(dirname "$0")"
source ./config.env

case "$VM_HOST" in *CHANGE_ME*) echo "Set VM_HOST in config.env first."; exit 1;; esac
[ -f "$VM_KEY" ] || { echo "SSH key $VM_KEY not found (set VM_KEY in config.env)."; exit 1; }

SSH_OPTS=(-i "$VM_KEY" -p "$VM_PORT" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
REMOTE="${VM_USER}@${VM_HOST}"
ssh_vm() { ssh "${SSH_OPTS[@]}" "$REMOTE" "$@"; }

case "${1:-up}" in
  ssh)
    exec ssh "${SSH_OPTS[@]}" "$REMOTE" ;;
  tunnel)
    echo "Grafana    -> http://localhost:3000/grafana/"
    echo "Prometheus -> http://localhost:9090   (Ctrl-C to stop)"
    exec ssh "${SSH_OPTS[@]}" -N \
      -L 3000:localhost:$((PORT_BASE + 7)) -L 9090:localhost:$((PORT_BASE + 6)) "$REMOTE" ;;
  up)
    echo "[deploy] copying project to ${REMOTE}:~/${VM_DIR}"
    ssh_vm "mkdir -p ~/${VM_DIR}"
    tar czf - --exclude=.git --exclude='__pycache__' \
        --exclude=venv --exclude=.venv --exclude=screenshots --exclude='*.pdf' --exclude=report.md --exclude=reports . \
      | ssh_vm "tar xzf - -C ~/${VM_DIR}"
    [ -f secrets.env ] && ssh_vm "chmod 600 ~/${VM_DIR}/secrets.env"
    ssh_vm "cd ~/${VM_DIR} && chmod +x run_stack.sh entrypoint.sh && ./run_stack.sh up" ;;
  *)
    ssh_vm "cd ~/${VM_DIR} && ./run_stack.sh $*" ;;
esac
