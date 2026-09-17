"""SEC Filings agent tests: filing evidence, red-flag sweep, graceful degradation."""

import json

import httpx
import respx
from agents.base import load_prompt
from agents.research.sec_filings import (
    RED_FLAG_PHRASES,
    attribute_hits,
    efts_hit_document_url,
    filing_payload_rows,
    recent_filings,
)
from data.connectors.edgar import parse_efts_hits, parse_submissions

from tests.test_connectors.conftest import FakeClock, load_fixture_json

from .conftest import (
    StubLLM,
    canned_response,
    last_user_payload,
    make_sec_agent,
    signals_from_fixture,
)

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK0000320193.json"
EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
AAPL_FILING_URL = (
    "https://www.sec.gov/Archives/edgar/data/320193/000032019326000105/aapl-20260628.htm"
)
TICKER_BY_CIK = {"0000320193": "AAPL", "0000789019": "MSFT"}

# A FilingRecord literal (TypedDicts are plain dicts at runtime), matching the
# first row of the submissions fixture.
FILING_ROW: dict[str, str] = {
    "accession_number": "0000320193-26-000105",
    "form": "10-Q",
    "filing_date": "2026-08-01",
    "report_date": "2026-06-28",
    "primary_document": "aapl-20260628.htm",
}


def test_red_flag_phrases_are_auditable_in_prompt() -> None:
    """The prompt lists the exact trigger phrases the code sweeps for."""
    text = load_prompt("sec_filings_v1.md").lower()
    for phrase in RED_FLAG_PHRASES:
        assert phrase.lower() in text, f"phrase {phrase!r} missing from prompt"


def test_efts_hits_resolve_to_canonical_filing_urls() -> None:
    hits = parse_efts_hits(load_fixture_json("edgar_efts_hits.json"))

    assert len(hits) == 2
    assert efts_hit_document_url(hits[0]) == AAPL_FILING_URL
    assert efts_hit_document_url(hits[1]).startswith("https://www.sec.gov/Archives/edgar/data/")


def test_recent_filings_window_filters_by_filing_date() -> None:
    parsed = parse_submissions(load_fixture_json("edgar_submissions.json"))

    both = recent_filings(parsed, since_date="2026-05-01")
    none = recent_filings(parsed, since_date="2026-09-01")

    assert [row["accession_number"] for row in both] == [
        "0000320193-26-000105",
        "0000320193-26-000098",
    ]
    assert none == []


def test_hits_attributed_by_cik_and_off_universe_hits_drop() -> None:
    hits = parse_efts_hits(load_fixture_json("edgar_efts_hits.json"))

    attributed = attribute_hits(hits, TICKER_BY_CIK, phrase="going concern")

    assert set(attributed) == {"AAPL"}  # MSFT has no hits — never fabricated
    rows = attributed["AAPL"]
    assert len(rows) == 2
    assert rows[0]["phrase"] == "going concern"
    assert rows[0]["url"] == AAPL_FILING_URL


def test_attribute_ignores_unknown_ciks() -> None:
    hits = parse_efts_hits(load_fixture_json("edgar_efts_hits.json"))

    attributed = attribute_hits(hits, {"0000789019": "MSFT"}, phrase="fraud")

    assert attributed == {}  # off-universe CIKs never become signals


def test_filing_payload_rows_carry_canonical_urls() -> None:
    parsed = parse_submissions(load_fixture_json("edgar_submissions.json"))

    rows = filing_payload_rows("0000320193", parsed)

    assert rows[0]["url"] == AAPL_FILING_URL
    assert rows[0]["form"] == "10-Q"


@respx.mock
def test_run_emits_signals_with_filing_url_evidence(fake_clock: FakeClock) -> None:
    respx.get(SUBMISSIONS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture_json("edgar_submissions.json"))
    )
    # Every phrase hits the same fixture; each contributes attributed rows.
    respx.get(EFTS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture_json("edgar_efts_hits.json"))
    )
    stub = StubLLM(canned_response("signals_sec.json"))
    agent = make_sec_agent(fake_clock, stub)

    signals = agent.run(["AAPL"])

    assert len(signals) == 1
    signal = signals[0]
    assert signal.agent == "sec_filings"
    assert signal.tier == 3
    assert signal.ticker == "AAPL"
    assert any(evidence.url == AAPL_FILING_URL for evidence in signal.evidence)
    # The LLM said red_flag=false but the sweep found red-flag filings — the
    # mechanical flag wins.
    assert signal.red_flag is True
    payload = last_user_payload(stub)
    assert "going concern" in payload


def test_llm_red_flag_survives_when_no_sweep_hits(fake_clock: FakeClock) -> None:
    """No sweep hits: the model's own red-flag judgment is respected."""
    data = {
        "filings": {"AAPL": [dict(FILING_ROW)]},
        "red_flag_hits": {},
        "errors": {"submissions": {}, "efts": {}},
    }
    flagged_model = json.dumps([{**signals_from_fixture("signals_sec.json")[0], "red_flag": True}])

    flagged_agent = make_sec_agent(fake_clock, StubLLM(flagged_model))
    flagged = flagged_agent.analyze(["AAPL"], data)

    plain_agent = make_sec_agent(fake_clock, StubLLM(canned_response("signals_sec.json")))
    plain = plain_agent.analyze(["AAPL"], data)

    assert len(flagged) == 1 and flagged[0].red_flag is True
    assert len(plain) == 1 and plain[0].red_flag is False


@respx.mock
def test_efts_failure_degrades_to_gap_not_signal(fake_clock: FakeClock) -> None:
    respx.get(SUBMISSIONS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture_json("edgar_submissions.json"))
    )
    respx.get(EFTS_URL).mock(return_value=httpx.Response(500, json={"error": "boom"}))
    stub = StubLLM(canned_response("signals_sec.json"))
    agent = make_sec_agent(fake_clock, stub)

    data = agent.collect(["AAPL"])
    assert "going concern" in data["errors"]["efts"]

    signals = agent.run(["AAPL"])
    assert signals == []  # fixture filings are outside the window, sweep failed
    assert stub.call_count == 0  # silence beats an evidence-free signal
