"""Tests for the shared plumbing: caching, backoff, rate limiting, degradation."""

from typing import Any

import httpx
import pytest
import redis
import respx
from data.connectors.base import BaseConnector, ConnectorError, RedisCache
from fakeredis import FakeRedis

PROBE_URL = "https://probe.example.com/data"


class ProbeConnector(BaseConnector):
    """Minimal concrete connector for exercising the shared plumbing."""

    name = "probe"

    def fetch(self, url: str) -> Any:
        return self._get_json(url)


def make_probe(**options: Any) -> ProbeConnector:
    options.setdefault("backoff_base", 1.0)
    return ProbeConnector(**options)


@respx.mock
def test_fetch_parses_json(fake_clock: Any, fake_redis: FakeRedis) -> None:
    route = respx.get(PROBE_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    connector = make_probe(redis_client=fake_redis, clock=fake_clock.now, sleeper=fake_clock.sleep)

    assert connector.fetch(PROBE_URL) == {"ok": True}
    assert route.call_count == 1


@respx.mock
def test_cache_roundtrip_serves_second_call_from_redis(
    fake_clock: Any, fake_redis: FakeRedis
) -> None:
    route = respx.get(PROBE_URL).mock(return_value=httpx.Response(200, json={"n": 1}))
    connector = make_probe(redis_client=fake_redis, clock=fake_clock.now, sleeper=fake_clock.sleep)

    assert connector.fetch(PROBE_URL) == {"n": 1}
    assert connector.fetch(PROBE_URL) == {"n": 1}
    assert route.call_count == 1  # second call came from the cache
    assert len(fake_redis.keys("yhf:cache:probe:*")) == 1


@respx.mock
def test_cache_disabled_when_no_redis_given(fake_clock: Any) -> None:
    route = respx.get(PROBE_URL).mock(return_value=httpx.Response(200, json={"n": 1}))
    connector = make_probe(clock=fake_clock.now, sleeper=fake_clock.sleep)

    connector.fetch(PROBE_URL)
    connector.fetch(PROBE_URL)
    assert route.call_count == 2  # nothing cached — no redis, no cache


@respx.mock
def test_redis_down_never_fails_the_fetch(fake_clock: Any) -> None:
    """A broken cache degrades to no caching; the fetch still succeeds."""

    class DownRedis:
        def get(self, key: str) -> None:
            raise redis.ConnectionError("redis is down")

        def set(self, *args: Any, **kwargs: Any) -> None:
            raise redis.ConnectionError("redis is down")

    route = respx.get(PROBE_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    connector = make_probe(clock=fake_clock.now, sleeper=fake_clock.sleep)
    connector._cache = RedisCache(DownRedis())  # type: ignore[arg-type]

    assert connector.fetch(PROBE_URL) == {"ok": True}
    assert route.call_count == 1


@respx.mock
def test_429_backs_off_then_succeeds_on_retry(fake_clock: Any) -> None:
    route = respx.get(PROBE_URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json={"recovered": True}),
        ]
    )
    connector = make_probe(clock=fake_clock.now, sleeper=fake_clock.sleep)

    assert connector.fetch(PROBE_URL) == {"recovered": True}
    assert route.call_count == 2
    assert len(fake_clock.sleeps) == 1  # one backoff sleep, no rate-limit sleep
    assert 0.0 <= fake_clock.sleeps[0] <= 1.0  # full jitter on the first retry


@respx.mock
def test_403_backs_off_then_succeeds_on_retry(fake_clock: Any) -> None:
    respx.get(PROBE_URL).mock(
        side_effect=[
            httpx.Response(403),
            httpx.Response(200, json={"recovered": True}),
        ]
    )
    connector = make_probe(clock=fake_clock.now, sleeper=fake_clock.sleep)

    assert connector.fetch(PROBE_URL) == {"recovered": True}
    assert len(fake_clock.sleeps) == 1


@respx.mock
def test_timeout_backs_off_then_succeeds_on_retry(fake_clock: Any) -> None:
    respx.get(PROBE_URL).mock(
        side_effect=[
            httpx.ConnectError("connection timed out"),
            httpx.Response(200, json={"recovered": True}),
        ]
    )
    connector = make_probe(clock=fake_clock.now, sleeper=fake_clock.sleep)

    assert connector.fetch(PROBE_URL) == {"recovered": True}
    assert len(fake_clock.sleeps) == 1


@respx.mock
def test_unexpected_4xx_raises_immediately_without_backoff(fake_clock: Any) -> None:
    route = respx.get(PROBE_URL).mock(return_value=httpx.Response(404, text="nope"))
    connector = make_probe(clock=fake_clock.now, sleeper=fake_clock.sleep)

    with pytest.raises(ConnectorError, match="404"):
        connector.fetch(PROBE_URL)
    assert route.call_count == 1
    assert fake_clock.sleeps == []


@respx.mock
def test_retries_exhausted_raises_connector_error(fake_clock: Any) -> None:
    respx.get(PROBE_URL).mock(return_value=httpx.Response(429))
    connector = make_probe(clock=fake_clock.now, sleeper=fake_clock.sleep, max_retries=2)

    with pytest.raises(ConnectorError, match="429"):
        connector.fetch(PROBE_URL)
    assert len(fake_clock.sleeps) == 2  # backoff after each failed attempt


@respx.mock
def test_backoff_grows_exponentially_with_full_jitter(fake_clock: Any) -> None:
    respx.get(PROBE_URL).mock(return_value=httpx.Response(429))
    connector = make_probe(clock=fake_clock.now, sleeper=fake_clock.sleep, max_retries=3)

    with pytest.raises(ConnectorError):
        connector.fetch(PROBE_URL)
    # Ceilings are base * 2**attempt: 1, 2, 4 — each sleep bounded by its ceiling.
    assert len(fake_clock.sleeps) == 3
    assert fake_clock.sleeps[0] <= 1.0
    assert fake_clock.sleeps[1] <= 2.0
    assert fake_clock.sleeps[2] <= 4.0
