"""Reasoning settings: the AI provider credentials and the rightsizing policy thresholds."""

from __future__ import annotations

from dataclasses import dataclass

from app.auth.common import AuthError, read_env

DEFAULT_MODEL = "gpt-5.6-luna"

# Evidence policy: an instance is evaluated only when every rule passes.
MIN_COVERAGE_PERCENT = 90.0    # each metric must cover at least this share of the window hours
MIN_EVIDENCE_HOURS = 1.0       # and at least this many hours, whatever the window

# Capacity policy (same factors as the original capacity analysis).
CPU_SAFETY_FACTOR = 1.25
MEMORY_SAFETY_FACTOR = 1.25

# AI output is validated; an invalid answer is retried with feedback up to this many times.
MAX_AI_ATTEMPTS = 3


class ReasoningError(AuthError):
    """Raised when reasoning cannot run (missing key, AI provider error, invalid AI output)."""


@dataclass(frozen=True)
class ReasoningSettings:
    """OpenAI credentials and model from .env."""

    api_key: str
    model: str


def load_settings() -> ReasoningSettings:
    """Read OPENAI_API_KEY (required) and OPENAI_MODEL (optional) from .env."""
    env = read_env((), ReasoningError, optional={"OPENAI_API_KEY": "", "OPENAI_MODEL": DEFAULT_MODEL})
    if not env["OPENAI_API_KEY"]:
        raise ReasoningError(
            "OPENAI_API_KEY is not set in .env",
            ("Add OPENAI_API_KEY=... to .env", "Or run with --no-ai for rule-based decisions only"),
        )
    return ReasoningSettings(api_key=env["OPENAI_API_KEY"], model=env["OPENAI_MODEL"] or DEFAULT_MODEL)