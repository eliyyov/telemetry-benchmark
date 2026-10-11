#!/usr/bin/env bash
# setup.sh - one-time setup, run on VM-Mon:  cd ~/bench && ./setup.sh
# Checks both VMs, installs the monitoring stack on VM-Mon, the cAdvisor stack and the
# agent images on VM-Data, and smoke-tests every link. Idempotent: safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

dk() { docker "$@" 2>/dev/null || sg docker -c "docker $*"; }
ok()  { echo "  [ok] $*"; }
bad() { echo "  [FAIL] $*"; exit 1; }

echo "== 1/6 VM-Mon local checks =="
command -v docker >/dev/null || bad "docker not installed (cloud-init may still be running)"
dk info >/dev/null 2>&1 || bad "docker daemon not reachable"
dk compose version >/dev/null 2>&1 || bad "docker compose v2 not available"
[ -f "$HOME/.bench_env" ] || bad "$HOME/.bench_env missing (cloud-init created it; is this VM-Mon?)"
# shellcheck disable=SC1091
source "$HOME/.bench_env"
[ -n "${DATA_IP:-}" ] || bad "DATA_IP not set in ~/.bench_env"
ok "docker + compose, DATA_IP=$DATA_IP"

echo "== 2/6 SSH to VM-Data =="
ssh -o BatchMode=yes -o ConnectTimeout=10 data 'echo connected as $(whoami)@$(hostname)' >/dev/null \
  || bad "cannot ssh to VM-Data via the 'data' alias"
ssh -o BatchMode=yes data 'docker info >/dev/null 2>&1 || sg docker -c "docker info" >/dev/null' \
  || bad "docker not working on VM-Data"
ok "VM-Data reachable, docker working"

echo "== 3/6 Prometheus + Grafana on VM-Mon =="
sed "s/DATA_IP/$DATA_IP/" configs/prometheus.yml.tpl > configs/prometheus.yml
grep -q "$DATA_IP" configs/prometheus.yml || bad "prometheus.yml generation failed"
if [ ! -s "$HOME/.grafana_admin_password" ]; then
  ( umask 077; openssl rand -hex 20 > "$HOME/.grafana_admin_password" )
  echo "  Grafana admin password generated -> ~/.grafana_admin_password"
fi
dk compose -f compose.monitoring.yml up -d
for i in $(seq 1 30); do
  curl -sf http://127.0.0.1:9090/-/ready >/dev/null && break; sleep 2
done
curl -sf http://127.0.0.1:9090/-/ready >/dev/null || bad "Prometheus not ready"
curl -sf http://127.0.0.1:3000/api/health >/dev/null || bad "Grafana not healthy"
ok "Prometheus :9090 and Grafana :3000 up (localhost only - use the SSH tunnel)"

echo "== 4/6 Bench bundle + agent images on VM-Data =="
rsync -a --exclude logs --exclude results --exclude raw_metrics --exclude __pycache__ \
  ./ "data:bench/"
ssh -o BatchMode=yes data 'mkdir -p bench/logs'
ssh -o BatchMode=yes data 'for i in timberio/vector:0.59.0-debian otel/opentelemetry-collector-contrib:0.162.0 ghcr.io/google/cadvisor:v0.60.6; do docker pull -q "$i" >/dev/null; done'
ok "bundle synced, images pulled (Vector 0.59.0-debian, OTel 0.162.0, cAdvisor 0.60.6)"

echo "== 5/6 cAdvisor on VM-Data (core 1) =="
ssh -o BatchMode=yes data 'cd bench && (docker compose -f compose.cadvisor.yml up -d >/dev/null 2>&1 || sg docker -c "docker compose -f compose.cadvisor.yml up -d" >/dev/null)'
for i in $(seq 1 30); do
  curl -sf "http://$DATA_IP:8080/metrics" >/dev/null 2>&1 && break; sleep 2
done
curl -sf "http://$DATA_IP:8080/metrics" | grep -q container_cpu_usage_seconds_total \
  || bad "cAdvisor not exposing container metrics on $DATA_IP:8080"
ok "cAdvisor scraping from VM-Mon"

echo "== 6/6 Prometheus targets =="
sleep 16
targets=$(curl -sf 'http://127.0.0.1:9090/api/v1/targets?state=active' \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print(",".join(sorted(t["labels"]["job"]+": "+t["health"] for t in d["data"]["activeTargets"])))')
echo "  targets: $targets"
echo "$targets" | grep -q "cadvisor: up" || echo "  [warn] cadvisor target not up yet (check /targets in the UI)"

echo
echo "Setup complete. Next:"
echo "  ./run.sh                                  # interactive menu (smoke run first)"
echo "  ssh -L 3000:127.0.0.1:3000 \$(hostname -I | awk '{print \$1}')  # Grafana tunnel is in 'terraform output grafana_tunnel'"
echo "  Grafana: http://localhost:3000  admin / \$(cat ~/.grafana_admin_password)"
