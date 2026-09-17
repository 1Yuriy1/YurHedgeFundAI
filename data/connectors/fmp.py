"""Financial Modeling Prep connector — OPTIONAL, behind FMP_API_KEY.

Degradation contract (spec): when FMP_API_KEY is unset this connector raises
``DisabledConnectorError`` at construction and the Fundamentals analyst falls
back to yfinance with lower-confidence signals — **that agent-side fallback is
stage 4's job, not this module's**; here, "disabled" is a clean, typed error.

The free tier allows **250 requests/day** (research notes §6). The budget is a
persistent Redis counter that decrements per call (date-scoped key), with an
in-process fallback when Redis is unreachable — enforcement survives the cache
being down, only its cross-process persistence degrades. Three endpoints
(profile, ratios, income statement) are supported; one budget unit per request.
"""

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any, Final, TypedDict

import redis
from core.config import Settings
from redis import Redis

from data.connectors.base import (
    BaseConnector,
    ConnectorError,
    DisabledConnectorError,
)

logger = logging.getLogger(__name__)

FMP_BASE_URL: Final[str] = "https://financialmodelingprep.com/api/v3"
FMP_DAILY_REQUEST_LIMIT: Final[int] = 250
# Date-scoped keys self-expire; two days of TTL safely covers UTC rollover.
BUDGET_KEY_TTL_SECONDS: Final[int] = 172800
BUDGET_KEY_TEMPLATE: Final[str] = "yhf:fmp:budget:{day}"


class BudgetExceededError(ConnectorError):
    """The daily request budget for a metered provider is exhausted."""


class FundamentalsBundle(TypedDict):
    """Everything the Fundamentals analyst needs for one symbol."""

    profile: dict[str, Any]
    ratios: list[dict[str, Any]]
    income_statement: list[dict[str, Any]]


class DailyBudget:
    """A persistent per-day request counter backed by Redis.

    Starts at ``limit`` and decrements per ``consume()``; when Redis is
    unreachable the counter degrades to process-local counting (a warning is
    logged) so quota enforcement never silently disappears.
    """

    def __init__(
        self,
        client: Redis | None,
        *,
        limit: int = FMP_DAILY_REQUEST_LIMIT,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._client = client
        self._limit = limit
        # UTC day scoping so the counter rolls over at midnight, not local time.
        self._today = today or _utc_today
        self._local_remaining: int | None = None

    def consume(self) -> None:
        """Take one request from the budget; raise when it is exhausted."""
        remaining = self._decrement()
        if remaining is not None and remaining < 0:
            raise BudgetExceededError(
                f"FMP daily budget of {self._limit} requests exhausted — try again tomorrow"
            )

    def remaining(self) -> int | None:
        """Requests left today, or None when it cannot be determined."""
        key = self._key()
        if self._client is not None:
            try:
                value = self._client.get(key)
                if value is not None:
                    decoded = value.decode() if isinstance(value, bytes) else str(value)
                    return int(decoded)
                return self._limit  # key absent — nothing consumed yet
            except (redis.exceptions.RedisError, OSError) as exc:
                logger.warning("FMP budget read fell back to in-process counter: %s", exc)
        return self._limit if self._local_remaining is None else self._local_remaining

    def _key(self) -> str:
        return BUDGET_KEY_TEMPLATE.format(day=self._today().isoformat())

    def _decrement(self) -> int | None:
        """Decrement the persistent counter; None means local-only mode."""
        key = self._key()
        if self._client is not None:
            try:
                # Seed the key only if absent (SET NX), then decrement — the
                # counter starts at the limit and decreases per call.
                self._client.set(key, self._limit, ex=BUDGET_KEY_TTL_SECONDS, nx=True)
                return int(self._client.decr(key))
            except (redis.exceptions.RedisError, OSError) as exc:
                logger.warning(
                    "FMP budget Redis unavailable; using in-process counter "
                    "(persistence degraded): %s",
                    exc,
                )
        if self._local_remaining is None:
            self._local_remaining = self._limit
        self._local_remaining -= 1
        return self._local_remaining


def _utc_today() -> date:
    """Today's date in UTC — the budget's scoping clock."""
    return datetime.now(UTC).date()


class FmpConnector(BaseConnector):
    """Optional FMP client: profile, ratios, and income statement per symbol."""

    name = "fmp"

    def __init__(
        self, settings: Settings, *, budget: DailyBudget | None = None, **options: Any
    ) -> None:
        api_key = settings.fmp_api_key
        if not api_key:
            raise DisabledConnectorError(
                "FMP_API_KEY is not set — connector disabled; the Fundamentals analyst "
                "falls back to yfinance with lower confidence (agent-side, stage 4)"
            )
        super().__init__(**options)
        self._api_key: str = api_key
        self._budget = budget if budget is not None else DailyBudget(self._redis_client)

    def fetch_profile(self, symbol: str) -> dict[str, Any]:
        """Company profile snapshot; empty dict for an unknown symbol."""
        rows = self._fetch_list(f"profile/{symbol}")
        return rows[0] if rows else {}

    def fetch_ratios(self, symbol: str, *, period: str = "annual") -> list[dict[str, Any]]:
        """Financial ratios history (annual by default)."""
        return self._fetch_list(f"ratios/{symbol}", params={"period": period})

    def fetch_income_statement(
        self, symbol: str, *, period: str = "annual"
    ) -> list[dict[str, Any]]:
        """Income-statement history (annual by default)."""
        return self._fetch_list(f"income-statement/{symbol}", params={"period": period})

    def fetch_fundamentals(self, symbol: str) -> FundamentalsBundle:
        """Profile + ratios + income statement — three budgeted requests."""
        return FundamentalsBundle(
            profile=self.fetch_profile(symbol),
            ratios=self.fetch_ratios(symbol),
            income_statement=self.fetch_income_statement(symbol),
        )

    def _before_network_request(self) -> None:
        """Consume one budget unit per real HTTP attempt (retries included)."""
        self._budget.consume()

    def _fetch_list(self, path: str, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
        """One budgeted GET; the api key never lands in a cache key."""
        request_params = {"apikey": self._api_key, **(params or {})}
        payload = self._get_json(
            f"{FMP_BASE_URL}/{path}",
            params=request_params,
            cache_key_params={k: v for k, v in request_params.items() if k != "apikey"},
        )
        if not isinstance(payload, list):
            raise ConnectorError(f"fmp: unexpected response for {path} (not a JSON array)")
        return [row for row in payload if isinstance(row, dict)]


__all__ = [
    "BUDGET_KEY_TTL_SECONDS",
    "FMP_BASE_URL",
    "FMP_DAILY_REQUEST_LIMIT",
    "BudgetExceededError",
    "DailyBudget",
    "FmpConnector",
    "FundamentalsBundle",
]
