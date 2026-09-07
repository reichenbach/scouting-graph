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


PIPELINE_VERSION = "yds-graph 0.1.0"
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


def _load_env() -> None:
    if load_dotenv is None:
        return
    load_dotenv(home() / ".env", override=False)
    secrets = Path.home() / ".config" / "yds" / "secrets.env"
    if secrets.exists():
        load_dotenv(secrets, override=False)


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
