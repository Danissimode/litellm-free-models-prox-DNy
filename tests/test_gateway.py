"""
Aion Model Gateway — smoke-level unit tests.

These cover the policy/admission logic and config invariants that keep the
gateway safe without needing a running LiteLLM:
  - free-only admission (allow / deny / quarantine)
  - manual routing-group protection (aion-* never auto-deleted)
  - role aliases exist in config.yaml
  - no real API keys committed anywhere in config / .env.example
  - config.yaml parses and references secrets only via os.environ/
"""
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config.yaml"
ENV_EXAMPLE = REPO / ".env.example"


# ─────────────────────────────────────────────────────────────────────────────
# Load sync_models as a module (its functions are pure where we test them).
# ─────────────────────────────────────────────────────────────────────────────
def _load_sync(monkeypatch_env):
    spec = importlib.util.spec_from_file_location("sync_models", REPO / "sync_models.py")
    mod = importlib.util.module_from_spec(spec)
    for k, v in monkeypatch_env.items():
        os.environ[k] = v
    spec.loader.exec_module(mod)
    return mod


# ─────────────────────────────────────────────────────────────────────────────
# Config invariants
# ─────────────────────────────────────────────────────────────────────────────
def test_config_yaml_parses():
    cfg = yaml.safe_load(CONFIG.read_text())
    assert isinstance(cfg.get("model_list"), list) and len(cfg["model_list"]) > 0


def test_role_aliases_present():
    cfg = yaml.safe_load(CONFIG.read_text())
    names = {e["model_name"] for e in cfg["model_list"]}
    for alias in (
        "aion-architect",
        "aion-programmer",
        "aion-reviewer",
        "aion-fast",
        "smart",
        "fast",
        "reasoning",
        "coder",
        "long",
        "vision",
    ):
        assert alias in names, f"missing routing group/alias: {alias}"


def test_config_has_no_literal_api_keys():
    text = CONFIG.read_text()
    # Every api_key must be an os.environ/ reference, never a literal.
    for m in re.finditer(r"api_key:\s*(.+)", text):
        assert m.group(1).strip().startswith(
            "os.environ/"
        ), f"literal api_key in config.yaml: {m.group(0)!r}"


def test_no_real_provider_keys_committed():
    """Scan repo (minus docs/, .git, .env.example) for real-looking keys."""
    patterns = [
        r"sk-or-v1-[a-zA-Z0-9]{16,}",   # OpenRouter
        r"gsk_[A-Za-z0-9]{30,}",        # Groq
        r"AIza[0-9A-Za-z_-]{30,}",      # Google
        r"gh[pousr]_[A-Za-z0-9]{30,}",  # GitHub
        r"hf_[A-Za-z0-9]{30,}",         # HuggingFace
        r"nvapi-[A-Za-z0-9]{30,}",      # NVIDIA
        r"sk-lf-[A-Za-z0-9]{30,}",      # Langfuse
    ]
    rx = re.compile("|".join(patterns))
    bad = []
    for path in REPO.rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(REPO))
        if rel.startswith(".git/") or rel.startswith("docs/") or path.name == ".env.example":
            continue
        if path.suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".lock"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if rx.search(text):
            bad.append(rel)
    assert not bad, f"possible real API keys committed in: {bad}"


# ─────────────────────────────────────────────────────────────────────────────
# Admission policy
# ─────────────────────────────────────────────────────────────────────────────
def test_admission_freeonly():
    os.environ["CONFIG_DIR"] = str(REPO / "config")
    mod = _load_sync({"FREE_ONLY": "true"})

    # allowlisted :free model is admitted even if "paid"
    admit, _ = mod.admission_decision(
        "openrouter/meta-llama/llama-3.3-70b-instruct:free",
        provider_paid=True,
        free_marker=True,
    )
    assert admit

    # paid + denylisted gpt-4 always denied
    admit, reason = mod.admission_decision(
        "openai/gpt-4", provider_paid=True, free_marker=True
    )
    assert not admit and reason == "denylisted"

    # paid provider, not marked free -> denied (distinct, informative reason)
    admit, reason = mod.admission_decision(
        "openai/gpt-3.5-turbo", provider_paid=True, free_marker=False
    )
    assert not admit and "paid" in reason

    # non-paid provider with ambiguous pricing (no free marker, not
    # allowlisted) -> quarantine, never silently routed
    admit, reason = mod.admission_decision(
        "unknown-provider/some-model", provider_paid=False, free_marker=False
    )
    assert not admit and "quarantined" in reason

    # free-confirmed non-paid provider -> admitted
    admit, _ = mod.admission_decision(
        "groq/llama-3.3-70b-versatile", provider_paid=False, free_marker=True
    )
    assert admit


def test_admission_freeonly_disabled():
    """When FREE_ONLY=false, ambiguity alone no longer blocks (deny still wins)."""
    mod = _load_sync({"FREE_ONLY": "false"})
    admit, reason = mod.admission_decision(
        "some-unknown-provider/some-model", provider_paid=False, free_marker=False
    )
    assert admit and reason == "free-only disabled"
    # denylist still applies
    admit, reason = mod.admission_decision(
        "openai/gpt-4", provider_paid=False, free_marker=True
    )
    assert not admit and reason == "denylisted"


def test_manual_groups_protected():
    mod = _load_sync({"FREE_ONLY": "true"})
    for name in ("smart", "fast", "reasoning", "coder", "long", "vision",
                 "aion-architect", "aion-programmer", "aion-reviewer", "aion-fast"):
        assert mod.is_protected_route_name(name), f"{name} should be protected"
    # a discovered provider route must NOT be protected
    assert not mod.is_protected_route_name("or/meta-llama/llama-3.3-70b:free")
    assert not mod.is_protected_route_name("groq/llama-3.3-70b-versatile")


# ─────────────────────────────────────────────────────────────────────────────
# CLI surface (smoke): --help and --validate-only don't crash offline
# ─────────────────────────────────────────────────────────────────────────────
def test_sync_help_runs():
    r = subprocess.run(
        [sys.executable, str(REPO / "sync_models.py"), "--help"],
        capture_output=True, text=True, timeout=20,
    )
    assert r.returncode == 0
    assert "--dry-run" in r.stdout
    assert "--validate-only" in r.stdout


def test_env_example_required_vars_present():
    text = ENV_EXAMPLE.read_text()
    for var in ("LITELLM_MASTER_KEY", "LITELLM_SALT_KEY", "POSTGRES_PASSWORD",
                "DATABASE_URL", "FREE_ONLY"):
        assert re.search(rf"^{var}\s*=", text, re.M), f".env.example missing {var}"


def test_env_example_has_no_demo_master_key():
    """The template value for LITELLM_MASTER_KEY must be empty, not a demo key."""
    text = ENV_EXAMPLE.read_text()
    m = re.search(r"^LITELLM_MASTER_KEY=(.*)$", text, re.M)
    assert m, "LITELLM_MASTER_KEY line missing"
    assert m.group(1).strip() == "", "LITELLM_MASTER_KEY should be blank in template"
