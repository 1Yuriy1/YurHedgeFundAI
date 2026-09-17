"""FRED connector — /fred/series/observations with pinned parsing behavior.

Research notes §4: the observations API returns values as **strings** and
marks missing data as ``"."`` — both are normalized here so downstream code
sees floats or ``None``. ``file_type=json`` is set on every request because
FRED's default response format is XML.
"""

import logging
from typing import Any, Final, TypedDict

from core.config import Settings

from data.connectors.base import BaseConnector, ConnectorError, DisabledConnectorError

logger = logging.getLogger(__name__)

FRED_BASE_URL: Final[str] = "https://api.stlouisfed.org/fred"
MISSING_VALUE_TOKEN: Final[str] = "."
# Six hours: daily macro series move slowly, and FRED's free tier allows
# 120 req/min — the cache exists for politeness, not quota survival.
FRED_CACHE_TTL_SECONDS: Final[float] = 21600.0


class FredObservation(TypedDict):
    """One normalized observation: series id, date, and a float-or-None value."""

    series_id: str
    date: str
    value: float | None


def parse_observations(payload: dict[str, Any], *, series_id: str) -> list[FredObservation]:
    """Normalize FRED's observations array: string values to floats, ``.`` to None."""
    rows = payload.get("observations", [])
    if not isinstance(rows, list):
        return []
    parsed: list[FredObservation] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_value = row.get("value")
        value: float | None
        if isinstance(raw_value, str) and raw_value != MISSING_VALUE_TOKEN:
            try:
                value = float(raw_value)
            except ValueError:
                value = None
        else:
            value = None
        parsed.append(
            FredObservation(series_id=series_id, date=str(row.get("date", "")), value=value)
        )
    return parsed


class FredConnector(BaseConnector):
    """Fetches observations for the configured FRED series (core/config.py)."""

    name = "fred"
    default_cache_ttl = FRED_CACHE_TTL_SECONDS

    def __init__(self, settings: Settings, **options: Any) -> None:
        if not settings.fred_api_key:
            raise DisabledConnectorError(
                "FRED_API_KEY is not set — get a free key at https://fredaccount.stlouisfed.org/apikeys"
            )
        self._api_key: str = settings.fred_api_key
        self._series: list[str] = list(settings.fred_series)
        super().__init__(**options)

    @property
    def configured_series(self) -> list[str]:
        """The series list this connector was configured with (spec default:
        UNRATE, FEDFUNDS, DGS10, DGS2, T10Y2Y, BAMLH0A0HYM2, CPIAUCSL, PCEPI)."""
        return list(self._series)

    def fetch_observations(
        self,
        series_id: str,
        *,
        observation_start: str | None = None,
        observation_end: str | None = None,
    ) -> list[FredObservation]:
        """Fetch one series; window bounds are YYYY-MM-DD or None for open."""
        params = {"series_id": series_id, "api_key": self._api_key, "file_type": "json"}
        if observation_start is not None:
            params["observation_start"] = observation_start
        if observation_end is not None:
            params["observation_end"] = observation_end
        payload = self._get_json(
            f"{FRED_BASE_URL}/series/observations",
            params=params,
            cache_key_params={k: v for k, v in params.items() if k != "api_key"},
        )
        if not isinstance(payload, dict):
            raise ConnectorError(f"fred: unexpected response for {series_id} (not a JSON object)")
        if "error_code" in payload:
            # FRED signals bad keys/params as HTTP 200 + an error envelope.
            raise ConnectorError(
                f"fred: API error {payload.get('error_code')}: {payload.get('error_message', '')}"
            )
        return parse_observations(payload, series_id=series_id)

    def fetch_configured(
        self,
        *,
        observation_start: str | None = None,
        observation_end: str | None = None,
    ) -> dict[str, list[FredObservation]]:
        """Fetch every configured series into one mapping (one request each)."""
        return {
            series_id: self.fetch_observations(
                series_id,
                observation_start=observation_start,
                observation_end=observation_end,
            )
            for series_id in self._series
        }


__all__ = [
    "FRED_BASE_URL",
    "FRED_CACHE_TTL_SECONDS",
    "FredConnector",
    "FredObservation",
    "parse_observations",
]
