"""Runtime configuration.

Everything tunable about the agent lives here so that experiments can vary one
knob at a time without editing agent code.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTOPS_",
        env_file=(REPO_ROOT / ".env"),
        extra="ignore",
    )

    # --- Model ---
    model: str = "claude-opus-5"
    effort: str = "medium"
    max_tokens: int = 4096

    # --- Agent loop ---
    max_steps: int = Field(
        default=8,
        description="Hard ceiling on model calls per run. The loop never runs unbounded.",
    )

    # --- Business rules, enforced in code (DD-004) ---
    refund_window_days: int = 30
    refund_auto_approve_max_cents: int = 5_000
    refund_manager_approval_max_cents: int = 20_000

    # --- Data ---
    seed_dir: Path = REPO_ROOT / "data" / "seed"
    documents_dir: Path = REPO_ROOT / "data" / "documents"

    simulated_today: date = Field(
        default=date(2026, 10, 12),
        description=(
            "Business data is evaluated against a fixed date so that eligibility "
            "windows — and therefore evaluation results — do not drift as real "
            "time passes."
        ),
    )

    # --- Guardrail demonstration ---
    expose_unauthorized_tools: bool = Field(
        default=True,
        description=(
            "Advertise tools the agent lacks the scopes to execute. Keeping them "
            "visible is what makes the permission boundary testable end to end: "
            "discovering a tool must never imply authorization to run it."
        ),
    )


@lru_cache
def get_settings() -> Settings:
    return get_settings_uncached()


def get_settings_uncached(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]
