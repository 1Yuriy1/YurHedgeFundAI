"""Persistence layer — the audit trail between scoring and PM synthesis.

The pipeline writes every validated signal and every ladder score record to
Postgres BEFORE the PM call, so a crash mid-synthesis never loses the record
of what the analysts saw and scored. Tests run offline against in-memory
fakes of the :class:`SignalRepository` protocol; the Postgres implementation
keeps its SQL construction in pure, assertable functions.
"""

from typing import Final, Protocol

from core.schemas import Signal
from core.scoring import VoteRecord
from pydantic import BaseModel, Field

# Group key for market-wide signals (ticker is None on the Signal itself).
MARKET_GROUP: Final[str] = "MARKET"


class ScoreRecord(BaseModel):
    """One persisted ladder score — a scored group's aggregate() result.

    ``ticker`` is None for the market-wide group; a snapshot of the vote
    trail rides along so the audit trail explains every score after the fact.
    """

    ticker: str | None
    score: float = Field(ge=-1.0, le=1.0)
    led_by: str | None
    red_flag: bool
    votes: list[VoteRecord]


class SignalRepository(Protocol):
    """Where validated signals and score records are written.

    Implemented by :class:`storage.postgres.PostgresSignalRepository` in
    production and by in-memory fakes in tests. Persistence happens after
    scoring and before PM synthesis — the crash-safety contract.
    """

    def save_signals(self, run_id: str, run_date: str, signals: list[Signal]) -> int:
        """Persist the run's validated signals; returns the row count."""
        ...

    def save_score_records(self, run_id: str, run_date: str, records: list[ScoreRecord]) -> int:
        """Persist the run's ladder scores; returns the row count."""
        ...


__all__ = ["MARKET_GROUP", "ScoreRecord", "SignalRepository"]
