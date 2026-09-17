"""Scaffold sanity checks; component suites arrive with later stages."""

from typing import Final

from core.config import DEFAULT_UNIVERSE, Settings

SPEC_UNIVERSE: Final = (
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


def test_default_universe_matches_spec() -> None:
    """The default universe is the spec's ten starter mega-caps, in order."""
    assert DEFAULT_UNIVERSE == SPEC_UNIVERSE


def test_universe_is_env_overridable_as_csv() -> None:
    """UNIVERSE arrives from the environment as a CSV string and is normalized."""
    settings = Settings(universe=" aapl,tsla ")
    assert settings.universe == ["AAPL", "TSLA"]
