"""Shared signal schema — the plan's Signal JSON as Pydantic v2 models.

Every analyst returns the same structure so the PM can score signals
mechanically before reasoning about them. The evidence rule is a validator,
not a convention: a signal with no evidence item, or an evidence item with no
http(s) URL, raises at construction. This is the main defense against
invented facts ("evidence or it doesn't exist", Master Plan p.8).
"""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class Direction(StrEnum):
    """A signal's directional stance; serializes to its plain string value."""

    BULLISH = "bullish"
    NEUTRAL = "neutral"
    BEARISH = "bearish"


class Evidence(BaseModel):
    """One cited source. A signal without at least one of these is invalid."""

    source: str  # e.g. "Form 4", "FRED UNRATE", "GDELT article"
    url: str
    excerpt: str


class Signal(BaseModel):
    """The shared schema every analyst emits; scored by core/scoring.py."""

    agent: str
    tier: int
    ticker: str | None = None  # None for market-wide signals (macro)
    timestamp: datetime
    signal: Direction
    strength: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    horizon: str  # "1-3 months", "days", ...
    red_flag: bool = False
    thesis: str
    evidence: list[Evidence] = Field(min_length=1)
    risks: list[str] = []

    @field_validator("timestamp")
    @classmethod
    def tz_aware(cls, value: datetime) -> datetime:
        """Coerce to UTC — naive datetimes corrupt the brief's ordering."""
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @field_validator("evidence")
    @classmethod
    def urls_required(cls, value: list[Evidence]) -> list[Evidence]:
        """Rule zero: no evidence, no signal."""
        for item in value:
            if not item.url or not item.url.startswith(("http://", "https://")):
                raise ValueError(f"evidence must carry a URL: {item.source!r}")
        return value
