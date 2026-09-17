"""Environment-driven configuration.

Every setting the platform needs comes from the environment (or a local `.env`
file) — nothing about keys, models, or infrastructure is hardcoded at call
sites. See `.env.example` for the documented variables.
"""

from functools import lru_cache
from typing import Final

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_UNIVERSE: Final[tuple[str, ...]] = (
    "AAPL",
    "MSFT",
    "NVDA",
    "GOOGL",
    "AMZN",
    "META",
    "TSLA",
    "JPM",
    "XOM",
    "UNH",
)


class Settings(BaseSettings):
    """All platform settings, read from the environment.

    Locked decision 2: model IDs live in config, never hardcoded. The exact
    API model-ID strings are flagged for confirmation in docs/research-notes.md
    (§a); changing them is a one-line edit here.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Data universe (overridable via UNIVERSE="AAPL,MSFT,...")
    universe: list[str] = Field(default_factory=lambda: list(DEFAULT_UNIVERSE))

    # Credentials and service endpoints
    anthropic_api_key: str | None = None
    fred_api_key: str | None = None
    fmp_api_key: str | None = None
    edgar_user_agent: str | None = None
    database_url: str = "postgresql+psycopg://yhf:yhf@localhost:5432/yhf"
    redis_url: str = "redis://localhost:6379/0"

    # Model IDs per role (Anthropic)
    anthropic_model_pm: str = "claude-opus-5"
    anthropic_model_analyst: str = "claude-sonnet-5"

    @field_validator("universe", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Accept UNIVERSE as a comma-separated string (env) or a list (programmatic)."""
        if isinstance(value, str):
            return [part.strip().upper() for part in value.split(",") if part.strip()]
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
