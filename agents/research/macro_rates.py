"""Macro & Rates analyst — tier 2, market-wide signals from FRED data.

Collects the configured FRED series (core/config.py ``FRED_SERIES``), derives
per-series latest/previous/change summaries from the actual observed values,
and makes one Sonnet-family call that classifies the macro regime. Emits
market-wide signals — ``ticker`` is always ``None`` — e.g. an inverted 2s10s
curve or a disinflation trend, with strength/confidence grounded in the
observed values (latest DGS2 vs DGS10 spread, not vibes).
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Final

from core.config import Settings
from core.schemas import Signal
from data.connectors.base import ConnectorError
from data.connectors.fred import FredConnector, FredObservation

from agents.base import (
    AnalystSignalPayload,
    BaseAgent,
    LLMClient,
    compact_json,
)

logger = logging.getLogger(__name__)

# How far back to fetch each series: enough for "recent deltas" without
# hauling years of history into the prompt.
DEFAULT_LOOKBACK_DAYS: Final[int] = 90


def latest_observation(observations: list[FredObservation]) -> FredObservation | None:
    """The most recent non-missing observation (FRED rows are chronological)."""
    for row in reversed(observations):
        if row["value"] is not None:
            return row
    return None


def previous_observation(observations: list[FredObservation]) -> FredObservation | None:
    """The second-most-recent non-missing observation."""
    seen_latest = False
    for row in reversed(observations):
        if row["value"] is None:
            continue
        if seen_latest:
            return row
        seen_latest = True
    return None


def series_summary(rows: list[FredObservation]) -> dict[str, Any] | None:
    """Latest, previous, and change for one series — None when no real data."""
    latest = latest_observation(rows)
    if latest is None:
        return None
    previous = previous_observation(rows)
    change: float | None = None
    if previous is not None and previous["value"] is not None and latest["value"] is not None:
        change = round(latest["value"] - previous["value"], 4)
    return {
        "latest": {"date": latest["date"], "value": latest["value"]},
        "previous": {"date": previous["date"], "value": previous["value"]} if previous else None,
        "change": change,
    }


def yield_curve_spread(dgs2: float | None, dgs10: float | None) -> float | None:
    """The 2s10s spread (DGS10 - DGS2) from the two observed values, or None."""
    if dgs2 is None or dgs10 is None:
        return None
    return round(dgs10 - dgs2, 4)


class MacroRatesAgent(BaseAgent):
    """Tier 2: classifies the macro regime from FRED; ticker is always None."""

    name: ClassVar[str] = "macro_rates"
    prompt_filename: ClassVar[str] = "macro_rates_v1.md"

    def __init__(
        self,
        *,
        llm: LLMClient,
        settings: Settings,
        fred: FredConnector,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    ) -> None:
        super().__init__(llm=llm, settings=settings)
        self._fred = fred
        self._lookback_days = lookback_days

    def collect(self, tickers: list[str]) -> dict[str, Any]:
        """Fetch every configured FRED series; per-series failures become gaps.

        One broken series must not take down the regime read: the failure is
        logged with its series id and surfaced to the LLM in ``errors``.
        """
        start = (datetime.now(UTC) - timedelta(days=self._lookback_days)).date().isoformat()
        series: dict[str, list[FredObservation]] = {}
        errors: dict[str, str] = {}
        for series_id in self._fred.configured_series:
            try:
                series[series_id] = self._fred.fetch_observations(
                    series_id, observation_start=start
                )
            except ConnectorError as exc:
                errors[series_id] = str(exc)
                logger.warning(
                    "connector_gap",
                    extra={
                        "agent": self.name,
                        "source": "fred",
                        "series": series_id,
                        "error": str(exc),
                    },
                )
        return {"series": series, "errors": errors}

    def analyze(self, tickers: list[str], data: dict[str, Any]) -> list[Signal]:
        """One regime-classification call over the collected series."""
        series: dict[str, list[FredObservation]] = data.get("series", {})
        summaries = {sid: s for sid, rows in series.items() if (s := series_summary(rows))}
        if not summaries:
            logger.warning(
                "analysis_skipped_no_data",
                extra={"agent": self.name, "reason": "no FRED series returned observations"},
            )
            return []
        dgs2 = summaries.get("DGS2", {}).get("latest", {}).get("value")
        dgs10 = summaries.get("DGS10", {}).get("latest", {}).get("value")
        spread = yield_curve_spread(dgs2, dgs10)
        payload = {
            "instructions": (
                "Classify the macro regime from these FRED observations and emit "
                "market-wide signals per the system prompt."
            ),
            "series_summary": summaries,
            "raw_observations": {sid: rows for sid, rows in series.items() if rows},
            "derived": {"twos_tens_spread": spread},
            "errors": data.get("errors", {}),
        }
        # Market-wide enforcement happens in _materialize: ticker is always None.
        return self.request_signals(compact_json(payload))

    def _materialize(self, payloads: list[AnalystSignalPayload]) -> list[Signal]:
        """Force ticker=None — this agent never emits ticker-level signals."""
        market_wide = [payload.model_copy(update={"ticker": None}) for payload in payloads]
        return super()._materialize(market_wide)


__all__ = [
    "DEFAULT_LOOKBACK_DAYS",
    "MacroRatesAgent",
    "latest_observation",
    "previous_observation",
    "series_summary",
    "yield_curve_spread",
]
