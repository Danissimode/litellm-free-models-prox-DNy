#!/usr/bin/env bash
# =============================================================================
# Aion Model Gateway — create per-agent virtual keys (BACKLOG / helper)
# =============================================================================
# Creates scoped virtual keys via the LiteLLM Management API so each agent
# (opencode, reviewer, AionUI) gets its own key with rpm/tpm + model limits,
# instead of everyone sharing LITELLM_MASTER_KEY.
#
# This is intentionally a manual helper (TZ §15: "prepare, but not necessarily
# fully automate"). Run once after first boot to mint keys; store the returned
# keys in your agents' env (NEVER commit them).
#
# Prereqs: gateway is up, LITELLM_MASTER_KEY set.
#
#   ./scripts/create-agent-keys.sh
# =============================================================================
set -uo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:4000}"
# shellcheck disable=SC1091
[[ -z "${LITELLM_MASTER_KEY:-}" && -f ./.env ]] && { set -a; . ./.env; set +a; }

if [[ -z "${LITELLM_MASTER_KEY:-}" ]]; then
  echo "FAIL: LITELLM_MASTER_KEY not set." >&2; exit 1
fi

create_key() {
  # $1 = label
  # $2 = allowed models, space-separated (e.g. "aion-programmer coder")
  # $3 = rpm, $4 = tpm, $5 = max_budget (USD) optional
  local label="$1" models="$2" rpm="$3" tpm="$4" budget="${5:-}"
  echo "→ creating $label (rpm=$rpm, tpm=$tpm)…"

  # Build the JSON payload with python to avoid fragile shell quoting. We pass
  # the fields as argv (not env) so there's no ambiguity: `env VAR=x cmd`
  # would also work, but argv is explicit and order-independent here.
  local payload
  payload=$(python3 -c '
import json, sys
label, models, rpm, tpm, budget = sys.argv[1:6]
doc = {
    "key_alias": label,
    "models": models.split(),
    "rpm": int(rpm),
    "tpm": int(tpm),
    "metadata": {"created_by": "create-agent-keys.sh"},
}
if budget:
    doc["max_budget"] = float(budget)
sys.stdout.write(json.dumps(doc))
' "$label" "$models" "$rpm" "$tpm" "$budget")


  curl -s -X POST "$BASE_URL/key/generate" \
    -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
    -H "Content-Type: application/json" \
    -d "$payload" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print("    (no JSON returned)"); sys.exit(0)
key = d.get("key") or d.get("token") or ""
if key:
    print("    key:      " + key)
    print("    expires:  " + str(d.get("expires", "n/a")))
    print("    (store in your agent env; never commit)")
else:
    print("    response: " + json.dumps(d)[:200])
'
}

echo "== Aion Model Gateway — virtual key creation =="
echo "  BASE_URL = $BASE_URL"
echo "  (Each key is printed ONCE. Store it securely.)"
echo

create_key "opencode-programmer" "aion-programmer coder"                       30 100000 ""
create_key "opencode-architect"  "aion-architect reasoning smart"               20 150000 ""
create_key "reviewer"            "aion-reviewer reasoning"                      20 120000 ""
create_key "aionui"              "smart fast coder reasoning vision aion-architect aion-programmer aion-reviewer aion-fast" 60 500000 ""

echo
echo "Done. To list/revoke keys later see:"
echo "  GET  $BASE_URL/key/list      (with master key auth)"
echo "  POST $BASE_URL/key/delete    {\"keys\":[...]}"
