#!/usr/bin/env python3
"""
LiteLLM model auto-sync.

Queries each configured provider for available (free) models and registers
any new ones via LiteLLM's management API.

Sources:
  - Provider /models APIs (primary)
  - cheahjs/free-llm-api-resources README (cross-reference for providers
    whose API does not expose pricing info)

Does NOT touch routing groups (smart/fast/etc.) — only adds named routes
like or/llama-3.3-70b, groq/llama-3.3-70b-versatile, etc.
"""

import argparse
import concurrent.futures
import datetime
import json
import logging
import os
import pathlib
import re
import sys
import time
import urllib.request

from common import _is_free, _opener

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

LITELLM_BASE = os.environ.get("LITELLM_BASE_URL", "http://litellm:4000")
LITELLM_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
SYNC_INTERVAL_H = int(os.environ.get("SYNC_INTERVAL_HOURS", "24"))
STARTUP_DELAY_S = int(os.environ.get("STARTUP_DELAY_SECONDS", "60"))
CLEANUP_STALE = os.environ.get("CLEANUP_STALE_MODELS", "").lower() in (
    "1",
    "true",
    "yes",
)
# FREE_ONLY=true (default): only register models the provider marks free or
# that are explicitly allowlisted (see config/provider-policy.yaml).
FREE_ONLY = os.environ.get("FREE_ONLY", "true").lower() in ("1", "true", "yes")

CONFIG_DIR = pathlib.Path(os.environ.get("CONFIG_DIR", "/app/config"))
SNAPSHOT_DIR = pathlib.Path(os.environ.get("SNAPSHOT_DIR", "/app/snapshots"))

# Community-maintained list of free LLM APIs (auto-generated, updated frequently).
CHEAHJS_README_URL = (
    "https://raw.githubusercontent.com/cheahjs/free-llm-api-resources"
    "/refs/heads/main/README.md"
)

# Manual routing groups that auto-sync must NEVER create or delete via the
# Management API. These live in config.yaml and are curated by hand.
PROTECTED_GROUP_NAMES = frozenset(
    {
        "smart",
        "fast",
        "reasoning",
        "coder",
        "long",
        "vision",
        "aion-architect",
        "aion-programmer",
        "aion-reviewer",
        "aion-fast",
    }
)

# Name prefixes that indicate a model is a routing group rather than a
# discovered route. sync never adds/deletes these.
GROUP_NAME_PREFIXES = (
    "smart",
    "fast",
    "reasoning",
    "coder",
    "long",
    "vision",
    "aion-",
    "inbox-zero",
)

# ── HTTP helpers (stdlib only) ────────────────────────────────────────────────


_HEADERS = {"User-Agent": "litellm-free-models-proxy/1.0"}


def _http_get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={**_HEADERS, **(headers or {})})
    with _opener.open(req, timeout=timeout) as r:
        return r.read().decode()


def _json_get(url, headers=None, timeout=20):
    return json.loads(_http_get(url, headers, timeout))


def _post_litellm(path, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{LITELLM_BASE}{path}",
        data=data,
        headers={
            "Authorization": f"Bearer {LITELLM_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    max_retries = 4
    for attempt in range(max_retries):
        try:
            with _opener.open(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if attempt == max_retries - 1:
                raise e
            time.sleep(2**attempt)


def _get_litellm(path):
    return _json_get(
        f"{LITELLM_BASE}{path}",
        headers={"Authorization": f"Bearer {LITELLM_KEY}"},
    )


# ── Policy: allowlist / denylist / free-only ─────────────────────────────────
# config/model-allowlist.yaml, model-denylist.yaml, provider-policy.yaml are
# intentionally simple (lists of regex / string scalars under one key). We parse
# them with a tiny hand-rolled reader so sync stays dependency-free and runs in
# the stock python:3.12-slim image.


def _load_policy_list(path, key):
    """Read a single-key list-of-scalars YAML file. Returns a list of strings."""
    if not path.exists():
        return []
    items = []
    in_section = False
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if not line.startswith(" ") and not line.startswith("\t") and stripped.endswith(":"):
            in_section = stripped[:-1] == key
            continue
        if in_section and stripped.startswith("- "):
            val = stripped[2:].strip().strip('"').strip("'")
            if val:
                items.append(val)
    return items


def _compile(patterns):
    return [re.compile(p) for p in patterns]


_ALLOW = _compile(_load_policy_list(CONFIG_DIR / "model-allowlist.yaml", "allow"))
_DENY = _compile(_load_policy_list(CONFIG_DIR / "model-denylist.yaml", "deny"))
_PAID_PROVIDERS = set(
    _load_policy_list(CONFIG_DIR / "provider-policy.yaml", "paid_providers")
)


def _matches_any(model_str, compiled_patterns):
    return any(p.search(model_str) for p in compiled_patterns)


def admission_decision(litellm_model, provider_paid, free_marker):
    """
    Decide whether a discovered model may be registered.

    Returns (admit: bool, reason: str).
      litellm_model: e.g. 'openrouter/x:free' or 'openai/gpt-4'
      provider_paid: True if this provider is in provider-policy.paid_providers
      free_marker:   True if the fetcher already verified the model is free
                     (pricing==0, ':free' suffix, curated free-tier list, etc.)
    """
    # Deny always wins.
    if _matches_any(litellm_model, _DENY):
        return False, "denylisted"
    # Explicit allowlist admits regardless of ambiguity.
    if _matches_any(litellm_model, _ALLOW):
        return True, "allowlisted"
    # FREE_ONLY policy:
    if FREE_ONLY:
        if provider_paid and not free_marker:
            return False, "paid provider, not marked free"
        if not free_marker:
            # Pricing unknown / ambiguous and not allowlisted → quarantine,
            # do NOT silently register (would risk routing to paid models).
            return False, "ambiguous pricing (quarantined)"
        return True, "free-confirmed"
    # FREE_ONLY=false: operator opted into broader admission; denylist still
    # applies above.
    return True, "free-only disabled"


def is_protected_route_name(model_name):
    """True for names that belong to manual routing groups (never sync these)."""
    return model_name in PROTECTED_GROUP_NAMES or model_name.startswith(
        GROUP_NAME_PREFIXES
    )


# ── Snapshots ────────────────────────────────────────────────────────────────


def snapshot_existing_models(existing):
    """Persist a JSON snapshot of current LiteLLM model entries before mutating.

    Capped retention: keep the last 10 snapshots. Failures here are non-fatal —
    they must not block a sync run, only warn.
    """
    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ"
        )
        path = SNAPSHOT_DIR / f"models-{stamp}.json"
        path.write_text(json.dumps(list(existing.keys()), indent=2))
        # Retain only the 10 most recent.
        snaps = sorted(SNAPSHOT_DIR.glob("models-*.json"))
        for old in snaps[:-10]:
            old.unlink()
        log.info(f"[snapshot] saved {path.name} ({len(existing)} models)")
    except Exception as e:  # pragma: no cover - defensive
        log.warning(f"[snapshot] could not save snapshot: {e}")





# ── LiteLLM state ─────────────────────────────────────────────────────────────


def get_existing_litellm_models():
    """Return dict: litellm_model → {"id": ..., "api_key": ...}"""
    try:
        data = _get_litellm("/model/info")
        result = {}
        for entry in data.get("data", []):
            lp = entry.get("litellm_params", {})
            model_str = lp.get("model", "")
            if model_str:
                result[model_str] = {
                    "id": entry.get("model_info", {}).get("id", ""),
                    "api_key": lp.get("api_key", ""),
                }
        return result
    except Exception as e:
        log.error(f"Failed to fetch existing models: {e}")
        return {}


def delete_model(model_id):
    try:
        _post_litellm("/model/delete", {"id": model_id})
        return True
    except Exception as e:
        log.error(f"  Failed to delete model {model_id}: {e}")
        return False


def add_model(model_name, litellm_model, api_key_env, rpm=None, api_base=None):
    params = {"model": litellm_model, "api_key": f"os.environ/{api_key_env}"}
    if rpm:
        params["rpm"] = rpm
    if api_base:
        params["api_base"] = api_base
    try:
        _post_litellm(
            "/model/new", {"model_name": model_name, "litellm_params": params}
        )
        return True
    except Exception as e:
        log.error(f"  Failed to add {model_name} ({litellm_model}): {e}")
        return False


# ── cheahjs/free-llm-api-resources cross-reference ───────────────────────────


def _extract_section(readme, heading):
    """Return text of a markdown/HTML section starting at ### heading."""
    pattern = rf"### \[?{re.escape(heading)}"
    m = re.search(pattern, readme, re.IGNORECASE)
    if not m:
        return ""
    start = m.start()
    next_section = re.search(r"\n### ", readme[start + 1 :])
    end = start + 1 + next_section.start() if next_section else len(readme)
    return readme[start:end]


def fetch_community_free_models():
    """
    Parse cheahjs/free-llm-api-resources README.
    Returns dict provider_key → set of model IDs.

    Only extracts providers where the README lists actual model IDs
    (not just display names). Currently: cohere, openrouter.
    """
    result = {"cohere": set(), "openrouter": set()}
    try:
        readme = _http_get(CHEAHJS_README_URL, timeout=20)
    except Exception as e:
        log.warning(f"[community] Could not fetch cheahjs README: {e}")
        return result

    # Cohere section lists model IDs directly as plain-text list items
    cohere_section = _extract_section(readme, "Cohere")
    for line in cohere_section.splitlines():
        line = line.strip().lstrip("- ")
        if (
            line
            and not line.startswith("[")
            and not line.startswith("#")
            and not line.startswith("*")
            and not line.startswith("<")
        ):
            if "/" not in line and len(line) < 60:
                result["cohere"].add(line)

    # OpenRouter section has links like (https://openrouter.ai/provider/model:free)
    or_section = _extract_section(readme, "OpenRouter")
    for m in re.finditer(r"openrouter\.ai/([^)\"'\s]+:free)", or_section):
        result["openrouter"].add(m.group(1))

    log.info(
        f"[community] cheahjs: {len(result['openrouter'])} OR models, "
        f"{len(result['cohere'])} Cohere models"
    )
    return result


# ── Provider fetchers ──────────────────────────────────────────────────────────


def fetch_openrouter(api_key, community_ids=None):
    """Free models: pricing.prompt == '0' AND pricing.completion == '0'."""
    try:
        data = _json_get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        free = [
            m["id"]
            for m in data.get("data", [])
            if _is_free(m.get("pricing") or {}, "prompt")
            and _is_free(m.get("pricing") or {}, "completion")
        ]
        if community_ids:
            free = list(set(free) | set(community_ids))
        log.info(f"[OpenRouter] {len(free)} free models from API")
        return free
    except Exception as e:
        log.error(f"[OpenRouter] {e}")
        if community_ids:
            log.info(
                f"[OpenRouter] Falling back to community list ({len(community_ids)} models)"
            )
            return list(community_ids)
        return None


def fetch_groq(api_key):
    """All text/chat models are on free tier (rate-limited)."""
    try:
        data = _json_get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        ids = [
            m["id"]
            for m in data.get("data", [])
            if not any(
                x in m.get("id", "").lower()
                for x in ("whisper", "tts", "embed", "guard")
            )
        ]
        log.info(f"[Groq] {len(ids)} models")
        return ids
    except Exception as e:
        log.error(f"[Groq] {e}")
        return None


def fetch_cerebras(api_key):
    """All models are on free tier (1M tokens/day)."""
    try:
        data = _json_get(
            "https://api.cerebras.ai/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        ids = [m["id"] for m in data.get("data", [])]
        log.info(f"[Cerebras] {len(ids)} models")
        return ids
    except Exception as e:
        log.error(f"[Cerebras] {e}")
        return None


def fetch_sambanova(api_key):
    try:
        data = _json_get(
            "https://api.sambanova.ai/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        ids = [m["id"] for m in data.get("data", [])]
        log.info(f"[SambaNova] {len(ids)} models")
        return ids
    except Exception as e:
        log.error(f"[SambaNova] {e}")
        return None


def fetch_together(api_key):
    """Free models: -Free/-free suffix or pricing == 0."""
    try:
        data = _json_get(
            "https://api.together.ai/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        items = data if isinstance(data, list) else data.get("data", [])
        free = []
        for m in items:
            mid = m.get("id", "")
            p = m.get("pricing") or {}
            if (
                (_is_free(p, "input") and _is_free(p, "output"))
                or "-Free" in mid
                or "-free" in mid
            ):
                free.append(mid)
        log.info(f"[Together] {len(free)} free models")
        return free
    except Exception as e:
        log.error(f"[Together] {e}")
        return None


def fetch_cohere(api_key, community_ids=None):
    """
    Chat models from the trial key, cross-referenced with cheahjs README.
    If the API fails, falls back to the community list.
    """
    try:
        data = _json_get(
            "https://api.cohere.com/v2/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        ids = [
            m["name"]
            for m in data.get("models", [])
            if "chat" in m.get("endpoints", [])
        ]
        if community_ids:
            # add any models listed in community reference that API missed
            ids = list(set(ids) | community_ids)
        log.info(f"[Cohere] {len(ids)} chat models")
        return ids
    except Exception as e:
        log.error(f"[Cohere] API error: {e}")
        if community_ids:
            log.info(
                f"[Cohere] Falling back to community list ({len(community_ids)} models)"
            )
            return list(community_ids)
        return None


def fetch_gemini(api_key):
    """
    Free-tier Gemini models: generateContent-capable flash/gemma variants.
    Pro and Ultra models require billing; TTS/embedding models are excluded.
    """
    try:
        data = _json_get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": api_key},
        )
        free = []
        for m in data.get("models", []):
            name = m.get("name", "").replace(
                "models/", ""
            )  # "models/gemini-2.5-flash" → "gemini-2.5-flash"
            methods = m.get("supportedGenerationMethods", [])
            if "generateContent" not in methods:
                continue
            nl = name.lower()
            # Exclude non-free variants
            if any(
                x in nl for x in ("-pro", "-ultra", "embedding", "-tts", "robotics")
            ):
                continue
            # Include flash, lite, and gemma (open) models
            if any(x in nl for x in ("flash", "gemma")):
                free.append(name)
        log.info(f"[Gemini] {len(free)} free-tier models")
        return free
    except Exception as e:
        log.error(f"[Gemini] {e}")
        return None


def fetch_nvidia(api_key):
    """All NVIDIA NIM models have 40 RPM free credits."""
    try:
        data = _json_get(
            "https://integrate.api.nvidia.com/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        ids = [
            m["id"]
            for m in data.get("data", [])
            if not any(x in m.get("id", "").lower() for x in ("embed", "rerank", "tts"))
        ]
        log.info(f"[NVIDIA NIM] {len(ids)} models")
        return ids
    except Exception as e:
        log.error(f"[NVIDIA NIM] {e}")
        return None


def fetch_huggingface(api_key):
    try:
        data = _json_get(
            "https://router.huggingface.co/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        ids = [
            m["id"]
            for m in data.get("data", [])
            if not any(
                x in m.get("id", "").lower() for x in ("embed", "vision", "tts", "stt")
            )
        ]
        log.info(f"[HuggingFace] {len(ids)} models")
        return ids
    except Exception as e:
        log.error(f"[HuggingFace] {e}")
        return None


def fetch_mistral(api_key):
    """
    Mistral La Plateforme — Experiment/free plan.
    API does not expose pricing, so we add all text gen models and let
    the Mistral rate-limiter handle it (free tier: 1 req/s, 1B tok/month).
    Only adds models not already in config.
    """
    try:
        data = _json_get(
            "https://api.mistral.ai/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        ids = [
            m["id"]
            for m in data.get("data", [])
            if m.get("object") == "model"
            and not any(x in m.get("id", "").lower() for x in ("embed", "moderation"))
        ]
        log.info(f"[Mistral] {len(ids)} models")
        return ids
    except Exception as e:
        log.error(f"[Mistral] {e}")
        return None


def fetch_github(api_key):
    """GitHub Models — free tier (rate-limited), higher limits with Copilot."""
    try:
        data = _json_get(
            "https://models.inference.ai.azure.com/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        items = data if isinstance(data, list) else data.get("data", [])
        ids = [
            m.get("id") or m.get("name", "")
            for m in items
            if not any(
                x in (m.get("id") or m.get("name", "")).lower()
                for x in ("embed", "tts", "whisper", "dall-e", "image")
            )
            and (m.get("id") or m.get("name", ""))
        ]
        log.info(f"[GitHub Models] {len(ids)} models")
        return ids
    except Exception as e:
        log.error(f"[GitHub Models] {e}")
        return None


def fetch_cloudflare(api_key):
    """Cloudflare Workers AI — 10k neurons/day free."""
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    if not account_id:
        log.warning("[Cloudflare] CLOUDFLARE_ACCOUNT_ID not set, skipping")
        return None
    try:
        data = _json_get(
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/models/search?per_page=100",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        ids = [
            m["name"]
            for m in data.get("result", [])
            if "text" in str(m.get("task", {}).get("name", "")).lower()
            and "gen" in str(m.get("task", {}).get("name", "")).lower()
        ]
        log.info(f"[Cloudflare] {len(ids)} models")
        return ids
    except Exception as e:
        log.error(f"[Cloudflare] {e}")
        return None


def fetch_pollinations(api_key):
    try:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        data = _json_get("https://gen.pollinations.ai/v1/models", headers=headers)
        ids = []
        for m in data.get("data", []):
            if "text" not in (m.get("output_modalities") or []):
                continue
            if "/v1/chat/completions" not in (m.get("supported_endpoints") or []):
                continue
            ids.append(m["id"])
        log.info(f"[Pollinations] {len(ids)} models")
        return ids
    except Exception as e:
        log.error(f"[Pollinations] {e}")
        return None


def fetch_kluster(api_key):
    try:
        data = _json_get(
            "https://api.kluster.ai/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        ids = [
            m["id"]
            for m in data.get("data", [])
            if not any(
                x in m.get("id", "").lower()
                for x in ("embed", "bge", "rerank", "tts", "whisper")
            )
        ]
        log.info(f"[Kluster] {len(ids)} models")
        return ids
    except Exception as e:
        log.error(f"[Kluster] {e}")
        return None


def fetch_llm7(api_key):
    """LLM7 works anonymously; token only raises rate limit."""
    try:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        data = _json_get("https://api.llm7.io/v1/models", headers=headers)
        items = data if isinstance(data, list) else data.get("data", [])
        ids = [
            m["id"]
            for m in items
            if m.get("id")
            and not any(
                x in m["id"].lower()
                for x in ("embed", "tts", "audio", "whisper", "image")
            )
        ]
        log.info(f"[LLM7] {len(ids)} models")
        return ids
    except Exception as e:
        log.error(f"[LLM7] {e}")
        return None


def fetch_zai(api_key):
    """Z.ai / Zhipu — documented free Flash tier; /v4/models is unreliable."""
    free_flash = [
        "glm-4-flash",
        "glm-4-flash-250414",
        "glm-4v-flash",
        "glm-z1-flash",
        "glm-4.5-flash",
        "cogvideox-flash",
    ]
    ids = list(free_flash)
    try:
        data = _json_get(
            "https://open.bigmodel.cn/api/paas/v4/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        items = data.get("data", []) if isinstance(data, dict) else data
        for m in items:
            mid = m.get("id") or m.get("modelCode") or ""
            if not mid or mid in ids or "flash" not in mid.lower():
                continue
            if any(
                x in mid.lower()
                for x in ("embed", "rerank", "tts", "stt", "audio", "image")
            ):
                continue
            ids.append(mid)
    except Exception as e:
        log.warning(f"[Z.ai] /models lookup failed ({e}); using hardcoded list")
    log.info(f"[Z.ai] {len(ids)} models")
    return ids


# ── Slug helper ───────────────────────────────────────────────────────────────


def slug(model_id):
    return model_id.split("/")[-1].replace(":free", "").lower()


# ── Provider table ─────────────────────────────────────────────────────────────
# fetch_fn receives (api_key) or (api_key, community_data) — see sync() below.

PROVIDERS = [
    {
        "name": "OpenRouter",
        "env_key": "OPENROUTER_API_KEY",
        "fetch": None,  # handled specially with community data
        "litellm_fmt": lambda mid: f"openrouter/{mid}",
        "name_fmt": lambda mid: f"or/{slug(mid)}",
        "rpm": 20,
        "api_base": None,
    },
    {
        "name": "Groq",
        "env_key": "GROQ_API_KEY",
        "fetch": fetch_groq,
        "litellm_fmt": lambda mid: f"groq/{mid}",
        "name_fmt": lambda mid: f"groq/{mid}",
        "rpm": 30,
        "api_base": None,
    },
    {
        "name": "Cerebras",
        "env_key": "CEREBRAS_API_KEY",
        "fetch": fetch_cerebras,
        "litellm_fmt": lambda mid: f"cerebras/{mid}",
        "name_fmt": lambda mid: f"cerebras/{mid}",
        "rpm": 30,
        "api_base": None,
    },
    {
        "name": "SambaNova",
        "env_key": "SAMBANOVA_API_KEY",
        "fetch": fetch_sambanova,
        "litellm_fmt": lambda mid: f"sambanova/{mid}",
        "name_fmt": lambda mid: f"sn/{slug(mid)}",
        "rpm": 30,
        "api_base": None,
    },
    {
        "name": "Together",
        "env_key": "TOGETHER_API_KEY",
        "fetch": fetch_together,
        "litellm_fmt": lambda mid: f"together_ai/{mid}",
        "name_fmt": lambda mid: f"t/{slug(mid)}",
        "rpm": 15,
        "api_base": None,
    },
    {
        "name": "Cohere",
        "env_key": "COHERE_API_KEY",
        "fetch": None,  # handled specially with community data
        "litellm_fmt": lambda mid: f"cohere/{mid}",
        "name_fmt": lambda mid: f"co/{mid}",
        "rpm": 20,
        "api_base": None,
    },
    {
        "name": "Gemini",
        "env_key": "GEMINI_API_KEY",
        "fetch": fetch_gemini,
        "litellm_fmt": lambda mid: f"gemini/{mid}",
        "name_fmt": lambda mid: f"gemini/{mid}",
        "rpm": 15,
        "api_base": None,
    },
    {
        "name": "NVIDIA NIM",
        "env_key": "NVIDIA_NIM_API_KEY",
        "fetch": fetch_nvidia,
        "litellm_fmt": lambda mid: f"nvidia_nim/{mid}",
        "name_fmt": lambda mid: f"nv/{slug(mid)}",
        "rpm": 20,
        "api_base": None,
    },
    {
        "name": "HuggingFace",
        "env_key": "HF_TOKEN",
        "fetch": fetch_huggingface,
        "litellm_fmt": lambda mid: f"openai/{mid}",
        "name_fmt": lambda mid: f"hf/{slug(mid)}",
        "rpm": 10,
        "api_base": "https://router.huggingface.co/v1",
    },
    {
        "name": "Mistral",
        "env_key": "MISTRAL_API_KEY",
        "fetch": fetch_mistral,
        "litellm_fmt": lambda mid: f"mistral/{mid}",
        "name_fmt": lambda mid: f"mistral/{mid}",
        "rpm": 5,
        "api_base": None,
    },
    {
        "name": "GitHub Models",
        "env_key": "GH_MODELS_TOKEN",
        "fetch": fetch_github,
        "litellm_fmt": lambda mid: f"github/{mid}",
        "name_fmt": lambda mid: f"gh/{slug(mid)}",
        "rpm": 15,
        "api_base": None,
    },
    {
        "name": "Cloudflare",
        "env_key": "CLOUDFLARE_API_KEY",
        "fetch": fetch_cloudflare,
        "litellm_fmt": lambda mid: f"cloudflare/{mid}",
        "name_fmt": lambda mid: f"cf/{slug(mid)}",
        "rpm": 20,
        "api_base": None,  # constructed dynamically in sync() — includes account_id
    },
    {
        "name": "Pollinations",
        "env_key": "POLLINATIONS_API_KEY",
        "fetch": fetch_pollinations,
        "litellm_fmt": lambda mid: f"openai/{mid}",
        "name_fmt": lambda mid: f"pol/{slug(mid)}",
        "rpm": 15,
        "api_base": "https://gen.pollinations.ai/v1",
    },
    {
        "name": "Kluster",
        "env_key": "KLUSTER_API_KEY",
        "fetch": fetch_kluster,
        "litellm_fmt": lambda mid: f"openai/{mid}",
        "name_fmt": lambda mid: f"kl/{slug(mid)}",
        "rpm": 15,
        "api_base": "https://api.kluster.ai/v1",
    },
    {
        "name": "LLM7",
        "env_key": "LLM7_API_KEY",
        "fetch": fetch_llm7,
        "litellm_fmt": lambda mid: f"openai/{mid}",
        "name_fmt": lambda mid: f"llm7/{slug(mid)}",
        "rpm": 30,
        "api_base": "https://api.llm7.io/v1",
        "anonymous_ok": True,
    },
    {
        "name": "Z.ai",
        "env_key": "ZAI_API_KEY",
        "fetch": fetch_zai,
        "litellm_fmt": lambda mid: f"openai/{mid}",
        "name_fmt": lambda mid: f"zai/{slug(mid)}",
        "rpm": 20,
        "api_base": "https://open.bigmodel.cn/api/paas/v4",
    },
]


# ── Main sync ─────────────────────────────────────────────────────────────────


def sync(dry_run=False, validate_only=False):
    """
    Discover and register free models.

    Modes:
      dry_run (default False): compute the add/delete plan but do NOT call the
        Management API. Prints what *would* change.
      validate_only (default False): run the discovery + policy admission, then
        exit without touching LiteLLM at all (not even snapshot). Useful as a
        CI / preflight check: "would sync produce any paid/ambiguous routes?"
    """
    mode = "VALIDATE-ONLY" if validate_only else ("DRY-RUN" if dry_run else "LIVE")
    log.info(f"=== Model sync started [{mode}] FREE_ONLY={FREE_ONLY} ===")
    if CLEANUP_STALE:
        log.info("Stale model cleanup enabled (CLEANUP_STALE_MODELS=true)")

    community = fetch_community_free_models()

    existing = get_existing_litellm_models()  # dict: litellm_model → {"id", "api_key"}
    existing_set = set(existing.keys())
    log.info(f"Currently {len(existing_set)} litellm model entries registered")

    added = skipped = errors = deleted = quarantined = 0
    failed_providers = []
    to_delete = []
    to_add = []

    def fetch_provider_models(provider):
        api_key = os.environ.get(provider["env_key"], "")
        if not api_key and not provider.get("anonymous_ok"):
            return provider, None

        if provider["name"] == "Cohere":
            models = fetch_cohere(api_key, community.get("cohere"))
        elif provider["name"] == "OpenRouter":
            models = fetch_openrouter(api_key, community.get("openrouter"))
        else:
            models = provider["fetch"](api_key)

        return provider, models

    fetched_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(fetch_provider_models, p) for p in PROVIDERS]
        # To maintain deterministic behavior and avoid race conditions with existing_set,
        # we process the results sequentially after they are fetched.
        for future in futures:
            try:
                provider, models = future.result()
            except Exception as e:
                # A single provider blowing up must not abort the whole sync.
                log.error(f"[fetch] provider failed during discovery: {e}")
                failed_providers.append(str(e)[:120])
                continue
            fetched_results.append((provider, models))
            if models is None:
                failed_providers.append(f"{provider['name']}: no result (skipped)")

    for provider, models in fetched_results:
        if models is None:
            continue

        cf_account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
        api_base = (
            f"https://api.cloudflare.com/client/v4/accounts/{cf_account_id}/ai"
            if provider["name"] == "Cloudflare" and cf_account_id
            else provider.get("api_base")
        )

        # Is this provider one we treat as paid-by-default? (provider-policy.yaml)
        provider_paid = _provider_is_paid(provider)

        # Remove models that are no longer offered by this provider.
        # IMPORTANT: never delete protected group routes (smart/fast/aion-* …).
        if CLEANUP_STALE and models is not None and not validate_only:
            expected_key = f"os.environ/{provider['env_key']}"
            current = {provider["litellm_fmt"](mid) for mid in models}
            for model_str, info in list(existing.items()):
                if info["api_key"] != expected_key or model_str in current:
                    continue
                if is_protected_route_name(model_str):
                    log.info(
                        f"  ⛔ refusing to delete protected group route "
                        f"{model_str} (manual)"
                    )
                    continue
                to_delete.append((model_str, info["id"]))
                existing_set.discard(model_str)

        for mid in models:
            litellm_model = provider["litellm_fmt"](mid)
            if litellm_model in existing_set:
                skipped += 1
                continue

            model_name = provider["name_fmt"](mid)
            # Admission policy (free-only / allow / deny / quarantine).
            admit, reason = admission_decision(
                litellm_model=litellm_model,
                provider_paid=provider_paid,
                free_marker=True,  # fetch_* already filtered to free tier
            )
            if not admit:
                if reason == "ambiguous pricing (quarantined)":
                    quarantined += 1
                    log.info(f"  ⏸  quarantine: {litellm_model} ({reason})")
                else:
                    log.info(f"  ✋ skip {litellm_model}: {reason}")
                continue

            to_add.append(
                {
                    "model_name": model_name,
                    "litellm_model": litellm_model,
                    "api_key_env": provider["env_key"],
                    "rpm": provider["rpm"],
                    "api_base": api_base,
                    "reason": reason,
                }
            )
            existing_set.add(litellm_model)

    # --- validate-only: report and stop, no writes --------------------------
    if validate_only:
        log.info(
            f"[validate-only] would add {len(to_add)}, "
            f"quarantine {quarantined}, delete {len(to_delete)} "
            f"(no changes applied)"
        )
        _emit_report(0, 0, skipped, quarantined, errors, failed_providers)
        return

    # --- snapshot before mutating (defensive, non-fatal) --------------------
    if not dry_run:
        snapshot_existing_models(existing)

    # --- dry-run: report the plan and stop ----------------------------------
    if dry_run:
        for item in to_add:
            log.info(f"  [would add] {item['model_name']} ({item['litellm_model']})")
        for model_str, _mid in to_delete:
            log.info(f"  [would del] {model_str}")
        _emit_report(len(to_add), len(to_delete), skipped, quarantined, errors, failed_providers)
        return

    # --- live apply ---------------------------------------------------------
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        future_to_delete = {
            executor.submit(delete_model, model_id): model_str
            for model_str, model_id in to_delete
        }
        for future in concurrent.futures.as_completed(future_to_delete):
            model_str = future_to_delete[future]
            try:
                ok = future.result()
                if ok:
                    log.info(f"  - Removed stale: {model_str}")
                    deleted += 1
                else:
                    errors += 1
            except Exception as e:
                log.error(f"Error removing {model_str}: {e}")
                errors += 1

        future_to_add = {
            executor.submit(
                add_model,
                model_name=item["model_name"],
                litellm_model=item["litellm_model"],
                api_key_env=item["api_key_env"],
                rpm=item["rpm"],
                api_base=item["api_base"],
            ): item
            for item in to_add
        }
        for future in concurrent.futures.as_completed(future_to_add):
            item = future_to_add[future]
            try:
                ok = future.result()
                if ok:
                    log.info(f"  + {item['model_name']}  ({item['litellm_model']})")
                    added += 1
                else:
                    errors += 1
            except Exception as e:
                log.error(
                    f"Error adding {item['model_name']} ({item['litellm_model']}): {e}"
                )
                errors += 1

    _emit_report(added, deleted, skipped, quarantined, errors, failed_providers)


def _provider_is_paid(provider):
    """
    Map a provider entry to its LiteLLM model-string prefix and check whether
    that prefix is listed under provider-policy.yaml::paid_providers.
    """
    # Build one representative litellm_model string from the provider's
    # litellm_fmt, then check if its first path segment is a paid provider.
    sample = provider["litellm_fmt"]("__probe__")
    first = sample.split("/")[0]
    return first in _PAID_PROVIDERS


def _emit_report(added, deleted, skipped, quarantined, errors, failed_providers):
    log.info(
        f"=== Done: +{added} added, -{deleted} removed, "
        f"{skipped} already existed, {quarantined} quarantined, "
        f"{errors} errors ==="
    )
    if failed_providers:
        log.warning(
            f"Failed/unreachable providers ({len(failed_providers)}): "
            + "; ".join(sorted(set(failed_providers)))
        )


def wait_for_litellm():
    log.info(f"Waiting up to {STARTUP_DELAY_S}s for LiteLLM to be ready...")
    deadline = time.time() + STARTUP_DELAY_S
    while time.time() < deadline:
        try:
            _get_litellm("/health/liveliness")
            log.info("LiteLLM is up.")
            return
        except Exception:
            time.sleep(5)
    log.warning("LiteLLM did not become ready in time — proceeding anyway.")


def _parse_args(argv):
    p = argparse.ArgumentParser(
        description="Aion Model Gateway — free-model auto-discovery sync.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute add/delete plan and print it, but do not change LiteLLM.",
    )
    p.add_argument(
        "--validate-only",
        action="store_true",
        help=(
            "Run discovery + policy admission and exit. No snapshot, no writes. "
            "Use as a preflight: exit 0 means no paid/ambiguous models would be "
            "registered."
        ),
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Run a single sync pass and exit (skip the wait-for-ready + loop).",
    )
    p.add_argument(
        "--no-wait",
        action="store_true",
        help="Skip the LiteLLM readiness wait (use with --once in CI).",
    )
    return p.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])

    # validate-only and dry-run are single-pass by nature.
    single_pass = args.once or args.dry_run or args.validate_only

    if single_pass:
        if not args.no_wait and not args.dry_run and not args.validate_only:
            wait_for_litellm()
        try:
            sync(dry_run=args.dry_run, validate_only=args.validate_only)
        except Exception as e:
            log.error(f"Sync failed: {e}")
            sys.exit(1)
        sys.exit(0)

    # Long-running daemon mode (the docker compose `model-sync` service).
    if not args.no_wait:
        wait_for_litellm()
    while True:
        try:
            sync()
        except Exception as e:
            log.error(f"Sync failed unexpectedly: {e}")
        log.info(f"Next sync in {SYNC_INTERVAL_H}h")
        time.sleep(SYNC_INTERVAL_H * 3600)
