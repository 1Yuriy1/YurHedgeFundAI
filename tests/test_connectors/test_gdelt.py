"""GDELT connector tests: artlist parsing, 250 cap, 5 s pacing, offline."""

import json

import httpx
import pytest
import respx
from data.connectors.gdelt import GDELT_DOC_URL, GdeltConnector
from fakeredis import FakeRedis

from .conftest import FakeClock, load_fixture


def make_connector(fake_clock: FakeClock, fake_redis: FakeRedis | None = None) -> GdeltConnector:
    return GdeltConnector(redis_client=fake_redis, clock=fake_clock.now, sleeper=fake_clock.sleep)


@respx.mock
def test_parses_article_fixture(fake_clock: FakeClock) -> None:
    route = respx.get(GDELT_DOC_URL).mock(
        return_value=httpx.Response(200, text=load_fixture("gdelt_articles.json"))
    )
    connector = make_connector(fake_clock)

    articles = connector.search_articles("Federal Reserve", timespan="1w")

    assert len(articles) == 2
    assert articles[0]["title"] == "Fed signals slower rate path as inflation cools"
    assert articles[0]["url"] == "https://example.com/fed-signals-rate-path"
    assert articles[0]["seendate"] == "20260917T120000Z"
    assert articles[0]["source"] == "example.com"  # GDELT 'domain' surfaced as 'source'
    assert articles[1]["source_country"] == "united states"

    params = route.calls[-1].request.url.params
    assert params["query"] == "Federal Reserve"
    assert params["mode"] == "artlist"
    assert params["format"] == "json"
    assert params["maxrecords"] == "250"
    assert params["timespan"] == "1w"


@respx.mock
def test_caps_results_at_250(fake_clock: FakeClock) -> None:
    """An oversized article list truncates to the hard cap in the request too."""
    payload = {
        "articles": [
            {"url": f"https://e.example.com/{i}", "title": f"t{i}", "domain": "e.example.com"}
            for i in range(260)
        ]
    }
    route = respx.get(GDELT_DOC_URL).mock(
        return_value=httpx.Response(200, text=json.dumps(payload))
    )
    connector = make_connector(fake_clock)

    articles = connector.search_articles("anything", max_records=300)

    assert len(articles) == 250
    assert route.calls[-1].request.url.params["maxrecords"] == "250"


@respx.mock
def test_first_call_is_immediate_second_is_paced_5s(fake_clock: FakeClock) -> None:
    respx.get(GDELT_DOC_URL).mock(
        return_value=httpx.Response(200, text=load_fixture("gdelt_articles.json"))
    )
    connector = make_connector(fake_clock)

    connector.search_articles("rates")
    assert fake_clock.sleeps == []  # first call: no pacing debt yet

    connector.search_articles("rates")
    assert len(fake_clock.sleeps) == 1
    assert fake_clock.sleeps[0] >= 5.0


@respx.mock
def test_non_json_body_degrades_to_empty_list(fake_clock: FakeClock) -> None:
    respx.get(GDELT_DOC_URL).mock(
        return_value=httpx.Response(200, text="<html>gateway error</html>")
    )
    connector = make_connector(fake_clock)

    assert connector.search_articles("rates") == []


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        "not json at all",
        '{"articles": "nope"}',
        "[1, 2, 3]",
        '{"articles": ["junk", 7]}',
        "",
    ],
)
def test_malformed_bodies_degrade_to_empty_list(body: str, fake_clock: FakeClock) -> None:
    respx.get(GDELT_DOC_URL).mock(return_value=httpx.Response(200, text=body))
    connector = make_connector(fake_clock)

    assert connector.search_articles("rates") == []


@respx.mock
def test_request_never_carries_api_key(fake_clock: FakeClock) -> None:
    """GDELT is keyless; no credential params may leak into the query string."""
    route = respx.get(GDELT_DOC_URL).mock(
        return_value=httpx.Response(200, text=load_fixture("gdelt_articles.json"))
    )
    connector = make_connector(fake_clock)

    connector.search_articles("rates")

    assert "apikey" not in route.calls[-1].request.url.params
    assert "token" not in route.calls[-1].request.url.params
