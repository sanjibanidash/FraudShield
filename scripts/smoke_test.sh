#!/usr/bin/env bash
# End-to-end smoke test of the running stack (used by CI, also handy locally: make smoke)
#  1. API healthy + prediction works
#  2. Kafka -> consumer -> API -> Postgres pipeline is flowing
#  3. Prometheus scrapes every service, Grafana is up
#  4. Drift monitor runs; injecting drift fires DataDriftDetected -> Alertmanager -> dashboard
#  5. Retraining produces a promoted/rejected challenger
set -euo pipefail
API=${API:-http://localhost:8000}
DASH=${DASH:-http://localhost:5000}
PROM=${PROM:-http://localhost:9090}
GRAFANA=${GRAFANA:-http://localhost:3000}

pass() { echo "  ✅ $*"; }
fail() { echo "  ❌ $*"; exit 1; }
json() { python3 -c "import sys,json; d=json.load(sys.stdin); print(eval(sys.argv[1]))" "$1"; }
wait_for() {  # wait_for "<description>" <timeout_s> <command...>
  local desc=$1 timeout=$2; shift 2
  local start=$SECONDS
  until "$@" >/dev/null 2>&1; do
    (( SECONDS - start > timeout )) && fail "timeout waiting for: $desc"
    sleep 5
  done
  pass "$desc ($((SECONDS - start))s)"
}
prom_value() { curl -fsS "$PROM/api/v1/query" --data-urlencode "query=$1" | json "d['data']['result'][0]['value'][1] if d['data']['result'] else ''"; }

echo "1) API"
wait_for "API /health" 300 curl -fsS "$API/health"
body='{"step":410,"type":"TRANSFER","amount":181000,"nameOrig":"C1","oldbalanceOrg":181000,"newbalanceOrig":0,"nameDest":"C2","oldbalanceDest":0,"newbalanceDest":0}'
p=$(curl -fsS -X POST "$API/predict" -H 'content-type: application/json' -d "$body" | json "d['fraud_probability']")
pass "POST /predict -> fraud_probability=$p"

echo "2) Streaming pipeline"
scored() { [ "$(curl -fsS "$DASH/api/stats" | json "d['total']")" -gt 200 ]; }
wait_for "200+ transactions streamed through Kafka and stored" 300 scored
echo "   stats: $(curl -fsS "$DASH/api/stats")"

echo "3) Observability"
all_up() { [ "$(curl -fsS "$PROM/api/v1/targets" | json "sum(t['health']!='up' for t in d['data']['activeTargets'])")" = "0" ]; }
wait_for "all Prometheus targets up" 180 all_up
wait_for "Grafana healthy" 120 curl -fsS "$GRAFANA/api/health"

echo "4) Drift monitoring + alerting"
monitor_ran() { [ -n "$(prom_value fraudshield_drift_share)" ]; }
wait_for "drift monitor published metrics" 300 monitor_ran
curl -fsS -o /dev/null -X POST "$DASH/controls" -d action=drift -d mode=amount_inflation
pass "injected drift: amount_inflation"
drift_on() { [ "$(prom_value fraudshield_dataset_drift)" = "1" ]; }
wait_for "dataset drift detected" 300 drift_on
alert_in_dash() { curl -fsS "$DASH/alerts" | grep -q DataDriftDetected; }
wait_for "DataDriftDetected alert delivered to dashboard" 300 alert_in_dash
curl -fsS -o /dev/null -X POST "$DASH/controls" -d action=drift -d mode=none

echo "5) Retraining (champion vs challenger)"
docker compose exec -T batch python /app/service/batch_job.py --retrain >/dev/null
curl -fsS "$DASH/models" | grep -Eq ">(promoted|rejected)<" || fail "no retraining event recorded"
pass "retraining event recorded: $(curl -fsS "$DASH/models" | grep -Eo '>(promoted|rejected)<' | head -1 | tr -d '<>')"

echo
echo "🎉 All smoke tests passed"
