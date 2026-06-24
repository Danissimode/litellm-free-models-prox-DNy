#!/usr/bin/env bash
# =============================================================================
# Aion Model Gateway — smoke tests
# =============================================================================
# Verifies the gateway responds correctly for each routing group + aion alias.
# Expects a running gateway at http://127.0.0.1:4000.
#
# Auth is required: set LITELLM_MASTER_KEY in your env or ./.env before running.
#
# Usage:
#   ./scripts/smoke-test.sh
#   BASE_URL=http://127.0.0.1:4000 MAX_LATENCY=90 ./scripts/smoke-test.sh
#
# Exit 0 = all required checks passed; 1 = at least one hard failure.
# Models that have no provider key configured are reported as SKIPPED, not
# failures — a provider-less local install is still a valid gateway.
# =============================================================================
set -uo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:4000}"
MAX_LATENCY="${MAX_LATENCY:-90}"

# shellcheck disable=SC1091
[[ -z "${LITELLM_MASTER_KEY:-}" && -f ./.env ]] && { set -a; . ./.env; set +a; }

if [[ -z "${LITELLM_MASTER_KEY:-}" ]]; then
  echo "FAIL: LITELLM_MASTER_KEY not set (env or ./.env)." >&2
  exit 1
fi

PASS=0; FAIL=0; SKIP=0
PROBE_PROMPT='Reply with the single word: ok'

check_health() {
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$BASE_URL/health")
  if [[ "$code" == "200" ]]; then
    echo "  [PASS] GET /health -> 200"; PASS=$((PASS+1))
  else
    echo "  [FAIL] GET /health -> $code (expected 200)"; FAIL=$((FAIL+1))
  fi
}

check_models_list() {
  local code body
  code=$(curl -s -o /tmp/aion_smoke_models.json -w '%{http_code}' --max-time 15 \
    -H "Authorization: Bearer $LITELLM_MASTER_KEY" "$BASE_URL/v1/models")
  if [[ "$code" != "200" ]]; then
    echo "  [FAIL] GET /v1/models -> $code"; FAIL=$((FAIL+1)); return
  fi
  # Ensure no plaintext provider key leaked into the response.
  if grep -qiE 'sk-[a-z0-9]{20,}|sk-or-v1-|gsk_[a-z0-9]{20,}|AIza[a-z0-9]{20,}' /tmp/aion_smoke_models.json; then
    echo "  [FAIL] GET /v1/models response contains a plaintext API key"; FAIL=$((FAIL+1)); return
  fi
  echo "  [PASS] GET /v1/models -> 200, no key leak"; PASS=$((PASS+1))
}

chat_probe() {
  # $1 = model name (routing group / aion alias)
  local model="$1"
  local t0 t1 body code latency
  t0=$(date +%s)
  code=$(curl -s -o /tmp/aion_smoke_chat.json -w '%{http_code}' --max-time 120 \
    -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
    -H "Content-Type: application/json" \
    -X POST "$BASE_URL/v1/chat/completions" \
    -d "{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"$PROBE_PROMPT\"}],\"max_tokens\":5}")
  t1=$(date +%s); latency=$((t1 - t0))

  # Treat provider-less / no-credentials responses as SKIP, not FAIL.
  if echo /tmp/aion_smoke_chat.json | grep -qiE 'no available deployments|No matching|no models|Authentication|invalid_api_key|missing credentials'; then
    echo "  [SKIP] $model -> no provider credentials configured ($code)"; SKIP=$((SKIP+1)); return
  fi
  if [[ "$code" == "401" || "$code" == "403" ]]; then
    echo "  [FAIL] $model -> $code (auth)"; FAIL=$((FAIL+1)); return
  fi
  if [[ "$code" != "200" ]]; then
    echo "  [FAIL] $model -> HTTP $code"; FAIL=$((FAIL+1)); return
  fi
  # Must contain a choices array.
  if ! grep -q '"choices"' /tmp/aion_smoke_chat.json; then
    echo "  [FAIL] $model -> 200 but no choices array"; FAIL=$((FAIL+1)); return
  fi
  # Latency guard.
  if (( latency > MAX_LATENCY )); then
    echo "  [WARN] $model -> ok but slow (${latency}s > ${MAX_LATENCY}s)"; PASS=$((PASS+1)); return
  fi
  echo "  [PASS] $model -> ok (${latency}s)"; PASS=$((PASS+1))
}

echo "== Aion Model Gateway smoke test =="
echo "  BASE_URL      = $BASE_URL"
echo "  MAX_LATENCY   = ${MAX_LATENCY}s"
echo

echo "[1/3] health + models"
check_health
check_models_list
echo

echo "[2/3] routing groups"
for m in fast smart reasoning coder; do chat_probe "$m"; done
echo

echo "[3/3] aion role aliases"
for m in aion-architect aion-programmer aion-reviewer aion-fast; do chat_probe "$m"; done
echo

echo "== smoke result: PASS=$PASS FAIL=$FAIL SKIP=$SKIP =="
rm -f /tmp/aion_smoke_models.json /tmp/aion_smoke_chat.json
[[ "$FAIL" -eq 0 ]] || exit 1
exit 0
