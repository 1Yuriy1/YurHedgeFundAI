"""Fundamentals analyst — tier 4, valuation and business quality.

Uses the FMP connector when it is enabled; when FMP is disabled (no
``FMP_API_KEY``), falls back to yfinance with confidence capped lower
(≤ 0.5) and an explicit note on every emitted signal. The LLM payload
carries the provider's figures exactly as returned — P/E, margins, revenue
growth — so every cited number traces to the data, never to memory.
"""

import logging
from typing import Any, ClassVar, Final

from core.config import Settings
from core.schemas import Signal
from data.connectors.base import ConnectorError
from data.connectors.fmp import FmpConnector

from agents.base import BaseAgent, LLMClient, compact_json

logger = logging.getLogger(__name__)

# Fallback data is less reliable than the paid provider — confidence is
# capped and the fallback is disclosed on the signal itself.
FALLBACK_MAX_CONFIDENCE: Final[float] = 0.5
FALLBACK_NOTE: Final[str] = (
    "fundamentals sourced via yfinance fallback (FMP disabled); "
    f"confidence capped at {FALLBACK_MAX_CONFIDENCE}"
)


def yfinance_info(symbol: str) -> dict[str, Any]:
    """One ticker's info dict via yfinance, imported lazily.

    yfinance is an optional runtime dependency (pyproject extra
    ``yfinance``); tests stub it in ``sys.modules``. Kept module-level so
    the fallback path is patchable and the import cost is paid only when
    FMP is disabled.
    """
    import yfinance  # type: ignore[import-not-found]  # noqa: PLC0415 — optional dep, lazy

    info = yfinance.Ticker(symbol).info
    return info if isinstance(info, dict) else {}


class FundamentalsAgent(BaseAgent):
    """Tier 4: FMP scorecards, degrading to yfinance at lower confidence."""

    name: ClassVar[str] = "fundamentals"
    prompt_filename: ClassVar[str] = "fundamentals_v1.md"

    def __init__(
        self,
        *,
        llm: LLMClient,
        settings: Settings,
        fmp: FmpConnector | None,
    ) -> None:
        super().__init__(llm=llm, settings=settings)
        self._fmp = fmp

    @property
    def fmp_enabled(self) -> bool:
        """True when the FMP connector was injected (FMP_API_KEY set)."""
        return self._fmp is not None

    def collect(self, tickers: list[str]) -> dict[str, Any]:
        """Per-ticker fundamentals from FMP, or the yfinance fallback."""
        fundamentals: dict[str, Any] = {}
        errors: dict[str, str] = {}
        if self._fmp is not None:
            mode = "fmp"
            for ticker in tickers:
                try:
                    fundamentals[ticker] = self._fmp.fetch_fundamentals(ticker)
                except ConnectorError as exc:
                    errors[ticker] = str(exc)
                    logger.warning(
                        "connector_gap",
                        extra={
                            "agent": self.name,
                            "source": "fmp",
                            "ticker": ticker,
                            "error": str(exc),
                        },
                    )
        else:
            mode = "yfinance"
            for ticker in tickers:
                try:
                    fundamentals[ticker] = {"info": yfinance_info(ticker)}
                except Exception as exc:  # noqa: BLE001 — any failure is a gap, never a signal
                    errors[ticker] = f"yfinance fallback failed: {exc}"
                    logger.warning(
                        "connector_gap",
                        extra={
                            "agent": self.name,
                            "source": "yfinance",
                            "ticker": ticker,
                            "error": str(exc),
                        },
                    )
        return {"mode": mode, "fundamentals": fundamentals, "errors": errors}

    def analyze(self, tickers: list[str], data: dict[str, Any]) -> list[Signal]:
        """One scorecard call over the collected fundamentals."""
        mode: str = data.get("mode", "fmp")
        fundamentals: dict[str, Any] = data.get("fundamentals", {})
        if not fundamentals:
            logger.warning(
                "analysis_skipped_no_data",
                extra={"agent": self.name, "reason": "no fundamentals collected"},
            )
            return []
        payload = {
            "instructions": (
                "Build a per-ticker scorecard (value, growth, margin trend, leverage) from "
                "these figures and emit signals only where something material moved, per "
                "the system prompt. Cite figures exactly as returned."
            ),
            "mode": mode,
            "fundamentals": {
                ticker: fundamentals[ticker] for ticker in tickers if ticker in fundamentals
            },
            "errors": data.get("errors", {}),
        }
        signals = self.request_signals(compact_json(payload))
        if mode == "yfinance":
            return [self._apply_fallback_limits(signal) for signal in signals]
        return signals

    def _apply_fallback_limits(self, signal: Signal) -> Signal:
        """Cap confidence and disclose the fallback on a yfinance signal."""
        update: dict[str, Any] = {"risks": [*signal.risks, FALLBACK_NOTE]}
        if signal.confidence > FALLBACK_MAX_CONFIDENCE:
            update["confidence"] = FALLBACK_MAX_CONFIDENCE
        return signal.model_copy(update=update)


__all__ = [
    "FALLBACK_MAX_CONFIDENCE",
    "FALLBACK_NOTE",
    "FundamentalsAgent",
    "yfinance_info",
]
