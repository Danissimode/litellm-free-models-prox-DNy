#!/usr/bin/env bash
# =============================================================================
# Aion Model Gateway — preflight env check
# =============================================================================
# Fails closed: the gateway will NOT start unless the security-relevant env
# vars are present and non-demo. Run manually or as the compose entrypoint:
#
#   ./scripts/preflight.sh          # manual, before `docker compose up`
#   PREFLIGHT=1                     # set by docker-compose.yml inside the
#                                   # litellm container entrypoint
#
# Exit codes: 0 = pass, 1 = hard fail (do not boot), 2 = warnings only.
# =============================================================================
set -euo pipefail

# --- locate .env -------------------------------------------------------------

# Inside the compose container we read env directly; locally we source .env.
if [[ -n "${LITELLM_MASTER_KEY:-}" ]]; then
  : # already exported (container env)
elif [[ -f ./.env ]]; then
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
elif [[ "${1:-}" == "--no-env-file" ]]; then
  : # caller took responsibility for exporting vars
fi

# --- helpers -----------------------------------------------------------------
fail() { echo "PREFLIGHT FAIL: $*" >&2; exit 1; }
warn() { echo "PREFLIGHT WARN: $*" >&2; }

# --- LITELLM_MASTER_KEY (REQUIRED) -------------------------------------------
if [[ -z "${LITELLM_MASTER_KEY:-}" ]]; then
  fail "LITELLM_MASTER_KEY is not set. Generate one: openssl rand -hex 32 | sed 's/^/sk-/'"
fi

KEY="$LITELLM_MASTER_KEY"

# Must start with sk-
if [[ "$KEY" != sk-* ]]; then
  fail "LITELLM_MASTER_KEY must start with 'sk-'."
fi

# Reasonable strength: sk- + 16+ chars of entropy
payload="${KEY#sk-}"
if [[ ${#payload} -lt 16 ]]; then
  fail "LITELLM_MASTER_KEY looks too short (need sk- + ≥16 chars). Got ${#payload} chars after the prefix."
fi

# Forbidden demo / placeholder values (lowercased compare). Use `tr` so this
# works on bash 3.2 (macOS default) which lacks the ${VAR,,} expansion.
lc="$(printf '%s' "$KEY" | tr '[:upper:]' '[:lower:]')"
for forbidden in \
    "sk-your-litellm-master-key" \
    "sk-local-aionui-master-change-me" \
    "sk-change-me" \
    "sk-litellm-your-master-key" \
    "sk-changeme" \
    "changeme" \
    "password" \
    "test" \
    "sk-demo" \
    "sk-example" \
    "sk-master-key"; do
  if [[ "$lc" == "$forbidden" ]]; then
    fail "LITELLM_MASTER_KEY is a known demo/placeholder value. Set a real secret."
  fi
done

# --- POSTGRES_PASSWORD (REQUIRED for self-contained compose) -----------------
if [[ -z "${POSTGRES_PASSWORD:-}" ]]; then
  # Only hard-fail when we're clearly running the local postgres service.
  if [[ "${PREFLIGHT:-}" == "1" ]]; then
    fail "POSTGRES_PASSWORD is not set; the local postgres service cannot boot safely."
  else
    warn "POSTGRES_PASSWORD is empty — local postgres will not boot. Set it in .env."
  fi
else
  pg_lc="$(printf '%s' "$POSTGRES_PASSWORD" | tr '[:upper:]' '[:lower:]')"
  if [[ "$pg_lc" == "changeme" || "$pg_lc" == "password" ]]; then
    fail "POSTGRES_PASSWORD is a known weak value. Set a real secret."
  fi
fi

# --- UI_PASSWORD (soft warning) ----------------------------------------------
if [[ -n "${UI_PASSWORD:-}" ]]; then
  ui_lc="$(printf '%s' "${UI_PASSWORD}" | tr '[:upper:]' '[:lower:]')"
  if [[ "$ui_lc" == "changeme" ]]; then
    warn "UI_PASSWORD is still 'changeme'. Change it before exposing the UI."
  fi
fi

# --- Remote-bind sanity (defense in depth) -----------------------------------
if [[ "${ENABLE_REMOTE_BIND:-}" == "true" ]]; then
  warn "ENABLE_REMOTE_BIND=true: LiteLLM may be reachable beyond 127.0.0.1. Ensure per-agent virtual keys exist (scripts/create-agent-keys.sh)."
fi

echo "PREFLIGHT OK: LITELLM_MASTER_KEY present and non-demo."
exit 0
