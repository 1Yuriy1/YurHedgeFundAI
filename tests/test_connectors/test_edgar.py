"""SEC EDGAR tests: submissions columnar parsing, EFTS defense, UA, rate cap."""

from typing import Any

import httpx
import pytest
import respx
from data.connectors.base import ConnectorError, DisabledConnectorError
from data.connectors.edgar import EdgarConnector, filing_document_url, normalize_cik
from fakeredis import FakeRedis

from .conftest import TEST_EDGAR_USER_AGENT, FakeClock, load_fixture_json, make_settings

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK0000320193.json"
EFTS_URL = "https://efts.sec.gov/LATEST/search-index"


def make_connector(
    fake_clock: FakeClock,
    fake_redis: FakeRedis | None,
    **options: Any,
) -> EdgarConnector:
    return EdgarConnector(
        make_settings(),
        redis_client=fake_redis,
        clock=fake_clock.now,
        sleeper=fake_clock.sleep,
        **options,
    )


def test_normalize_cik_zero_pads_and_strips_cik_prefix() -> None:
    assert normalize_cik("320193") == "0000320193"
    assert normalize_cik("CIK0000320193") == "0000320193"
    with pytest.raises(ConnectorError):
        normalize_cik("not-a-cik")


def test_disabled_without_descriptive_user_agent() -> None:
    """SEC flatly rejects default-library user agents — refuse before dialing."""
    with pytest.raises(DisabledConnectorError):
        EdgarConnector(make_settings(edgar_user_agent=None))


@respx.mock
def test_parses_columnar_submissions_arrays(fake_clock: FakeClock) -> None:
    route = respx.get(SUBMISSIONS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture_json("edgar_submissions.json"))
    )
    connector = make_connector(fake_clock, None)

    records = connector.fetch_submissions("320193")

    assert route.call_count == 1
    assert len(records) == 2
    assert records[0]["accession_number"] == "0000320193-26-000105"
    assert records[0]["form"] == "10-Q"
    assert records[0]["filing_date"] == "2026-08-01"
    assert records[0]["report_date"] == "2026-06-28"
    assert records[0]["primary_document"] == "aapl-20260628.htm"
    assert records[1]["form"] == "10-Q"
    assert records[1]["filing_date"] == "2026-05-02"


@respx.mock
def test_short_columnar_arrays_degrade_to_fewer_rows(fake_clock: FakeClock) -> None:
    """A truncated column trims the rows instead of crashing (defensive parse)."""
    payload = load_fixture_json("edgar_submissions.json")
    payload["filings"]["recent"]["primaryDocument"].pop()
    respx.get(SUBMISSIONS_URL).mock(return_value=httpx.Response(200, json=payload))
    connector = make_connector(fake_clock, None)

    records = connector.fetch_submissions("320193")

    assert len(records) == 1


@respx.mock
def test_sends_descriptive_user_agent_header(fake_clock: FakeClock) -> None:
    route = respx.get(SUBMISSIONS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture_json("edgar_submissions.json"))
    )
    connector = make_connector(fake_clock, None)

    connector.fetch_submissions("320193")

    request = route.calls[-1].request
    assert request.headers["User-Agent"] == TEST_EDGAR_USER_AGENT


@respx.mock
def test_respects_five_requests_per_second_cap(fake_clock: FakeClock) -> None:
    """Seven rapid calls pace to >=6 * 0.2s waits, first call unpaced."""
    respx.get(SUBMISSIONS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture_json("edgar_submissions.json"))
    )
    connector = make_connector(fake_clock, None)

    for _ in range(7):
        connector.fetch_submissions("320193")

    assert len(fake_clock.sleeps) == 7  # every call sleeps at least the jitter
    assert fake_clock.sleeps[0] < 0.05  # first call: pacing jitter only
    for wait in fake_clock.sleeps[1:]:
        assert wait >= 0.2
    assert sum(fake_clock.sleeps) >= 6 * 0.2


@respx.mock
def test_parses_efts_hits_fixture(fake_clock: FakeClock) -> None:
    route = respx.get(EFTS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture_json("edgar_efts_hits.json"))
    )
    connector = make_connector(fake_clock, None)

    hits = connector.full_text_search("material weakness")

    assert route.call_count == 1
    assert len(hits) == 2
    first = hits[0]
    assert first["id"] == "0000320193-26-000105:aapl-20260628.htm"
    assert first["ciks"] == ["0000320193"]
    assert first["display_names"] == ["Apple Inc."]
    assert first["form"] == "10-Q"
    assert first["file_date"] == "2026-08-01"
    assert first["accession"] == "0000320193-26-000105"
    assert first["score"] == 1.5


@respx.mock
def test_efts_missing_hits_returns_empty_list(fake_clock: FakeClock) -> None:
    """The recorded zero-hit shape — hits present but hits.hits[] absent."""
    respx.get(EFTS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture_json("edgar_efts_no_hits.json"))
    )
    connector = make_connector(fake_clock, None)

    assert connector.full_text_search("nonexistent topic") == []


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        {"hits": []},
        {"hits": {"hits": "not-a-list"}},
        {"status": "error"},
        {},
        {"hits": {"hits": ["junk", 42]}},
    ],
)
def test_efts_defensive_against_malformed_bodies(body: Any, fake_clock: FakeClock) -> None:
    """EFTS is an unofficial schema — malformed hits degrade to an empty list."""
    respx.get(EFTS_URL).mock(return_value=httpx.Response(200, json=body))
    connector = make_connector(fake_clock, None)

    assert connector.full_text_search("anything") == []


@respx.mock
def test_efts_sends_query_and_filters(fake_clock: FakeClock) -> None:
    route = respx.get(EFTS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture_json("edgar_efts_hits.json"))
    )
    connector = make_connector(fake_clock, None)

    connector.full_text_search(
        "risk factor",
        forms=["10-K"],
        ciks=["320193"],
        start_date="2025-01-01",
        end_date="2026-01-01",
        limit=5,
    )

    params = route.calls[-1].request.url.params
    assert params["q"] == "risk factor"
    assert params["forms"] == "10-K"
    assert params["ciks"] == "0000320193"
    assert params["startdt"] == "2025-01-01"
    assert params["enddt"] == "2026-01-01"
    assert params["size"] == "5"


def test_filing_document_url_builds_archive_path() -> None:
    url = filing_document_url("320193", "0000320193-26-000105", "aapl-20260628.htm")
    assert url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019326000105/aapl-20260628.htm"
    )
