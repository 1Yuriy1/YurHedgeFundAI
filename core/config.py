"""Environment-driven configuration.

Every setting the platform needs comes from the environment (or a local `.env`
file) — nothing about keys, models, or infrastructure is hardcoded at call
sites. See `.env.example` for the documented variables.
"""

from functools import lru_cache
from typing import Any, Final

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

# Default macro series for the Macro & Rates analyst (spec's connector table).
DEFAULT_FRED_SERIES: Final[tuple[str, ...]] = (
    "UNRATE",
    "FEDFUNDS",
    "DGS10",
    "DGS2",
    "T10Y2Y",
    "BAMLH0A0HYM2",
    "CPIAUCSL",
    "PCEPI",
)

# Agent name → opinion-ladder tier (Master Plan p.3): macro leads the market
# regime, SEC filings carry company-level hard facts, fundamentals weigh least
# of the three analysts. Override via AGENT_TIERS="macro_rates:2,...".
DEFAULT_AGENT_TIERS: Final[dict[str, int]] = {
    "macro_rates": 2,
    "sec_filings": 3,
    "fundamentals": 4,
}

# Ticker → CIK for EDGAR submissions, verified against the SEC's official
# company_tickers.json on 2026-09-17 (zero-padded by the connector).
DEFAULT_EDGAR_CIKS: Final[dict[str, str]] = {
    "AAPL": "0000320193",
    "MSFT": "0000789019",
    "NVDA": "0001045810",
    "GOOGL": "0001652044",
    "AMZN": "0001018724",
    "META": "0001326801",
    "TSLA": "0001318605",
    "JPM": "0000019617",
    "XOM": "0002115436",
    "UNH": "0000731766",
}


def _parse_kv_string(raw: str, value_type: type[int] | type[str]) -> dict[str, Any]:
    """Parse "KEY:VALUE,KEY:VALUE" into a dict with values of ``value_type``.

    Used by the env-string forms of EDGAR_CIKS and AGENT_TIERS; malformed
    entries raise rather than being skipped so a typo fails loudly at startup.
    """
    parsed: dict[str, Any] = {}
    for part in raw.split(","):
        entry = part.strip()
        if not entry:
            continue
        key, sep, value = entry.partition(":")
        if not sep or not key.strip():
            raise ValueError(f"expected KEY:VALUE entries, got {entry!r}")
        parsed[key.strip().upper() if value_type is str else key.strip()] = value_type(
            value.strip()
        )
    return parsed


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

    # FRED series the Macro & Rates analyst watches (overridable via FRED_SERIES="A,B,...")
    fred_series: list[str] = Field(default_factory=lambda: list(DEFAULT_FRED_SERIES))

    # Analyst name → opinion-ladder tier (AGENT_TIERS="macro_rates:2,sec_filings:3,...")
    agent_tiers: dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_AGENT_TIERS))

    # Ticker → CIK for EDGAR submissions (EDGAR_CIKS="AAPL:0000320193,MSFT:0000789019,...")
    edgar_ciks: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_EDGAR_CIKS))

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

    # Token budget per analyst LLM call
    anthropic_max_tokens: int = 4096

    @field_validator("universe", "fred_series", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Accept UNIVERSE/FRED_SERIES as comma-separated strings (env) or lists."""
        if isinstance(value, str):
            return [part.strip().upper() for part in value.split(",") if part.strip()]
        return value

    @field_validator("edgar_ciks", mode="before")
    @classmethod
    def _parse_cik_map(cls, value: object) -> object:
        """Accept EDGAR_CIKS as a "TICKER:CIK,..." string (env) or a dict."""
        if isinstance(value, str):
            return _parse_kv_string(value, str)
        return value

    @field_validator("agent_tiers", mode="before")
    @classmethod
    def _parse_tier_map(cls, value: object) -> object:
        """Accept AGENT_TIERS as a "name:tier,..." string (env) or a dict."""
        if isinstance(value, str):
            return _parse_kv_string(value, int)
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
