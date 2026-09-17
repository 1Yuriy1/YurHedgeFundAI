"""tests for core/schemas.py — the evidence-mandatory Signal schema."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Final

import pytest
from core.schemas import Direction, Evidence, Signal
from pydantic import ValidationError

# The Master Plan's shared signal schema example (p.8), with a concrete SEC
# URL substituted for the plan's "..." placeholder. Numbers are the plan's.
PLAN_SIGNAL_JSON: Final[dict[str, Any]] = {
    "agent": "sec_filings",
    "tier": 3,
    "timestamp": "2026-09-17T13:30:00Z",
    "ticker": "XYZ",
    "signal": "bullish",
    "strength": 0.72,
    "confidence": 0.65,
    "horizon": "1-3 months",
    "red_flag": False,
    "thesis": "Cluster of 4 insider purchases totaling $3.1M after earnings",
    "evidence": [
        {
            "source": "Form 4",
            "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=4",
            "excerpt": "Cluster of insider purchases totaling $3.1M after earnings",
        }
    ],
    "risks": ["Buying may be pre-planned"],
}


def make_signal(**overrides: Any) -> Signal:
    """The plan's example signal with any fields overridden."""
    payload: dict[str, Any] = {**PLAN_SIGNAL_JSON}
    payload.update(overrides)
    return Signal.model_validate(payload)


def test_plan_example_signal_parses() -> None:
    """Every field of the plan's p.8 JSON schema round-trips into the model."""
    signal = make_signal()

    assert signal.agent == "sec_filings"
    assert signal.tier == 3
    assert signal.ticker == "XYZ"
    assert signal.signal is Direction.BULLISH
    assert signal.strength == 0.72
    assert signal.confidence == 0.65
    assert signal.horizon == "1-3 months"
    assert signal.red_flag is False
    assert signal.thesis == "Cluster of 4 insider purchases totaling $3.1M after earnings"
    assert signal.risks == ["Buying may be pre-planned"]
    assert signal.evidence[0].source == "Form 4"
    assert signal.timestamp.tzinfo is UTC
    assert signal.timestamp == datetime(2026, 9, 17, 13, 30, tzinfo=UTC)


def test_signal_dict_and_json_round_trips() -> None:
    """Model -> dict -> model and model -> JSON string -> model both preserve it."""
    signal = make_signal()

    assert Signal.model_validate(signal.model_dump()) == signal
    assert Signal.model_validate_json(signal.model_dump_json()) == signal


def test_direction_accepts_enum_and_plain_string() -> None:
    """Both Direction members and their string values validate."""
    assert make_signal(signal="bearish").signal is Direction.BEARISH
    assert make_signal(signal=Direction.NEUTRAL).signal is Direction.NEUTRAL
    with pytest.raises(ValidationError):
        make_signal(signal="sideways")


def test_empty_evidence_rejected() -> None:
    """A signal with no evidence at all is invalid — evidence or it doesn't exist."""
    with pytest.raises(ValidationError, match="evidence"):
        make_signal(evidence=[])


@pytest.mark.parametrize("bad_url", ["", "   ", "www.sec.gov/form4", "ftp://sec.gov/form4"])
def test_url_less_evidence_rejected(bad_url: str) -> None:
    """Evidence items must carry http(s) URLs; anything else is rejected."""
    with pytest.raises(ValidationError, match="evidence must carry a URL"):
        make_signal(evidence=[{"source": "Form 4", "url": bad_url, "excerpt": "..."}])


def test_http_and_https_urls_accepted() -> None:
    """Both schemes are valid evidence URLs."""
    make_signal(evidence=[{"source": "FRED", "url": "http://fred.stlouisfed.org", "excerpt": "x"}])
    make_signal(evidence=[{"source": "FRED", "url": "https://fred.stlouisfed.org", "excerpt": "x"}])


def test_naive_timestamp_coerced_to_utc() -> None:
    """A naive datetime is interpreted as UTC, not local time."""
    signal = make_signal(timestamp=datetime(2026, 9, 17, 13, 30))

    assert signal.timestamp.tzinfo is UTC
    assert signal.timestamp == datetime(2026, 9, 17, 13, 30, tzinfo=UTC)


def test_aware_timestamp_converted_to_utc() -> None:
    """A tz-aware datetime in another zone is converted, not just stamped."""
    offset_tz = timezone(timedelta(hours=2))
    signal = make_signal(timestamp=datetime(2026, 9, 17, 15, 30, tzinfo=offset_tz))

    assert signal.timestamp.tzinfo is UTC
    assert signal.timestamp == datetime(2026, 9, 17, 13, 30, tzinfo=UTC)


@pytest.mark.parametrize("field", ["strength", "confidence"])
@pytest.mark.parametrize("bad_value", [-0.01, 1.01])
def test_strength_and_confidence_bounded(field: str, bad_value: float) -> None:
    """strength and confidence are both 0..1."""
    with pytest.raises(ValidationError):
        make_signal(**{field: bad_value})
    make_signal(**{field: 0.0})
    make_signal(**{field: 1.0})


def test_ticker_optional_for_market_wide_signals() -> None:
    """Macro-style signals omit the ticker; it defaults to None."""
    signal = make_signal(ticker=None)

    assert signal.ticker is None


def test_missing_required_fields_rejected() -> None:
    """Required plan fields (thesis, agent, signal, horizon) cannot be dropped."""
    for required in ("agent", "signal", "thesis", "horizon", "timestamp", "evidence"):
        payload = {k: v for k, v in PLAN_SIGNAL_JSON.items() if k != required}
        with pytest.raises(ValidationError):
            Signal.model_validate(payload)


def test_risks_default_is_not_shared() -> None:
    """Mutable default must not leak state between instances."""
    first = make_signal()
    second = make_signal()

    first.risks.append("new risk")
    assert second.risks == ["Buying may be pre-planned"]


def test_evidence_model_is_strictly_typed() -> None:
    """Evidence requires all three fields."""
    with pytest.raises(ValidationError):
        Evidence.model_validate({"source": "Form 4", "url": "https://www.sec.gov"})
