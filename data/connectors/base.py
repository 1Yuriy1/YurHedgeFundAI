"""Shared connector plumbing — HTTP, caching, rate limiting, and backoff.

Every provider connector (FRED, EDGAR, GDELT, FMP) rides on BaseConnector so
each module stays focused on its API's shape instead of transport mechanics:

* one shared ``httpx.Client`` per connector (injectable transport for tests),
* Redis-backed response caching with a per-connector TTL — an unreachable
  Redis must never fail a fetch, only make it slower (degrade to no cache),
* exponential backoff with full jitter on 429/403/timeouts — the research
  notes' guidance is to back off rather than hammer a throttling provider,
* a per-connector client-side rate limiter (EDGAR ≤5 req/s, GDELT 5 s
  pacing) with an injectable clock so tests can assert pacing without
  actually sleeping.

All I/O boundaries take injected dependencies (clock, sleeper, redis client)
so the whole layer runs offline under fakeredis + respx in CI.
"""

import hashlib
import json
import logging
import random
import time
from collections.abc import Callable
from types import TracebackType
from typing import Any, ClassVar, Final, Self

import httpx
import redis
from redis import Redis

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
DEFAULT_MAX_RETRIES: Final[int] = 3
BACKOFF_BASE_SECONDS: Final[float] = 1.0
BACKOFF_CAP_SECONDS: Final[float] = 30.0

# Statuses we retry with backoff: 429/403 pinned by the spec, plus the
# transient 5xx codes (gateway flakiness) that carry no request-specific fault.
RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({403, 429, 500, 502, 503, 504})

# Polite default; EDGAR overrides this with the env-configured User-Agent.
DEFAULT_USER_AGENT: Final[str] = "YurHedgeFundAI/0.1 (research connectors)"

CACHE_KEY_PREFIX: Final[str] = "yhf:cache"

type Clock = Callable[[], float]
type Sleeper = Callable[[float], None]


class ConnectorError(RuntimeError):
    """A connector could not complete its fetch."""


class DisabledConnectorError(ConnectorError):
    """An optional/unconfigured connector was used without required settings."""


class RateLimiter:
    """Serializes calls to at most one per ``min_interval`` seconds.

    ``jitter`` adds a random 0..jitter seconds to every wait so several
    connectors started together don't tick in lockstep. Clock and sleeper are
    injected: tests pass a fake clock that advances on sleep and assert
    pacing without waiting wall-clock time.
    """

    def __init__(
        self,
        min_interval: float = 0.0,
        jitter: float = 0.0,
        *,
        clock: Clock = time.monotonic,
        sleeper: Sleeper = time.sleep,
    ) -> None:
        if min_interval < 0:
            raise ValueError("min_interval must be >= 0")
        if jitter < 0:
            raise ValueError("jitter must be >= 0")
        self._min_interval = min_interval
        self._jitter = jitter
        self._clock = clock
        self._sleeper = sleeper
        self._next_allowed = float("-inf")

    def acquire(self) -> None:
        """Block (via the injected sleeper) until the next call is allowed."""
        wait = max(0.0, self._next_allowed - self._clock())
        if self._jitter:
            wait += random.uniform(0, self._jitter)
        if wait > 0:
            self._sleeper(wait)
        self._next_allowed = self._clock() + self._min_interval


class RedisCache:
    """Redis-backed text cache that degrades to a no-op when Redis is down.

    Every operation is wrapped defensively: a cache miss or write failure is
    logged and swallowed so a fetch's outcome depends only on the provider,
    never on the cache. Works with any ``redis.Redis``-compatible client,
    including ``fakeredis.FakeRedis`` in tests; ``None`` disables caching.
    """

    def __init__(self, client: Redis | None) -> None:
        self._client = client

    @property
    def enabled(self) -> bool:
        """False when no client was provided — caching is fully disabled."""
        return self._client is not None

    def get(self, key: str) -> str | None:
        """Return the cached text for ``key``, or None on miss/failure."""
        if self._client is None:
            return None
        try:
            value = self._client.get(key)
        except (redis.exceptions.RedisError, OSError) as exc:
            logger.warning("cache read failed; continuing without cache: %s", exc)
            return None
        if value is None:
            return None
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)

    def set(self, key: str, value: str, ttl_seconds: float) -> None:
        """Cache ``value`` under ``key``; failures are logged and swallowed."""
        if self._client is None:
            return
        try:
            self._client.set(key, value, ex=max(1, int(ttl_seconds)))
        except (redis.exceptions.RedisError, OSError) as exc:
            logger.warning("cache write failed; continuing without cache: %s", exc)


class BaseConnector:
    """HTTP plumbing shared by all provider connectors.

    Subclasses set ``name`` and the class-level defaults below, then add
    domain methods that call ``_get_json``/``_get_text`` and parse. All
    constructor dependencies are injectable for offline tests.
    """

    name: ClassVar[str] = "connector"
    # Per-connector defaults; override via constructor per instance.
    default_cache_ttl: ClassVar[float] = 3600.0
    min_request_interval: ClassVar[float] = 0.0
    request_jitter: ClassVar[float] = 0.0

    def __init__(
        self,
        *,
        redis_client: Redis | None = None,
        cache: RedisCache | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        cache_ttl: float | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = BACKOFF_BASE_SECONDS,
        backoff_cap: float = BACKOFF_CAP_SECONDS,
        rate_limiter: RateLimiter | None = None,
        clock: Clock = time.monotonic,
        sleeper: Sleeper = time.sleep,
    ) -> None:
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout,
            headers=self._default_headers(),
            follow_redirects=True,
        )
        self._cache = cache if cache is not None else RedisCache(redis_client)
        self._redis_client = redis_client
        self._sleeper = sleeper
        self._cache_ttl = self.default_cache_ttl if cache_ttl is None else cache_ttl
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._rate_limiter = rate_limiter or RateLimiter(
            self.min_request_interval,
            self.request_jitter,
            clock=clock,
            sleeper=sleeper,
        )

    def _default_headers(self) -> dict[str, str]:
        """Headers every request carries; subclasses extend (e.g. EDGAR's UA)."""
        return {"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"}

    def _get_json(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        cache_key_params: dict[str, str] | None = None,
    ) -> Any:
        """GET ``url`` and parse the body as JSON (network, cache, retries)."""
        return json.loads(self._get_text(url, params=params, cache_key_params=cache_key_params))

    def _get_text(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        cache_key_params: dict[str, str] | None = None,
    ) -> str:
        """GET ``url``, serving repeated calls from the Redis cache.

        ``cache_key_params`` replaces ``params`` when building the cache key —
        used to keep secrets (API keys) out of stored keys.
        """
        key = self._cache_key(url, params if cache_key_params is None else cache_key_params)
        if key is not None:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
        text = self._send_with_retry(url, params)
        if key is not None:
            self._cache.set(key, text, self._cache_ttl)
        return text

    def _cache_key(self, url: str, params: dict[str, str] | None) -> str | None:
        """Cache key for this request, or None when caching is disabled."""
        if not self._cache.enabled:
            return None
        digest_input = json.dumps([self.name, url, sorted((params or {}).items())])
        return f"{CACHE_KEY_PREFIX}:{self.name}:{hashlib.sha256(digest_input.encode()).hexdigest()}"

    def _send_with_retry(self, url: str, params: dict[str, str] | None) -> str:
        """GET with the rate limiter, retrying 429/403/timeouts with backoff."""
        last_problem = ""
        for attempt in range(self._max_retries + 1):
            self._rate_limiter.acquire()
            self._before_network_request()
            try:
                response = self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                if attempt == self._max_retries:
                    raise ConnectorError(
                        f"{self.name}: transport failure after {attempt + 1} attempts: {exc}"
                    ) from exc
                last_problem = f"transport error: {exc}"
                self._sleep_backoff(attempt, last_problem)
                continue
            if response.status_code < 400:
                return response.text
            if response.status_code not in RETRYABLE_STATUS_CODES:
                raise ConnectorError(
                    f"{self.name}: unexpected HTTP {response.status_code} from {url}"
                )
            if attempt == self._max_retries:
                raise ConnectorError(
                    f"{self.name}: HTTP {response.status_code} after {attempt + 1} "
                    f"attempts from {url}"
                )
            last_problem = f"HTTP {response.status_code}"
            self._sleep_backoff(attempt, last_problem)
        raise ConnectorError(f"{self.name}: exhausted retries ({last_problem})")  # pragma: no cover

    def _before_network_request(self) -> None:
        """Hook run before every real HTTP attempt (retries included).

        Metered connectors consume their budget here — cache hits never cost
        quota because they never reach the network.
        """

    def _sleep_backoff(self, attempt: int, problem: str) -> None:
        """Exponential backoff with full jitter: uniform(0, base * 2**attempt)."""
        ceiling = min(self._backoff_cap, self._backoff_base * 2**attempt)
        delay = random.uniform(0, ceiling)
        logger.warning(
            "%s: retrying in %.2fs (attempt %d): %s", self.name, delay, attempt + 1, problem
        )
        self._sleeper(delay)

    def close(self) -> None:
        """Release the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def redis_client_from_url(url: str) -> Redis:
    """Build a redis client with bounded timeouts.

    Short socket timeouts matter: the cache must never add real latency to a
    fetch when Redis is unreachable — connections fail fast into the
    RedisCache no-op paths instead of hanging.
    """
    return Redis.from_url(
        url,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )


__all__ = [
    "BACKOFF_BASE_SECONDS",
    "BACKOFF_CAP_SECONDS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_TIMEOUT_SECONDS",
    "RETRYABLE_STATUS_CODES",
    "BaseConnector",
    "Clock",
    "ConnectorError",
    "DisabledConnectorError",
    "RateLimiter",
    "RedisCache",
    "Sleeper",
    "redis_client_from_url",
]
