"""Paths, version stamps and model configuration.

Every number this project publishes carries a version stamp. Bump
PIPELINE_VERSION whenever compute_facts changes shape or math, because a
report generated last week and one generated today must never be silently
different.
"""

from __future__ import annotations

import os
from pathlib import Path

try:  # optional, only used for local development
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - dotenv is pinned, but stay defensive
    load_dotenv = None


PIPELINE_VERSION = "yds-graph 0.2.0"
FACTS_SCHEMA_VERSION = "facts-v1"
RULES_VERSION = "gates-v1"

# Claude model. Do not downgrade without a reason written in the commit.
MODEL_ID = "claude-opus-5"
MAX_TOKENS = 16000

# Coverage gate thresholds. These are deliberately conservative: a thin
# sample produces a confident looking report, which is the failure mode
# this whole repo exists to prevent.
MIN_PITCHES_PER_PITCHER = 100
MIN_GAMES = 2
MAX_NULL_LOCATION_PCT = 10.0

# Sanity gate: plausible release speeds in miles per hour.
VELOCITY_MIN = 50.0
VELOCITY_MAX = 105.0
MAX_VELOCITY_OUTLIER_PCT = 2.0

# Strike zone in feet, used for the 3x3 grid and for chase rate.
ZONE_HALF_WIDTH = 0.83
ZONE_BOTTOM = 1.50
ZONE_TOP = 3.50

# Notes contract enforced in code after the model writes.
MIN_SENTENCES = 4
MAX_SENTENCES = 7
# A number in the prose may differ from the fact by this much and still be
# considered the same number (the model is allowed to round).
NUMBER_TOLERANCE = 0.55

# Library graph: retrieve this many passages, drop anything below the score.
# The store is TF-IDF vectors in SQLite. Roster and schedule stay SQL.
# A chunk also has to share a content word (5+ letters) with the question,
# so "who plays shortstop" does not retrieve a parent-message passage.
LIBRARY_TOP_K = 4
LIBRARY_MIN_SCORE = 0.12
LIBRARY_OVERLAP_MIN_LEN = 5


REPO_ROOT = Path(__file__).resolve().parent.parent


def _is_placeholder(value: str | None) -> bool:
    """True when the value is the shipped example rather than a real key.

    `.env.example` carries `ANTHROPIC_API_KEY=sk-ant-...`, and a copied `.env`
    that was never filled in must count as no key at all, not as a key that
    fails at the first call.
    """
    text = (value or "").strip()
    if not text:
        return True
    upper = text.upper()
    if text.startswith("sk-ant-") and (text.endswith("...") or "PASTE" in upper):
        return True
    return False


def _scrub_placeholders() -> None:
    for name in ("ANTHROPIC_API_KEY",):
        if name in os.environ and _is_placeholder(os.environ[name]):
            del os.environ[name]


def _load_env() -> None:
    """Load .env from the checkout, then ~/.config/yds/secrets.env.

    The path is relative to this file, not to home(), because the tests point
    YDS_GRAPH_HOME at a temporary directory and the real .env still has to be
    found.
    """
    if load_dotenv is not None:
        load_dotenv(REPO_ROOT / ".env", override=False)
        secrets = Path.home() / ".config" / "yds" / "secrets.env"
        if secrets.exists():
            load_dotenv(secrets, override=False)
    _scrub_placeholders()


def home() -> Path:
    """Repository root. Override with YDS_GRAPH_HOME (the tests do)."""
    override = os.environ.get("YDS_GRAPH_HOME")
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parent.parent


def inbox_dir() -> Path:
    return home() / "inbox"


def outbox_dir() -> Path:
    return home() / "outbox"


def errors_dir() -> Path:
    return home() / "errors"


def sample_data_dir() -> Path:
    return home() / "sample_data"


def state_dir() -> Path:
    d = home() / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def audit_db_path() -> Path:
    return home() / "audit.sqlite"


def checkpoint_db_path() -> Path:
    return state_dir() / "checkpoints.sqlite"


def assistant_db_path() -> Path:
    return state_dir() / "assistant.sqlite"


def stub_mode() -> str:
    """Empty string means "call the real model"."""
    return os.environ.get("YDS_GRAPH_STUB", "").strip()


def api_key_available() -> bool:
    _load_env()
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


# --------------------------------------------------------------------------
# Which model answers
#
# anthropic      the hosted model, the default when a key is present
# openai_compat  any server that speaks OpenAI /v1/chat/completions, which
#                includes llama.cpp's server and ollama, so the whole pipeline
#                runs on a machine on your own network at no cost
# stub           deterministic, no network; the same thing YDS_GRAPH_STUB does
# --------------------------------------------------------------------------

BACKEND_ANTHROPIC = "anthropic"
BACKEND_OPENAI_COMPAT = "openai_compat"
BACKEND_STUB = "stub"

# Local models are small and slow. Give them room and time.
LOCAL_MAX_TOKENS = 4000
LOCAL_TIMEOUT_SECONDS = 600.0


def model_base_url() -> str:
    """Base URL of an OpenAI compatible server, ending in /v1."""
    _load_env()
    return os.environ.get("YDS_MODEL_BASE_URL", "").strip().rstrip("/")


def model_name() -> str:
    _load_env()
    return os.environ.get("YDS_MODEL_NAME", "").strip()


def model_api_key() -> str:
    """llama.cpp ignores this. Some proxies want a non empty bearer token."""
    _load_env()
    return os.environ.get("YDS_MODEL_API_KEY", "").strip() or "none"


def model_backend() -> str:
    """Resolve the backend, honouring an explicit choice first.

    With no explicit choice: a real ANTHROPIC_API_KEY means the hosted model,
    otherwise a configured YDS_MODEL_BASE_URL means the local server. That is
    the auto detect, so a local only checkout needs two variables and no third.
    """
    _load_env()
    explicit = os.environ.get("YDS_MODEL_BACKEND", "").strip().lower()
    if explicit:
        if explicit not in (BACKEND_ANTHROPIC, BACKEND_OPENAI_COMPAT, BACKEND_STUB):
            raise ValueError(
                f"YDS_MODEL_BACKEND={explicit!r} is not one of "
                f"{BACKEND_ANTHROPIC}, {BACKEND_OPENAI_COMPAT}, {BACKEND_STUB}."
            )
        return explicit
    if os.environ.get("ANTHROPIC_API_KEY"):
        return BACKEND_ANTHROPIC
    if model_base_url():
        return BACKEND_OPENAI_COMPAT
    return BACKEND_ANTHROPIC


def local_model_configured() -> bool:
    return bool(model_base_url())


def live_backend_description() -> str:
    """One line naming what a live run would call. Used in skip messages."""
    backend = model_backend()
    if backend == BACKEND_ANTHROPIC:
        if os.environ.get("ANTHROPIC_API_KEY"):
            return f"anthropic {MODEL_ID}"
        return "anthropic (no ANTHROPIC_API_KEY)"
    if backend == BACKEND_OPENAI_COMPAT:
        base = model_base_url() or "(no YDS_MODEL_BASE_URL)"
        return f"openai_compat {model_name() or '(no YDS_MODEL_NAME)'} at {base}"
    return "stub"
