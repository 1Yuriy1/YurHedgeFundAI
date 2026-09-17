"""FMP connector tests: optional key, persistent budget, three endpoints."""

from datetime import date
from typing import Any, cast

import httpx
import pytest
import redis
import respx
from data.connectors.base import ConnectorError, DisabledConnectorError
from data.connectors.fmp import FMP_BASE_URL, BudgetExceededError, DailyBudget, FmpConnector
from fakeredis import FakeRedis
from redis import Redis

from .conftest import FakeClock, load_fixture_json, make_settings

FIXED_DAY = date(2026, 9, 17)
BUDGET_KEY = f"yhf:fmp:budget:{FIXED_DAY.isoformat()}"


def make_connector(
    fake_clock: FakeClock,
    fake_redis: Redis | None,
    **options: Any,
) -> FmpConnector:
    return FmpConnector(
        make_settings(fmp_api_key="test-fmp-key"),
        redis_client=fake_redis,
        clock=fake_clock.now,
        sleeper=fake_clock.sleep,
        **options,
    )


def mock_fundamentals_routes() -> None:
    respx.get(f"{FMP_BASE_URL}/profile/AAPL").mock(
        return_value=httpx.Response(200, json=load_fixture_json("fmp_profile.json"))
    )
    respx.get(f"{FMP_BASE_URL}/ratios/AAPL").mock(
        return_value=httpx.Response(200, json=load_fixture_json("fmp_ratios.json"))
    )
    respx.get(f"{FMP_BASE_URL}/income-statement/AAPL").mock(
        return_value=httpx.Response(200, json=load_fixture_json("fmp_income_statement.json"))
    )


def test_disabled_without_api_key() -> None:
    """No FMP_API_KEY → clean typed failure; the agent side falls back (stage 4)."""
    with pytest.raises(DisabledConnectorError):
        FmpConnector(make_settings(fmp_api_key=None))


@respx.mock
def test_fetches_profile_ratios_and_income_statement(
    fake_clock: FakeClock, fake_redis: FakeRedis
) -> None:
    mock_fundamentals_routes()
    connector = make_connector(fake_clock, fake_redis)

    bundle = connector.fetch_fundamentals("AAPL")

    assert bundle["profile"]["companyName"] == "Apple Inc."
    assert bundle["profile"]["sector"] == "Technology"
    assert [row["date"] for row in bundle["ratios"]] == ["2025-09-27", "2024-09-28"]
    assert bundle["income_statement"][0]["revenue"] == 416160000000

    params = respx.calls[-1].request.url.params
    assert params["apikey"] == "test-fmp-key"
    for key in fake_redis.keys("yhf:cache:*"):
        assert b"test-fmp-key" not in key  # the api key never lands in a cache key


@respx.mock
def test_budget_counter_decrements_per_call_and_cache_hits_are_free(
    fake_clock: FakeClock, fake_redis: FakeRedis
) -> None:
    """250/day budget: 3 network calls → 247 left; a cached refetch costs nothing."""
    mock_fundamentals_routes()
    budget = DailyBudget(fake_redis, today=lambda: FIXED_DAY)
    connector = make_connector(fake_clock, fake_redis, budget=budget)

    connector.fetch_fundamentals("AAPL")
    assert fake_redis.get(BUDGET_KEY) == b"247"

    connector.fetch_fundamentals("AAPL")  # fully cached this time
    assert fake_redis.get(BUDGET_KEY) == b"247"


def test_budget_exhaustion_raises(fake_redis: FakeRedis) -> None:
    budget = DailyBudget(fake_redis, limit=2, today=lambda: FIXED_DAY)

    budget.consume()
    budget.consume()
    assert budget.remaining() == 0
    with pytest.raises(BudgetExceededError):
        budget.consume()


def test_budget_resets_next_day(fake_redis: FakeRedis) -> None:
    day = {"value": FIXED_DAY}
    budget = DailyBudget(fake_redis, limit=1, today=lambda: day["value"])

    budget.consume()
    with pytest.raises(BudgetExceededError):
        budget.consume()

    day["value"] = date(2026, 9, 18)  # UTC midnight rollover
    budget.consume()  # fresh day-scoped key — allowed again


def test_budget_survives_redis_outage(fake_clock: FakeClock) -> None:
    """Redis down → in-process counting; quota enforcement never disappears."""

    class DownRedis:
        def get(self, key: str) -> None:
            raise redis.ConnectionError("redis is down")

        def set(self, *args: Any, **kwargs: Any) -> None:
            raise redis.ConnectionError("redis is down")

        def decr(self, key: str) -> None:
            raise redis.ConnectionError("redis is down")

    budget = DailyBudget(cast("Redis", DownRedis()), limit=2, today=lambda: FIXED_DAY)
    connector = make_connector(fake_clock, cast("Redis", DownRedis()), budget=budget)

    budget.consume()
    budget.consume()
    assert budget.remaining() == 0
    with pytest.raises(BudgetExceededError):
        budget.consume()
    with pytest.raises(BudgetExceededError):
        connector.fetch_profile("AAPL")  # fetches refuse to dial without budget


@respx.mock
def test_non_array_payload_raises_connector_error(fake_clock: FakeClock) -> None:
    """FMP error bodies are JSON objects, not arrays — surface a typed error."""
    respx.get(f"{FMP_BASE_URL}/profile/NOPE").mock(
        return_value=httpx.Response(200, json={"Error Message": "Invalid API KEY. Please retry."})
    )
    connector = make_connector(fake_clock, None)

    with pytest.raises(ConnectorError, match="not a JSON array"):
        connector.fetch_profile("NOPE")
