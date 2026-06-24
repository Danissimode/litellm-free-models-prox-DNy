#!/usr/bin/env bash
# =============================================================================
# Aion Model Gateway — provider connectivity checks
# =============================================================================
# Hits each provider's /models (or equivalent) endpoint directly with the
# configured key to classify reachability. Does NOT go through LiteLLM.
#
# Classification:
#   OK            -> 200 with a JSON model list
#   SKIPPED       -> env key not set (provider intentionally unused)
#   AUTH_ERROR    -> 401/403 (key invalid / revoked)
#   RATE_LIMITED  -> 429 (valid key, throttled — NOT fatal)
#   UNREACHABLE   -> network error / timeout (provider down or DNS fail)
#   SERVER_ERROR  -> 5xx
#
# Usage:  ./scripts/provider-check.sh
# =============================================================================
set -uo pipefail

# shellcheck disable=SC1091
[[ -f ./.env ]] && { set -a; . ./.env; set +a; }

TIMEOUT=15
OK=0; SKIPPED=0; AUTH=0; RATE=0; UNREACH=0; SERVER=0

probe() {
  # $1 = provider label
  # $2 = url
  # $3 = auth header value (e.g. "Authorization: Bearer $KEY" or "x-goog-api-key: $KEY")
  # $4 = env var name holding the key (to detect "not configured")
  # $5 = 'optional' to allow probing anonymously when the key is empty
  local label="$1" url="$2" hdr="$3" envvar="$4" optional="${5:-required}"
  local key="${!envvar:-}"

  if [[ -z "$key" ]]; then
    if [[ "$optional" == "optional" ]]; then
      : # probe anonymously below
    else
      echo "  [SKIPPED]  $label  ($envvar not set)"
      SKIPPED=$((SKIPPED+1)); return
    fi
  fi

  local code body
  if [[ -n "$key" ]]; then
    code=$(curl -s -o /tmp/aion_prov.json -w '%{http_code}' --max-time "$TIMEOUT" \
      -H "$hdr" "$url" 2>/dev/null || echo "000")
  else
    code=$(curl -s -o /tmp/aion_prov.json -w '%{http_code}' --max-time "$TIMEOUT" "$url" 2>/dev/null || echo "000")
  fi
  body="$(cat /tmp/aion_prov.json 2>/dev/null | head -c 300)"

  case "$code" in
    200)
      if echo "$body" | grep -qiE '"models"|"data"|"id"' ; then
        echo "  [OK]       $label  -> $code"; OK=$((OK+1))
      else
        echo "  [WARN]     $label  -> $code but body looks unusual: ${body:0:80}"
        OK=$((OK+1))
      fi
      ;;
    401|403)
      echo "  [AUTH]     $label  -> $code (invalid key)"; AUTH=$((AUTH+1)) ;;
    429)
      echo "  [RATE]     $label  -> 429 (rate limited, not fatal)"; RATE=$((RATE+1)) ;;
    000)
      echo "  [UNREACH]  $label  -> timeout/network error"; UNREACH=$((UNREACH+1)) ;;
    5*)
      echo "  [5xx]      $label  -> $code"; SERVER=$((SERVER+1)) ;;
    *)
      echo "  [?]        $label  -> $code  ${body:0:60}"; SERVER=$((SERVER+1)) ;;
  esac
}

echo "== Aion Model Gateway provider checks =="
echo

probe OpenRouter   "https://openrouter.ai/api/v1/models"          "Authorization: Bearer $OPENROUTER_API_KEY"   OPENROUTER_API_KEY
probe Groq         "https://api.groq.com/openai/v1/models"        "Authorization: Bearer $GROQ_API_KEY"         GROQ_API_KEY
probe Cerebras     "https://api.cerebras.ai/v1/models"            "Authorization: Bearer $CEREBRAS_API_KEY"     CEREBRAS_API_KEY
probe SambaNova    "https://api.sambanova.ai/v1/models"           "Authorization: Bearer $SAMBANOVA_API_KEY"    SAMBANOVA_API_KEY
probe "NVIDIA NIM" "https://integrate.api.nvidia.com/v1/models"   "Authorization: Bearer $NVIDIA_NIM_API_KEY"   NVIDIA_NIM_API_KEY
probe Cohere       "https://api.cohere.com/v2/models"             "Authorization: Bearer $COHERE_API_KEY"       COHERE_API_KEY
probe HuggingFace  "https://router.huggingface.co/v1/models"      "Authorization: Bearer $HF_TOKEN"             HF_TOKEN
probe Mistral      "https://api.mistral.ai/v1/models"             "Authorization: Bearer $MISTRAL_API_KEY"      MISTRAL_API_KEY
probe "GitHub"     "https://models.inference.ai.azure.com/models" "Authorization: Bearer $GH_MODELS_TOKEN"      GH_MODELS_TOKEN
probe Together     "https://api.together.ai/v1/models"            "Authorization: Bearer $TOGETHER_API_KEY"     TOGETHER_API_KEY

# Gemini uses x-goog-api-key
if [[ -n "${GEMINI_API_KEY:-}" ]]; then
  probe Gemini      "https://generativelanguage.googleapis.com/v1beta/models" "x-goog-api-key: $GEMINI_API_KEY" GEMINI_API_KEY
else
  echo "  [SKIPPED]  Gemini  (GEMINI_API_KEY not set)"; SKIPPED=$((SKIPPED+1))
fi

# Cloudflare needs an account id
if [[ -n "${CLOUDFLARE_API_KEY:-}" && -n "${CLOUDFLARE_ACCOUNT_ID:-}" ]]; then
  probe Cloudflare "https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/ai/models/search?per_page=5" \
                   "Authorization: Bearer $CLOUDFLARE_API_KEY" CLOUDFLARE_API_KEY
else
  echo "  [SKIPPED]  Cloudflare (CLOUDFLARE_API_KEY/ACCOUNT_ID not set)"; SKIPPED=$((SKIPPED+1))
fi

probe Pollinations  "https://gen.pollinations.ai/v1/models"        ""                                            POLLINATIONS_API_KEY optional
probe Kluster       "https://api.kluster.ai/v1/models"            "Authorization: Bearer $KLUSTER_API_KEY"       KLUSTER_API_KEY
probe LLM7          "https://api.llm7.io/v1/models"               ""                                            LLM7_API_KEY         optional
probe "Z.ai"        "https://open.bigmodel.cn/api/paas/v4/models" "Authorization: Bearer $ZAI_API_KEY"           ZAI_API_KEY
probe Perplexity    "https://api.perplexity.ai/models"             "Authorization: Bearer $PERPLEXITY_API_KEY"    PERPLEXITY_API_KEY
probe Codestral     "https://codestral.mistral.ai/v1/models"      "Authorization: Bearer $CODESTRAL_API_KEY"     CODESTRAL_API_KEY
probe PublicAI      "https://api.publicai.co/v1/models"           "Authorization: Bearer $PUBLICAI_API_KEY"      PUBLICAI_API_KEY

echo
echo "== provider result: OK=$OK SKIPPED=$SKIPPED AUTH=$AUTH RATE=$RATE UNREACHABLE=$UNREACH SERVER=$SERVER =="
rm -f /tmp/aion_prov.json
# AUTH errors are actionable (fix the key); everything else is informational.
[[ "$AUTH" -eq 0 ]] || exit 2
exit 0
