"""Fundamentals agent tests: FMP citations, yfinance fallback, capped confidence."""

import sys
from typing import Any

import httpx
import pytest
import respx
from agents.research.fundamentals import FALLBACK_MAX_CONFIDENCE, FALLBACK_NOTE
from data.connectors.fmp import FMP_BASE_URL

from tests.test_connectors.conftest import FakeClock, load_fixture_json, make_settings

from .conftest import StubLLM, canned_response, last_user_payload, make_fmp_agent

FMP_URL = FMP_BASE_URL  # https://financialmodelingprep.com/api/v3


def mock_fmp_routes() -> None:
    respx.get(f"{FMP_URL}/profile/AAPL").mock(
        return_value=httpx.Response(200, json=load_fixture_json("fmp_profile.json"))
    )
    respx.get(f"{FMP_URL}/ratios/AAPL").mock(
        return_value=httpx.Response(200, json=load_fixture_json("fmp_ratios.json"))
    )
    respx.get(f"{FMP_URL}/income-statement/AAPL").mock(
        return_value=httpx.Response(200, json=load_fixture_json("fmp_income_statement.json"))
    )


class StubYfinanceTicker:
    """Stand-in for yfinance.Ticker returning a canned info dict."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.info: dict[str, Any] = {
            "trailingPE": 31.2,
            "profitMargins": 0.26,
            "revenueGrowth": 0.06,
        }


class StubYfinanceModule:
    """Stand-in for the yfinance module (tests inject it into sys.modules)."""

    Ticker = StubYfinanceTicker


@respx.mock
def test_run_cites_exact_fmp_figures(fake_clock: FakeClock) -> None:
    mock_fmp_routes()
    stub = StubLLM(canned_response("signals_fundamentals.json"))
    agent = make_fmp_agent(fake_clock, None, stub)
    assert agent.fmp_enabled is True

    signals = agent.run(["AAPL"])

    assert len(signals) == 1
    signal = signals[0]
    assert signal.agent == "fundamentals"
    assert signal.tier == 4
    assert signal.ticker == "AAPL"
    # Confidence comes from the model at full weight when FMP is the source.
    assert signal.confidence == 0.9
    assert FALLBACK_NOTE not in signal.risks
    # The user payload carries the provider's figures exactly as returned.
    payload = last_user_payload(stub)
    assert '"mode": "fmp"' in payload
    assert "416160000000" in payload  # revenue, FY2025 fixture value
    assert "0.46" in payload  # grossProfitMargin fixture value


@respx.mock
def test_fmp_disabled_falls_back_to_yfinance_with_capped_confidence(
    fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "yfinance", StubYfinanceModule())
    stub = StubLLM(canned_response("signals_fundamentals.json"))
    agent = make_fmp_agent(fake_clock, None, stub, settings=make_settings(fmp_api_key=None))
    assert agent.fmp_enabled is False

    signals = agent.run(["AAPL"])

    assert len(signals) == 1
    signal = signals[0]
    # Fallback data is trusted less: confidence capped at 0.5 and disclosed.
    assert signal.confidence == FALLBACK_MAX_CONFIDENCE
    assert FALLBACK_NOTE in signal.risks
    # The payload discloses the fallback mode to the model.
    assert '"mode": "yfinance"' in last_user_payload(stub)


def test_yfinance_failure_is_a_gap_not_a_signal(
    fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken fallback is recorded as an error; silence, never a fake signal."""
    monkeypatch.setitem(sys.modules, "yfinance", None)  # import raises ImportError
    stub = StubLLM(canned_response("signals_fundamentals.json"))
    agent = make_fmp_agent(fake_clock, None, stub, settings=make_settings(fmp_api_key=None))

    data = agent.collect(["AAPL"])

    assert data["mode"] == "yfinance"
    assert "yfinance fallback failed" in data["errors"]["AAPL"]
    assert agent.analyze(["AAPL"], data) == []
    assert stub.call_count == 0


@respx.mock
def test_no_fundamentals_means_silence_without_llm_call(fake_clock: FakeClock) -> None:
    respx.get(f"{FMP_URL}/profile/AAPL").mock(return_value=httpx.Response(404, json={}))
    respx.get(f"{FMP_URL}/ratios/AAPL").mock(return_value=httpx.Response(404, json={}))
    respx.get(f"{FMP_URL}/income-statement/AAPL").mock(return_value=httpx.Response(404, json={}))
    stub = StubLLM(canned_response("signals_fundamentals.json"))
    agent = make_fmp_agent(fake_clock, None, stub)

    signals = agent.run(["AAPL"])

    assert signals == []
    assert stub.call_count == 0
