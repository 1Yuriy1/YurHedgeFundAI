"""Postgres implementation of the signal repository (psycopg 3).

SQL construction is kept in pure module-level functions —
:func:`signal_row_values`, :func:`score_row_values`, :func:`psycopg_dsn` —
so tests assert the generated statements and parameter tuples without a live
server. Only the two ``save_*`` methods touch a connection; the connection
factory is injectable, but CI never injects a real one.

JSONB parameters are pre-serialized with ``json.dumps`` and cast in SQL
(``%s::jsonb``) so the stored params are plain, assertable strings rather
than driver-specific wrapper objects.
"""

import json
import logging
from collections.abc import Callable, Sequence
from types import TracebackType
from typing import Any, Final, Protocol

import psycopg
from core.schemas import Signal

from storage.repository import ScoreRecord

logger = logging.getLogger(__name__)

SIGNAL_INSERT_SQL: Final[str] = """
INSERT INTO signals (run_id, run_date, agent, tier, ticker, ts, direction,
                     strength, confidence, horizon, red_flag, thesis, evidence, risks)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
""".strip()

SCORE_INSERT_SQL: Final[str] = """
INSERT INTO score_records (run_id, run_date, ticker, score, led_by, red_flag, votes)
VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
""".strip()

# Number of placeholders per row, asserted against in tests so the SQL and
# the row builders cannot drift apart silently.
SIGNAL_PLACEHOLDER_COUNT: Final[int] = SIGNAL_INSERT_SQL.count("%s")
SCORE_PLACEHOLDER_COUNT: Final[int] = SCORE_INSERT_SQL.count("%s")


def psycopg_dsn(database_url: str) -> str:
    """Normalize a settings-style URL to a psycopg DSN.

    ``postgresql+psycopg://user:pass@host:port/db`` (the SQLAlchemy-style
    scheme config carries) becomes ``postgresql://user:pass@host:port/db``;
    URLs without a ``+dialect`` pass through unchanged.
    """
    scheme, separator, rest = database_url.partition("://")
    if not separator:
        raise ValueError(f"not a database URL: {database_url!r}")
    return f"{scheme.split('+', 1)[0]}://{rest}"


def signal_row_values(run_id: str, run_date: str, signal: Signal) -> tuple[Any, ...]:
    """The INSERT parameter tuple for one signal row (JSONB as JSON text)."""
    return (
        run_id,
        run_date,
        signal.agent,
        signal.tier,
        signal.ticker,
        signal.timestamp,
        signal.signal.value,
        signal.strength,
        signal.confidence,
        signal.horizon,
        signal.red_flag,
        signal.thesis,
        json.dumps([evidence.model_dump() for evidence in signal.evidence]),
        json.dumps(signal.risks),
    )


def score_row_values(run_id: str, run_date: str, record: ScoreRecord) -> tuple[Any, ...]:
    """The INSERT parameter tuple for one score-record row."""
    return (
        run_id,
        run_date,
        record.ticker,
        record.score,
        record.led_by,
        record.red_flag,
        json.dumps(record.votes),
    )


class RepositoryCursor(Protocol):
    """The only cursor surface the repository uses (executemany)."""

    def executemany(self, query: str, params_seq: Sequence[tuple[Any, ...]]) -> None: ...

    def __enter__(self) -> "RepositoryCursor": ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class RepositoryConnection(Protocol):
    """Structural stand-in for ``psycopg.Connection`` as this repo uses it.

    The real psycopg connection satisfies it at runtime; tests inject fakes
    without casts.
    """

    def cursor(self) -> RepositoryCursor: ...

    def commit(self) -> None: ...

    def __enter__(self) -> "RepositoryConnection": ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class PostgresSignalRepository:
    """Writes signals and score records to the Postgres audit schema."""

    def __init__(
        self,
        dsn: str,
        *,
        connect: Callable[[], RepositoryConnection] | None = None,
    ) -> None:
        self._dsn = dsn
        self._connect = connect if connect is not None else (lambda: psycopg.connect(dsn))

    @classmethod
    def from_database_url(cls, database_url: str) -> "PostgresSignalRepository":
        """Build from the config's DATABASE_URL (dialect suffix tolerated)."""
        return cls(psycopg_dsn(database_url))

    def save_signals(self, run_id: str, run_date: str, signals: list[Signal]) -> int:
        """Insert every signal; one transaction, all-or-nothing."""
        rows = [signal_row_values(run_id, run_date, signal) for signal in signals]
        if not rows:
            return 0
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.executemany(SIGNAL_INSERT_SQL, rows)
            conn.commit()
        logger.info("signals_persisted", extra={"run_id": run_id, "rows": len(rows)})
        return len(rows)

    def save_score_records(self, run_id: str, run_date: str, records: list[ScoreRecord]) -> int:
        """Insert the run's score records; one transaction, all-or-nothing."""
        rows = [score_row_values(run_id, run_date, record) for record in records]
        if not rows:
            return 0
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.executemany(SCORE_INSERT_SQL, rows)
            conn.commit()
        logger.info("score_records_persisted", extra={"run_id": run_id, "rows": len(rows)})
        return len(rows)


__all__ = [
    "SCORE_INSERT_SQL",
    "SCORE_PLACEHOLDER_COUNT",
    "SIGNAL_INSERT_SQL",
    "SIGNAL_PLACEHOLDER_COUNT",
    "PostgresSignalRepository",
    "RepositoryConnection",
    "RepositoryCursor",
    "psycopg_dsn",
    "score_row_values",
    "signal_row_values",
]
