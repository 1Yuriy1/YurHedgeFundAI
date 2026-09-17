"""Offline end-to-end test of the pre-market pipeline: fixtures + StubLLM.

Runs the real LangGraph graph — fixture-routed connectors, canned analyst
responses, an in-memory repository — and asserts the brief's claims are
mechanical: ranked scores equal ``core.scoring.aggregate``'s output for the
fixture signals, every evidence mention carries a URL, and a dead connector
becomes a gap entry instead of a fabricated signal or a halted run.
"""

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from agents.base import BaseAgent
from agents.leadership.portfolio_manager import PortfolioManagerAgent
from core.config import Settings
from core.scoring import ScoreResult, aggregate
from data.connectors.edgar import filing_document_url
from data.connectors.fmp import FMP_BASE_URL
from data.connectors.fred import FRED_BASE_URL
from fakeredis import FakeRedis
from pipelines.premarket import (
    Gap,
    PipelineState,
    PremarketResult,
    build_premarket_graph,
    gaps_from_payload,
    group_signals,
    primary_signals,
    rank_groups,
    run_premarket,
    validate_signals,
)
from storage.repository import MARKET_GROUP, ScoreRecord

from tests.test_agents.conftest import (
    StubLLM,
    canned_response,
    make_fmp_agent,
    make_macro_agent,
    make_sec_agent,
    signals_from_fixture,
)
from tests.test_connectors.conftest import FakeClock, load_fixture_json, make_settings

FRED_URL = f"{FRED_BASE_URL}/series/observations"
EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
RUN_DATE = "2026-09-17"
RUN_TS = f"{RUN_DATE}T10:00:00Z"
PM_SYNTHESIS = (
    "Mechanical read: the market regime sits at the curve trigger while MSFT "
    "carries the only constructive skew; treat AAPL's flat scorecard as wait-and-see."
)
UNIVERSE = ["AAPL", "MSFT"]

GROUP_LINE = re.compile(
    r"^(?P<bullet>[-\d.]+)?\s*\*\*(?P<ticker>[A-Z]+)\*\* — score \*\*(?P<score>[+-][0-9.]+)\*\*, "
    r"leader: (?P<leader>.+?)(?: · RED FLAG)?$"
)
EVIDENCE_LINE = re.compile(r"^\s+- \[[^\]]+\]\((?P<url>[^)]+)\) — ")


class InMemorySignalRepository:
    """The SignalRepository protocol, kept in dicts for offline runs."""

    def __init__(self) -> None:
        self.saved_signals: dict[str, list[Any]] = {}
        self.saved_scores: dict[str, list[ScoreRecord]] = {}

    def save_signals(self, run_id: str, run_date: str, signals: list[Any]) -> int:
        self.saved_signals.setdefault(run_id, []).extend(signals)
        return len(signals)

    def save_score_records(self, run_id: str, run_date: str, records: list[ScoreRecord]) -> int:
        self.saved_scores.setdefault(run_id, []).extend(records)
        return len(records)


def msft_efts_hit() -> dict[str, Any]:
    """One EFTS full-text hit for MSFT's CIK, shaped like the recorded fixture."""
    return {
        "_id": "0000789019-26-000201:msft-20260630.htm",
        "_score": 1.2,
        "_source": {
            "ciks": ["0000789019"],
            "display_names": ["Microsoft Corporation"],
            "form": "10-Q",
            "file_date": "2026-07-30",
            "adsh": "0000789019-26-000201",
            "file_type": "10-Q",
        },
    }


# Built by the same helper the SEC agent uses to turn filings into evidence URLs.
MSFT_FILING_URL = filing_document_url("0000789019", "0000789019-26-000201", "msft-20260630.htm")


def msft_sec_variant() -> list[dict[str, Any]]:
    """The SEC fixture plus an MSFT twin carrying the MSFT hit's evidence."""
    base = signals_from_fixture("signals_sec.json")[0]
    evidence = [{**base["evidence"][0], "url": MSFT_FILING_URL}]
    variant = {
        **base,
        "ticker": "MSFT",
        "signal": "bullish",
        "strength": 0.5,
        "confidence": 0.6,
        "thesis": "Routine quarterly cadence with a constructive cash position — mirrored read.",
        "evidence": evidence,
    }
    return [base, variant]


def fixture_signals() -> list[dict[str, Any]]:
    """Every canned signal the pipeline is expected to materialize."""
    return [
        *signals_from_fixture("signals_macro.json"),
        *msft_sec_variant(),
        *signals_from_fixture("signals_fundamentals.json"),
    ]


def expected_groups() -> dict[str, ScoreResult]:
    """Recompute the expected scores the way the score node does."""
    from core.schemas import Signal

    signals = [
        Signal.model_validate({**payload, "timestamp": RUN_TS}) for payload in fixture_signals()
    ]
    return {
        ticker: aggregate(primary_signals(members))
        for ticker, members in group_signals(signals).items()
    }


def mock_all_sources(fred_status: int = 200) -> None:
    respx.get(FRED_URL).mock(
        return_value=httpx.Response(fred_status, json=load_fixture_json("fred_observations.json"))
    )
    respx.get(url__regex=r"^https://data\.sec\.gov/submissions/CIK\d+\.json$").mock(
        return_value=httpx.Response(200, json=load_fixture_json("edgar_submissions.json"))
    )
    efts = load_fixture_json("edgar_efts_hits.json")
    efts["hits"]["hits"].append(msft_efts_hit())
    efts["hits"]["total"]["value"] = len(efts["hits"]["hits"])
    respx.get(EFTS_URL).mock(return_value=httpx.Response(200, json=efts))
    for endpoint, fixture in (
        ("profile", "fmp_profile.json"),
        ("ratios", "fmp_ratios.json"),
        ("income-statement", "fmp_income_statement.json"),
    ):
        for ticker in ("AAPL", "MSFT"):
            respx.get(f"{FMP_BASE_URL}/{endpoint}/{ticker}").mock(
                return_value=httpx.Response(200, json=load_fixture_json(fixture))
            )


def build_analysts(settings: Settings, clock: FakeClock) -> list[BaseAgent]:
    """The three analysts with fixture-routed connectors and canned LLMs."""
    return [
        make_macro_agent(
            clock,
            FakeRedis(decode_responses=False),
            StubLLM(canned_response("signals_macro.json")),
            settings,
        ),
        make_sec_agent(clock, StubLLM(json.dumps(msft_sec_variant())), settings),
        make_fmp_agent(
            clock,
            FakeRedis(decode_responses=False),
            StubLLM(canned_response("signals_fundamentals.json")),
            settings,
        ),
    ]


def run_offline(
    tmp_path: Path, *, fred_status: int = 200
) -> tuple[PremarketResult, InMemorySignalRepository]:
    settings = make_settings(universe=UNIVERSE, fmp_api_key="test-fmp-key")
    clock = FakeClock()
    repo = InMemorySignalRepository()
    result = run_premarket(
        universe=UNIVERSE,
        analysts=build_analysts(settings, clock),
        pm=PortfolioManagerAgent(llm=StubLLM(PM_SYNTHESIS), settings=settings),
        # The in-memory fake satisfies the SignalRepository protocol at runtime.
        repo=repo,
        briefs_dir=tmp_path / "briefs",
        run_id="test-run",
        run_date=RUN_DATE,
    )
    return result, repo


def parse_group_line(line: str) -> re.Match[str] | None:
    """Match a scored-group headline, ranked or market-regime style."""
    return GROUP_LINE.match(line)


def line_for(brief: str, ticker: str) -> str:
    """The brief's rendered group line for one ticker."""
    return next(
        line for line in brief.splitlines() if f"**{ticker}**" in line and parse_group_line(line)
    )


@pytest.mark.respx
@respx.mock
def test_brief_exists_at_the_dated_path(tmp_path: Path) -> None:
    mock_all_sources()

    result, _ = run_offline(tmp_path)

    assert result.brief_path == tmp_path / "briefs" / f"{RUN_DATE}.md"
    assert result.brief_path.is_file()
    assert result.brief_path.read_text(encoding="utf-8") == result.brief
    assert f"# Pre-Market Brief — {RUN_DATE}" in result.brief
    assert f"**Universe:** {', '.join(UNIVERSE)}" in result.brief


@pytest.mark.respx
@respx.mock
def test_ranked_scores_match_aggregate(tmp_path: Path) -> None:
    """Brief scores are aggregate()'s output over the PERSISTED audit trail.

    The expected inputs are the signals the repository received, not the raw
    fixtures: agents may materialize a fixture with code-enforced changes
    (the SEC sweep upgrades red flags before scoring), and the invariant the
    brief must satisfy is persist→score→render consistency.
    """
    mock_all_sources()

    result, repo = run_offline(tmp_path)
    from core.schemas import Signal

    persisted = [
        Signal.model_validate(s) if isinstance(s, dict) else s
        for s in repo.saved_signals["test-run"]
    ]
    expected = {
        ticker: aggregate(primary_signals(members))
        for ticker, members in group_signals(persisted).items()
    }

    ranked: dict[str, tuple[float, str]] = {}
    for line in result.brief.splitlines():
        match = parse_group_line(line)
        if match is not None and re.match(r"^\d+\.", line):
            ranked[match["ticker"]] = (float(match["score"]), match["leader"])

    assert set(ranked) == {ticker for ticker in expected if ticker != MARKET_GROUP}
    scores = [score for score, _ in ranked.values()]
    assert scores == sorted(scores, reverse=True), "ranked ideas must be score-ordered"
    for ticker, (score, leader) in ranked.items():
        want = expected[ticker]
        assert score == want["score"]
        assert leader == (want["led_by"] or "no leader (all neutral)")
        # Sweep-enforced red flags must surface in the rendered line.
        assert (" · RED FLAG" in line_for(result.brief, ticker)) == any(
            s.red_flag for s in persisted if s.ticker == ticker
        )


@pytest.mark.respx
@respx.mock
def test_market_regime_line_matches_aggregate(tmp_path: Path) -> None:
    mock_all_sources()

    result, _ = run_offline(tmp_path)
    expected = expected_groups()

    market_lines = [
        match
        for line in result.brief.splitlines()
        if (match := parse_group_line(line)) is not None and match["bullet"] == "-"
    ]
    assert len(market_lines) == 1
    want = expected[MARKET_GROUP]
    assert float(market_lines[0]["score"]) == want["score"]
    assert market_lines[0]["leader"] == (want["led_by"] or "no leader (all neutral)")


@pytest.mark.respx
@respx.mock
def test_every_evidence_mention_carries_a_url(tmp_path: Path) -> None:
    mock_all_sources()

    result, _ = run_offline(tmp_path)
    rendered_urls = {
        match["url"]
        for line in result.brief.splitlines()
        if (match := EVIDENCE_LINE.match(line)) is not None
    }
    fixture_urls = {
        evidence["url"] for payload in fixture_signals() for evidence in payload["evidence"]
    }

    assert rendered_urls == fixture_urls
    assert all(url.startswith(("http://", "https://")) for url in rendered_urls)


@pytest.mark.respx
@respx.mock
def test_pm_synthesis_is_rendered(tmp_path: Path) -> None:
    mock_all_sources()

    result, _ = run_offline(tmp_path)

    header, _, rest = result.brief.partition("## Portfolio Manager synthesis")
    assert header.startswith("# Pre-Market Brief")
    assert PM_SYNTHESIS in rest.partition("## Market regime")[0]


def graph_edges() -> list[tuple[str, str]]:
    """The compiled graph's edges — the persistence-ordering contract."""
    settings = make_settings(universe=UNIVERSE, fmp_api_key="test-fmp-key")
    graph = build_premarket_graph(
        universe=UNIVERSE,
        analysts=build_analysts(settings, FakeClock()),
        pm=PortfolioManagerAgent(llm=StubLLM("synthesis"), settings=settings),
        repo=InMemorySignalRepository(),
        briefs_dir=Path("/tmp"),
        run_id="edge-probe",
        run_date=RUN_DATE,
    )
    return [(edge.source, edge.target) for edge in graph.get_graph().edges]


@pytest.mark.respx
@respx.mock
def test_persistence_happens_before_the_pm_call(tmp_path: Path) -> None:
    """Audit trail lands in the graph BEFORE synthesis (crash-safe order)."""
    mock_all_sources()

    result, repo = run_offline(tmp_path)

    edges = graph_edges()
    assert ("score", "persist") in edges
    assert ("persist", "synthesize") in edges
    assert len(repo.saved_scores["test-run"]) == 3  # MARKET + two tickers
    assert len(repo.saved_signals["test-run"]) == 4


@pytest.mark.respx
@respx.mock
def test_dead_connector_becomes_a_gap_and_the_run_completes(tmp_path: Path) -> None:
    mock_all_sources(fred_status=500)

    result, repo = run_offline(tmp_path, fred_status=500)

    _, _, gaps = result.brief.partition("## Gaps")
    assert "macro_rates" in gaps
    assert "fred" in gaps.lower()
    assert "None — every source returned data." not in result.brief
    # The two healthy sources still produced ranked ideas; the brief still exists.
    assert re.search(r"^\d+\. \*\*MSFT\*\*", result.brief, flags=re.MULTILINE)
    assert len(repo.saved_signals["test-run"]) == 3  # sec 2 + fundamentals 1, macro silent


def make_state(**overrides: Any) -> PipelineState:
    state: PipelineState = {
        "run_id": "r",
        "run_date": RUN_DATE,
        "universe": list(UNIVERSE),
        "raw_signals": [],
        "gaps": [],
        "signals": [],
        "scored": [],
        "pm_synthesis": "",
        "brief": "",
        "brief_path": "",
    }
    state.update(overrides)  # type: ignore[typeddict-item]
    return state


def test_invalid_signal_never_reaches_scoring() -> None:
    from core.schemas import Signal

    good = Signal.model_validate({**fixture_signals()[0], "timestamp": RUN_TS})
    broken: dict[str, Any] = {"agent": "sec_filings", "tier": 3, "thesis": "no evidence here"}

    result = validate_signals(make_state(raw_signals=[good, broken]))

    assert result["signals"] == [good]
    assert len(result["gaps"]) == 1
    assert result["gaps"][0].source == "sec_filings"
    assert "invalid signal dropped" in result["gaps"][0].detail


def test_connector_errors_become_ticker_attributed_gaps() -> None:
    universe = frozenset(UNIVERSE)

    flat = gaps_from_payload("macro_rates", {"UNRATE": "fred: HTTP 500"}, universe)
    nested = gaps_from_payload(
        "sec_filings", {"submissions": {"AAPL": "edgar: HTTP 503"}}, universe
    )

    assert flat == [Gap(source="macro_rates", ticker=None, detail="UNRATE: fred: HTTP 500")]
    assert nested == [
        Gap(source="sec_filings", ticker="AAPL", detail="submissions: edgar: HTTP 503")
    ]


def test_ranking_is_market_first_then_score_desc() -> None:
    from core.schemas import Signal

    payloads = fixture_signals()
    strong = {
        **payloads[1],
        "ticker": "MSFT",
        "signal": "bearish",
        "strength": 0.9,
        "confidence": 0.9,
    }
    signals = [Signal.model_validate({**p, "timestamp": RUN_TS}) for p in [*payloads, strong]]

    ranked = rank_groups(group_signals(signals))

    assert ranked[0].ticker == MARKET_GROUP
    assert ranked[1].ticker == "MSFT"
    assert ranked[1].result["score"] > ranked[2].result["score"]
