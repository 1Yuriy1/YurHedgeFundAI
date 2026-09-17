"""FRED connector tests: string values, '.'-to-None, file_type=json, offline."""

from typing import Any

import httpx
import pytest
import respx
from data.connectors.base import ConnectorError, DisabledConnectorError
from data.connectors.fred import FredConnector
from fakeredis import FakeRedis

from .conftest import FakeClock, load_fixture_json, make_settings

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"


def make_connector(fake_clock: FakeClock, fake_redis: FakeRedis, **options: Any) -> FredConnector:
    return FredConnector(
        make_settings(),
        redis_client=fake_redis,
        clock=fake_clock.now,
        sleeper=fake_clock.sleep,
        **options,
    )


def test_disabled_without_api_key() -> None:
    """FRED is pinned as required — a missing key is a configuration error."""
    with pytest.raises(DisabledConnectorError):
        FredConnector(make_settings(fred_api_key=None))


@respx.mock
def test_parses_string_values_and_maps_dot_to_none(
    fake_clock: FakeClock, fake_redis: FakeRedis
) -> None:
    route = respx.get(FRED_URL).mock(
        return_value=httpx.Response(200, json=load_fixture_json("fred_observations.json"))
    )
    connector = make_connector(fake_clock, fake_redis)

    rows = connector.fetch_observations("UNRATE")

    assert route.call_count == 1
    assert [(row["date"], row["value"]) for row in rows] == [
        ("2026-07-01", 4.2),
        ("2026-08-01", None),  # '.' is a missing observation, never 0.0
        ("2026-09-01", 4.1),
    ]
    assert all(row["series_id"] == "UNRATE" for row in rows)


@respx.mock
def test_always_sends_file_type_json(fake_clock: FakeClock, fake_redis: FakeRedis) -> None:
    """file_type=json is mandatory even when the caller passes extra params."""
    route = respx.get(FRED_URL).mock(return_value=httpx.Response(200, json={"observations": []}))
    connector = make_connector(fake_clock, fake_redis)

    connector.fetch_observations("UNRATE", observation_start="2020-01-01")

    params = route.calls[-1].request.url.params
    assert params["file_type"] == "json"
    assert params["series_id"] == "UNRATE"
    assert params["observation_start"] == "2020-01-01"
    assert params["api_key"] == "test-fred-key"


@respx.mock
def test_api_key_never_enters_cache_keys(fake_clock: FakeClock, fake_redis: FakeRedis) -> None:
    route = respx.get(FRED_URL).mock(
        return_value=httpx.Response(200, json=load_fixture_json("fred_observations.json"))
    )
    connector = make_connector(fake_clock, fake_redis)
    connector.fetch_observations("UNRATE")

    for key in fake_redis.keys("yhf:cache:*"):
        assert b"test-fred-key" not in key
    assert route.call_count == 1


@respx.mock
def test_fetches_every_configured_series(fake_clock: FakeClock, fake_redis: FakeRedis) -> None:
    """The config series list drives one observations call per series."""
    route = respx.get(FRED_URL).mock(
        return_value=httpx.Response(200, json=load_fixture_json("fred_observations.json"))
    )
    connector = FredConnector(
        make_settings(fred_series=["A", "B"]),
        redis_client=fake_redis,
        clock=fake_clock.now,
        sleeper=fake_clock.sleep,
    )

    result = connector.fetch_configured()

    assert connector.configured_series == ["A", "B"]
    assert set(result) == {"A", "B"}
    assert result["A"][0]["value"] == 4.2
    assert route.call_count == 2


@respx.mock
def test_error_envelope_raises_connector_error(fake_clock: FakeClock) -> None:
    """FRED reports problems as 200-JSON error envelopes — they must surface."""
    respx.get(FRED_URL).mock(
        return_value=httpx.Response(200, json={"error_code": 400, "error_message": "bad api key"})
    )
    connector = make_connector(fake_clock, FakeRedis())

    with pytest.raises(ConnectorError, match="bad api key"):
        connector.fetch_observations("UNRATE")


@respx.mock
def test_429_retries_with_backoff(fake_clock: FakeClock) -> None:
    respx.get(FRED_URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json=load_fixture_json("fred_observations.json")),
        ]
    )
    connector = make_connector(fake_clock, FakeRedis())

    assert len(connector.fetch_observations("UNRATE")) == 3
    assert len(fake_clock.sleeps) == 1
