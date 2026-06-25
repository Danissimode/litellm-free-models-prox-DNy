# Aion Model Gateway

A **hardened, local-only** [LiteLLM](https://github.com/BerriAI/litellm) gateway that auto-discovers and routes to LLM models on **free API tiers** (free tokens, no credit card). Any OpenAI-compatible client connects to a single endpoint:

```
http://127.0.0.1:4000/v1
```

This is a fork of [litellm-free-models-proxy](https://github.com/cheahjs/litellm-free-models-proxy), reworked for local security, reproducible startup, role-based aliases for AI coding agents, and a strict free-only admission policy.

> **Fork status:** All upstream author-specific traces have been removed. The repository is stabilized — CI passes, tests cover policy/config invariants, and the security posture is enforced by `preflight.sh` + `security-check.sh`. Contributions welcome.

## What this is

- A single OpenAI-compatible endpoint for many free/trial LLM providers (OpenRouter, Groq, Cerebras, SambaNova, NVIDIA NIM, Cohere, Gemini, HuggingFace, Mistral, GitHub Models, Cloudflare Workers AI, …).
- **Auto-discovery** of free models every 8h via `sync_models.py` (calls the LiteLLM Management API — never edits your routing groups).
- **Load-balancing** across multiple API keys for the same provider.
- **Role aliases** (`aion-architect`, `aion-programmer`, `aion-reviewer`, `aion-fast`) on top of curated routing groups (`smart`, `fast`, `reasoning`, `coder`, `long`, `vision`).
- Usage logging to a **local** Postgres (not exposed to the host).
- **Free-only policy**: ambiguous/paid models are quarantined or denied, never silently routed.

## What this is not

- **Not an AionUI plugin.** It is a standalone gateway. AionUI / opencode / Cline / KiloCode / Codex CLI / custom agents simply point `OPENAI_BASE_URL` at it.
- **Not a security boundary for code execution.** It authenticates API callers; it does not sandbox what an agent does with model output. Validate agent actions in your own harness.
- **Not a replacement for paid models.** Free tiers are rate-limited; expect occasional 429s and provider churn.

## Local-only quick start

```bash
git clone https://github.com/Danissimode/litellm-free-models-prox-DNy.git
cd litellm-free-models-prox-DNy

cp .env.example .env
# 1) Set a strong master key (REQUIRED — the gateway will not start without it):
echo "LITELLM_MASTER_KEY=sk-$(openssl rand -hex 32)" >> .env
# 2) Set a Postgres password:
echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)" >> .env
# 3) Add the provider keys you actually have (see "Provider keys" below).

./scripts/preflight.sh        # validates env before booting
docker compose up -d
docker compose ps
curl http://127.0.0.1:4000/health     # -> 200
```

LiteLLM binds **only to `127.0.0.1:4000`** by default. No port is published to other hosts, and Postgres is not published at all.

## Required env vars

Set these in `.env` (copy from `.env.example`). `preflight.sh` enforces them:

| Variable | Required | Notes |
|---|---|---|
| `LITELLM_MASTER_KEY` | yes | Must start with `sk-`, not a demo value, `sk-` + at least 16 chars. Generate: `openssl rand -hex 32 \| sed 's/^/sk-/'` |
| `LITELLM_SALT_KEY` | yes | Salt for hashing virtual keys. `openssl rand -hex 32` |
| `POSTGRES_PASSWORD` | yes | Powers the local postgres service. |
| `DATABASE_URL` | yes | `postgresql://litellm:${POSTGRES_PASSWORD}@postgres:5432/litellm` |
| `FREE_ONLY` | recommended | `true` (default). Quarantines ambiguous/paid models. |
| `UI_PASSWORD` | recommended | Admin UI password (change from any default). |

## Provider keys

Add only the providers you use; leave the rest blank. Empty keys simply mean that provider is skipped — the gateway still boots.

```
OPENROUTER_API_KEY=   GROQ_API_KEY=          CEREBRAS_API_KEY=
SAMBANOVA_API_KEY=    NVIDIA_NIM_API_KEY=    COHERE_API_KEY=
GEMINI_API_KEY=       HF_TOKEN=              GH_MODELS_TOKEN=
MISTRAL_API_KEY=      CLOUDFLARE_API_KEY=    CLOUDFLARE_ACCOUNT_ID=
TOGETHER_API_KEY=     CODESTRAL_API_KEY=     ...
```

> `OPENAI_API_KEY` is intentionally **commented out** — OpenAI is paid. Enable it only if you explicitly opt into paid fallback.

Second keys for load-balancing: `GROQ_API_KEY_2`, `GEMINI_API_KEY_2..8`, etc.

## Routing groups

Curated in `config.yaml`. Auto-sync adds named per-model routes (`or/...`, `groq/...`, …) but **never** modifies these groups.

| Group | Purpose |
|---|---|
| `smart` | Best reasoning/quality available |
| `fast` | Low-latency, smaller models |
| `reasoning` | Step-by-step reasoning models |
| `coder` | Code-focused models |
| `long` | Large context window |
| `vision` | Multimodal (text + image) |

## Aion role aliases

Stable role names for agents — each mirrors an upstream pool, so you can rename backing models without touching agent config:

| Alias | Backs | Use for |
|---|---|---|
| `aion-architect` | reasoning pool | architecture / planning |
| `aion-programmer` | coder pool | coding / execution |
| `aion-reviewer` | reasoning pool | review / testing (stricter models) |
| `aion-fast` | fast pool | small / low-latency tasks |

```bash
curl http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"aion-programmer","messages":[{"role":"user","content":"ping"}]}'
```

Direct provider routing also works: `or/llama-3.3-70b`, `groq/qwen3-32b`, `gemini/gemini-2.5-flash`, `co/command-a`, `gh/gpt-4o`, `cf/llama-3.1-8b-instruct`.

## Using with opencode

Copy `profiles/opencode.env.example` into your opencode env, then point opencode at the gateway:

```bash
export OPENAI_BASE_URL=http://127.0.0.1:4000/v1
export OPENAI_API_KEY=<master key OR a virtual key>
export OPENCODE_MODEL=aion-programmer
opencode ...
```

- **Quick local test:** use `LITELLM_MASTER_KEY` directly.
- **Regular use:** create a per-agent **virtual key** (`scripts/create-agent-keys.sh`) and put that key in the agent env — never share the master key.
- Map roles to tasks: planning → `aion-architect`, coding → `aion-programmer`, review → `aion-reviewer`, small tasks → `aion-fast`.

Any OpenAI-compatible tool works the same way (LangChain, Continue.dev, the OpenAI CLI, etc.) — set base URL to `http://127.0.0.1:4000`, key to your gateway key, model to a group/alias.

## Security model

- **Local-only bind.** `docker-compose.yml` publishes `127.0.0.1:4000:4000` only. No `0.0.0.0` / `*:4000`.
- **Master key is mandatory.** The `litellm` container runs `preflight.sh` as its entrypoint and **refuses to boot** if `LITELLM_MASTER_KEY` is missing, not `sk-`-prefixed, a known demo value, or too short.
- **Auth-gated endpoints.** `/v1/models`, `/v1/chat/completions`, and management endpoints require a valid key (the smoke/security tests assert this).
- **No plaintext provider keys in config.** Every `api_key:` in `config.yaml` is `os.environ/...`.
- **Free-only by default.** `FREE_ONLY=true` + `config/provider-policy.yaml` quarantine models with ambiguous pricing and deny known-paid ones. See `config/model-allowlist.yaml` / `config/model-denylist.yaml`.
- **No full prompt/response logging by default.** LiteLLM logs **usage metadata** (model, status, latency, token counts) to the local Postgres. Full prompt/response capture (e.g. Langfuse) is opt-in and off by default.
- **Admin UI** (`/ui`) is protected by `UI_USERNAME`/`UI_PASSWORD` on top of master-key auth.

### Verifying the posture

```bash
./scripts/security-check.sh     # no-auth→401, wrong-key→401, good-key→200, loopback bind
```

## Remote access / Tailscale warning

Remote bind is **opt-in** and discouraged. If you must (e.g. your own tailnet):

```bash
tailscale ip -4                      # note your tailnet IP, e.g. 100.x.y.z
cp docker-compose.tailscale.example.yml docker-compose.tailscale.yml
# edit docker-compose.tailscale.yml: replace 100.x.y.z with your IP
# create per-agent virtual keys FIRST:
./scripts/create-agent-keys.sh
docker compose -f docker-compose.yml -f docker-compose.tailscale.yml up -d
```

**Never** set the bind to `0.0.0.0:4000` or use the master key on a remotely-bound gateway. Always gate remote access behind virtual keys + (ideally) a reverse proxy with TLS.

## Observability (Langfuse, Prometheus)

Both are **off by default**. To enable Langfuse: set `ENABLE_LANGFUSE=true`, the `LANGFUSE_*` vars in `.env`, and add `- langfuse_otel` back under `litellm_settings.callbacks` in `config.yaml`. A reachable Langfuse service is then required. Prometheus `/metrics` is off unless `ENABLE_PROMETHEUS=true`; keep it local-only or behind auth if enabled.

## Smoke tests

```bash
./scripts/smoke-test.sh
```

Checks `GET /health`, `GET /v1/models` (no key leak), and a chat probe for `fast`, `smart`, `reasoning`, `coder`, and all four `aion-*` aliases. Providers without a configured key are reported **SKIPPED** (not failed) — a key-less local install is a valid gateway.

## Security checks

```bash
./scripts/security-check.sh
```

Asserts: no-auth → 401/403, wrong key → 401/403, correct key → 200, and that port 4000 is bound to loopback (not wildcard).

## Provider checks

```bash
./scripts/provider-check.sh
```

Hits each provider's `/models` endpoint directly (bypassing LiteLLM) and classifies `OK` / `SKIPPED` (no key) / `AUTH_ERROR` (bad key) / `RATE_LIMITED` (429, non-fatal) / `UNREACHABLE` (timeout). Non-zero exit (2) only on `AUTH_ERROR`.

## Sync models

```bash
# Dry-run: show what would change, write nothing
docker compose exec model-sync python /app/sync_models.py --dry-run

# Validate-only: discovery + policy admission, no snapshot, no writes (CI preflight)
docker compose exec model-sync python /app/sync_models.py --validate-only

# Force one live sync pass now (otherwise it runs every SYNC_INTERVAL_HOURS=8h)
docker compose exec model-sync python /app/sync_models.py --once
```

The sync never deletes the manual groups (`smart`, `fast`, …, `aion-*`); it only adds/removes discovered per-model routes, respects the free-only policy, and writes a JSON snapshot of registered models (`/app/snapshots/`) before mutating.

## Services

| Service | Purpose |
|---|---|
| `litellm` | The proxy (local-only `127.0.0.1:4000`). Runs `preflight.sh` as its entrypoint. |
| `postgres` | Local persistence for model configs + usage logs. **Not** published to the host. |
| `model-sync` | Auto-discovery every 8h. |

All three run on one compose network (`aion-model-gateway`). No external networks are required.

## Updating from upstream

- **LiteLLM image:** pinned to a specific stable tag in `docker-compose.yml` (e.g. `main-v1.83.14-stable`). To bump, choose the newest `main-vX.Y.Z-stable` tag from `ghcr.io/berriai/litellm`, update the line, and restart. Never use `:main-latest` or an `-rc` tag.
- **Sync/Python deps:** stdlib only — no pin churn.
- **Model lists:** the `update-models.yml` and `probe.yml` GitHub Actions regenerate `docs/` (the availability site) on their own schedule; they do **not** overwrite `config.yaml`.

## Development / tests

```bash
python3 -m pip install --user -e ".[dev]"   # pytest, ruff
python3 -m pytest                          # policy + config invariants
ruff check .
python3 -m compileall sync_models.py common.py probe_models.py generate_site.py
```

## Credits

- [LiteLLM](https://github.com/BerriAI/litellm) — the proxy.
- [cheahjs/litellm-free-models-proxy](https://github.com/cheahjs/litellm-free-models-proxy) — upstream this fork hardens.
- [cheahjs/free-llm-api-resources](https://github.com/cheahjs/free-llm-api-resources) — community free-API list used as a cross-reference.
