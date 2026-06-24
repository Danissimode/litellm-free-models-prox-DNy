#!/usr/bin/env bash
# =============================================================================
# Aion Model Gateway — security checks
# =============================================================================
# Verifies the gateway's auth + bind posture against a running instance.
# Covers TZ §12:
#   1. no auth header  -> expect 401/403
#   2. wrong key       -> expect 401/403
#   3. correct key     -> expect 200
#   4. bind address    -> expect 127.0.0.1 only (not *:4000 / 0.0.0.0)
#
# Usage:
#   ./scripts/security-check.sh
#   BASE_URL=http://127.0.0.1:4000 ./scripts/security-check.sh
# =============================================================================
set -uo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:4000}"

# shellcheck disable=SC1091
[[ -z "${LITELLM_MASTER_KEY:-}" && -f ./.env ]] && { set -a; . ./.env; set +a; }

if [[ -z "${LITELLM_MASTER_KEY:-}" ]]; then
  echo "FAIL: LITELLM_MASTER_KEY not set." >&2; exit 1
fi

PASS=0; FAIL=0

assert_status_in() {
  # $1 label, $2 actual code, $3.. space-sep expected codes
  local label="$1" actual="$2"; shift 2
  for exp in "$@"; do
    if [[ "$actual" == "$exp" ]]; then
      echo "  [PASS] $label -> $actual"; PASS=$((PASS+1)); return
    fi
  done
  echo "  [FAIL] $label -> $actual (expected one of: $*)"; FAIL=$((FAIL+1))
}

echo "== Aion Model Gateway security check =="
echo "  BASE_URL = $BASE_URL"
echo

# 1. No auth header
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$BASE_URL/v1/models")
assert_status_in "no auth header"      "$code" 401 403

# 2. Wrong key
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
  -H "Authorization: Bearer sk-invalid-key-1234567890" "$BASE_URL/v1/models")
assert_status_in "wrong key"           "$code" 401 403

# 3. Correct key
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" "$BASE_URL/v1/models")
assert_status_in "correct key"         "$code" 200
echo

# 4. Bind address — port 4000 must listen ONLY on 127.0.0.1 (loopback).
echo "  [4] bind address check"
listeners=$(lsof -nP -iTCP:4000 -sTCP:LISTEN 2>/dev/null || true)
if [[ -z "$listeners" ]]; then
  echo "  [WARN] lsof found no listener on :4000 (gateway not running locally?)"
  # Not a hard fail — the gateway may be on another host in CI.
else
  if echo "$listeners" | grep -qE '\*:4000|0\.0\.0\.0:4000|\[::\]:4000|\*:\*'; then
    echo "  [FAIL] port 4000 is bound to a wildcard address (open proxy risk):"
    echo "$listeners" | sed 's/^/        /'
    FAIL=$((FAIL+1))
  elif echo "$listeners" | grep -qE '127\.0\.0\.1:4000|\[::1\]:4000'; then
    echo "  [PASS] port 4000 bound to loopback only"
    PASS=$((PASS+1))
  else
    echo "  [WARN] port 4000 bound, but not to loopback/wildcard — review:"
    echo "$listeners" | sed 's/^/        /'
  fi
fi
echo

echo "== security result: PASS=$PASS FAIL=$FAIL =="
[[ "$FAIL" -eq 0 ]] || exit 1
exit 0
