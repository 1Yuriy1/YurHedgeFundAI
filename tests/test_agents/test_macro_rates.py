"""Macro & Rates agent tests: market-wide signals, per-series isolation, silence."""

import json

import httpx
import respx
from fakeredis import FakeRedis

from tests.test_connectors.conftest import FakeClock, load_fixture_json

from .conftest import StubLLM, canned_response, last_user_payload, make_macro_agent

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"


@respx.mock
def test_run_emits_market_wide_signals_with_fred_evidence(
    fake_clock: FakeClock, fake_redis: FakeRedis
) -> None:
    respx.get(FRED_URL).mock(
        return_value=httpx.Response(200, json=load_fixture_json("fred_observations.json"))
    )
    stub = StubLLM(canned_response("signals_macro.json"))
    agent = make_macro_agent(fake_clock, fake_redis, stub)

    signals = agent.run(["AAPL", "MSFT"])

    assert len(signals) == 1
    signal = signals[0]
    assert signal.agent == "macro_rates"
    assert signal.tier == 2
    assert signal.ticker is None  # market-wide, never ticker-scoped
    assert signal.timestamp.tzinfo is not None
    assert signal.evidence, "every signal cites at least one URL-backed source"
    assert all(
        evidence.url.startswith("https://fred.stlouisfed.org/") for evidence in signal.evidence
    )
    assert stub.call_count == 1  # one Sonnet-family call
    assert "URL-backed" in stub.calls[0]["system"]


@respx.mock
def test_payload_carries_series_summaries_with_actual_values(
    fake_clock: FakeClock, fake_redis: FakeRedis
) -> None:
    """The user payload must contain the observed values, not memory."""
    respx.get(FRED_URL).mock(
        return_value=httpx.Response(200, json=load_fixture_json("fred_observations.json"))
    )
    stub = StubLLM(canned_response("signals_macro.json"))
    agent = make_macro_agent(fake_clock, fake_redis, stub)

    agent.run(["AAPL"])

    payload = last_user_payload(stub)
    assert '"DGS2"' in payload and '"UNRATE"' in payload
    assert "4.1" in payload  # latest fixture observation, cited exactly


@respx.mock
def test_forced_ticker_from_llm_is_stripped(fake_clock: FakeClock, fake_redis: FakeRedis) -> None:
    canned = json.loads(canned_response("signals_macro.json"))
    canned[0]["ticker"] = "AAPL"  # the model tries to scope a macro signal
    respx.get(FRED_URL).mock(
        return_value=httpx.Response(200, json=load_fixture_json("fred_observations.json"))
    )
    agent = make_macro_agent(fake_clock, fake_redis, StubLLM(json.dumps(canned)))

    signals = agent.run(["AAPL"])

    assert len(signals) == 1
    assert signals[0].ticker is None


@respx.mock
def test_per_series_failure_is_isolated_not_fatal(
    fake_clock: FakeClock, fake_redis: FakeRedis
) -> None:
    respx.get(FRED_URL, params={"series_id": "UNRATE"}).mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    respx.get(FRED_URL).mock(
        return_value=httpx.Response(200, json=load_fixture_json("fred_observations.json"))
    )
    stub = StubLLM(canned_response("signals_macro.json"))
    agent = make_macro_agent(fake_clock, fake_redis, stub)

    data = agent.collect(["DGS2", "UNRATE"])
    assert data["series"]["DGS2"] is not None
    assert "UNRATE" in data["errors"]  # logged gap, not an exception

    signals = agent.analyze(["DGS2", "UNRATE"], data)
    assert len(signals) == 1  # surviving series still produce signals


@respx.mock
def test_no_data_means_silence_without_llm_call(
    fake_clock: FakeClock, fake_redis: FakeRedis
) -> None:
    respx.get(FRED_URL).mock(
        return_value=httpx.Response(
            200, json={"observations": [{"date": "2026-09-01", "value": "."}]}
        )
    )
    stub = StubLLM(canned_response("signals_macro.json"))
    agent = make_macro_agent(fake_clock, fake_redis, stub)

    signals = agent.run(["AAPL"])

    assert signals == []
    assert stub.call_count == 0  # nothing to analyze — no Sonnet call, no vote
