"""Storage tests — offline SQL-construction checks, no live Postgres.

The Postgres repository keeps statement and row building in pure functions;
these tests assert the generated SQL and parameter tuples directly. The
in-memory fake used by the premarket integration test lives with that test.
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from core.schemas import Direction, Evidence, Signal
from storage.postgres import (
    SCORE_INSERT_SQL,
    SCORE_PLACEHOLDER_COUNT,
    SIGNAL_INSERT_SQL,
    SIGNAL_PLACEHOLDER_COUNT,
    PostgresSignalRepository,
    RepositoryConnection,
    psycopg_dsn,
    score_row_values,
    signal_row_values,
)
from storage.repository import ScoreRecord

RUN_ID = "abc123"
RUN_DATE = "2026-09-17"


def make_signal(**overrides: object) -> Signal:
    base: dict[str, object] = {
        "agent": "sec_filings",
        "tier": 3,
        "ticker": "AAPL",
        "timestamp": datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC),
        "signal": Direction.BEARISH,
        "strength": 0.8,
        "confidence": 0.75,
        "horizon": "1-3 months",
        "red_flag": False,
        "thesis": "Going-concern language in the 10-Q.",
        "evidence": [
            Evidence(
                source="Form 10-Q",
                url="https://www.sec.gov/Archives/edgar/data.htm",
                excerpt="substantial doubt",
            )
        ],
        "risks": ["Management refutes the language."],
    }
    base.update(overrides)
    return Signal.model_validate(base)


def make_record(**overrides: object) -> ScoreRecord:
    base: dict[str, object] = {
        "ticker": "AAPL",
        "score": -0.412,
        "led_by": "sec_filings",
        "red_flag": False,
        "votes": [
            {
                "agent": "sec_filings",
                "tier": 3,
                "direction": "bearish",
                "strength": 0.8,
                "confidence": 0.75,
                "dampened": False,
                "vote": -0.6,
                "weight": 0.7,
            }
        ],
    }
    base.update(overrides)
    return ScoreRecord.model_validate(base)


def test_signal_insert_sql_shape() -> None:
    """The statement targets the right table and matches its row width."""
    assert "INSERT INTO signals" in SIGNAL_INSERT_SQL
    assert SIGNAL_INSERT_SQL.count("%s") == SIGNAL_PLACEHOLDER_COUNT == 14


def test_score_insert_sql_shape() -> None:
    assert "INSERT INTO score_records" in SCORE_INSERT_SQL
    assert SCORE_INSERT_SQL.count("%s") == SCORE_PLACEHOLDER_COUNT == 7


def test_signal_row_values() -> None:
    signal = make_signal()
    row = signal_row_values(RUN_ID, RUN_DATE, signal)

    assert row[:5] == (RUN_ID, RUN_DATE, "sec_filings", 3, "AAPL")
    assert row[5] == signal.timestamp
    assert row[6] == "bearish"
    assert row[7:11] == (0.8, 0.75, "1-3 months", False)
    assert row[11] == "Going-concern language in the 10-Q."
    evidence = json.loads(str(row[12]))
    assert evidence[0]["url"] == "https://www.sec.gov/Archives/edgar/data.htm"
    assert json.loads(str(row[13])) == ["Management refutes the language."]


def test_signal_row_values_market_wide_ticker_is_none() -> None:
    signal = make_signal(agent="macro_rates", tier=2, ticker=None, signal=Direction.BULLISH)
    row = signal_row_values(RUN_ID, RUN_DATE, signal)
    assert row[4] is None
    assert row[6] == "bullish"


def test_score_row_values() -> None:
    record = make_record()
    row = score_row_values(RUN_ID, RUN_DATE, record)
    assert row[:2] == (RUN_ID, RUN_DATE)
    assert row[2] == "AAPL"
    assert row[3] == -0.412
    assert row[4] == "sec_filings"
    assert row[5] is False
    votes = json.loads(str(row[6]))
    assert votes[0]["agent"] == "sec_filings"


def test_score_row_values_market_wide_ticker_is_none() -> None:
    record = make_record(ticker=None)
    assert score_row_values(RUN_ID, RUN_DATE, record)[2] is None


@pytest.mark.parametrize(
    ("database_url", "expected"),
    [
        (
            "postgresql+psycopg://yhf:yhf@localhost:5432/yhf",
            "postgresql://yhf:yhf@localhost:5432/yhf",
        ),
        ("postgresql://yhf:yhf@localhost:5432/yhf", "postgresql://yhf:yhf@localhost:5432/yhf"),
        (
            "postgresql+psycopg://yhf@db.internal:5432/yhf",
            "postgresql://yhf@db.internal:5432/yhf",
        ),
    ],
)
def test_psycopg_dsn_strips_dialect(database_url: str, expected: str) -> None:
    assert psycopg_dsn(database_url) == expected


def test_psycopg_dsn_rejects_non_url() -> None:
    with pytest.raises(ValueError, match="not a database URL"):
        psycopg_dsn("yhf:yhf@localhost")


def test_save_signals_with_fake_connection() -> None:
    """The repository round-trips through any connection factory, offline."""
    executed: list[tuple[str, Sequence[tuple[Any, ...]]]] = []

    class FakeCursor:
        def __enter__(self) -> "FakeCursor":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def executemany(self, sql: str, rows: Sequence[tuple[Any, ...]]) -> None:
            executed.append((sql, list(rows)))

    class FakeConnection:
        committed = 0

        def __enter__(self) -> "FakeConnection":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def cursor(self) -> FakeCursor:
            return FakeCursor()

        def commit(self) -> None:
            FakeConnection.committed += 1

    repo = PostgresSignalRepository(
        "postgresql://yhf:yhf@localhost:5432/yhf", connect=FakeConnection
    )
    count = repo.save_signals(RUN_ID, RUN_DATE, [make_signal(), make_signal()])

    assert count == 2
    assert len(executed) == 1
    sql, rows = executed[0]
    assert sql == SIGNAL_INSERT_SQL
    assert len(rows) == 2
    assert FakeConnection.committed == 1


def test_save_empty_is_noop() -> None:
    """An empty run writes nothing and never opens a connection."""

    def never_connect() -> RepositoryConnection:
        raise AssertionError("connection must not be opened for an empty save")

    repo = PostgresSignalRepository(
        "postgresql://yhf:yhf@localhost:5432/yhf", connect=never_connect
    )
    assert repo.save_signals(RUN_ID, RUN_DATE, []) == 0
    assert repo.save_score_records(RUN_ID, RUN_DATE, []) == 0
