"""Provider connectors (FRED, SEC EDGAR, GDELT, optional FMP) — thin HTTP clients only.

The shared plumbing lives in ``base`` (HTTP, Redis caching, rate limiting,
backoff); each provider module adds parsing and its API's pinned behaviors.
"""

from data.connectors.base import (
    BaseConnector,
    Clock,
    ConnectorError,
    DisabledConnectorError,
    RateLimiter,
    RedisCache,
    Sleeper,
    redis_client_from_url,
)
from data.connectors.edgar import EdgarConnector
from data.connectors.fmp import BudgetExceededError, DailyBudget, FmpConnector
from data.connectors.fred import FredConnector
from data.connectors.gdelt import GdeltConnector

__all__ = [
    "BaseConnector",
    "BudgetExceededError",
    "Clock",
    "ConnectorError",
    "DailyBudget",
    "DisabledConnectorError",
    "EdgarConnector",
    "FmpConnector",
    "FredConnector",
    "GdeltConnector",
    "RateLimiter",
    "RedisCache",
    "Sleeper",
    "redis_client_from_url",
]
