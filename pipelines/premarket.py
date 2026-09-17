"""Pre-market pipeline — one deterministic daily cycle, orchestrated by LangGraph.

    fetch_universe → analyze_macro_rates ┐
                   → analyze_sec_filings ├→ validate_signals → score
                   → analyze_fundamentals┘                       ↓
                              persist → synthesize (PM) → write_brief

Failure semantics (the spec's gap rule): a connector or agent failure for a
source yields a GAP — recorded in state, rendered in the brief's Gaps
section — and never a fabricated signal and never a halted run. The two
places a source can fail are both covered: an exception escaping the analyst
node becomes a gap, and connector failures the agent already swallowed into
its payload ``errors`` dict are harvested into gaps by :func:`gaps_from_payload`.

Persistence happens after scoring and BEFORE PM synthesis, so a crash
mid-synthesis never loses the audit trail (storage/repository.py).

Every claim in the brief is mechanical: scores are ``core.scoring.aggregate``'s
output, leader names are its ``led_by``, evidence URLs come from the signals
themselves. The PM (the only Opus call) writes synthesis prose over that
substrate — it never re-scores and cannot invent data without the mechanical
sections exposing the difference.
"""

import logging
import operator
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final, Protocol, TypedDict
from uuid import uuid4

from agents.base import BaseAgent, compact_json
from agents.leadership.portfolio_manager import PortfolioManagerAgent
from core.config import Settings
from core.schemas import Signal
from core.scoring import ScoreResult, aggregate
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError
from storage.postgres import PostgresSignalRepository
from storage.repository import MARKET_GROUP, ScoreRecord, SignalRepository

logger = logging.getLogger(__name__)

ALL_NEUTRAL_LEADER: Final[str] = "no leader (all neutral)"


class PipelineError(RuntimeError):
    """The pipeline cannot complete this run (infrastructure, not a source gap)."""


# ── State ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Gap:
    """One missing source/ticker, surfaced in the brief's Gaps section."""

    source: str
    detail: str
    ticker: str | None = None


@dataclass(frozen=True)
class ScoredGroup:
    """One scored idea: a market-wide group or one ticker's signal group."""

    ticker: str  # MARKET_GROUP for the market-wide group
    result: ScoreResult
    signals: list[Signal]


class PipelineState(TypedDict):
    """The graph's channels; ``raw_signals``/``gaps`` add across parallel nodes."""

    run_id: str
    run_date: str
    universe: list[str]
    raw_signals: Annotated[list[Signal], operator.add]
    gaps: Annotated[list[Gap], operator.add]
    signals: list[Signal]  # validated; invalid output never reaches scoring
    scored: list[ScoredGroup]  # market-wide group first, then ranked ideas
    pm_synthesis: str
    brief: str
    brief_path: str


class NodeFn(Protocol):
    """A graph node: reads the pipeline state, returns channel updates.

    Annotating factory returns with this protocol (instead of a plain
    ``Callable`` alias) is what lets strict mypy solve langgraph 1.x's
    contravariant ``NodeInputT`` against ``PipelineState``.
    """

    def __call__(self, state: PipelineState) -> dict[str, Any]: ...


# ── Pure transforms (unit-tested directly, offline) ─────────────────────────


def gaps_from_payload(source: str, errors: object, universe: frozenset[str]) -> list[Gap]:
    """Convert an agent's swallowed connector failures into gap entries.

    Analysts degrade per-unit connector failures inside collect() and report
    them in a payload ``errors`` key shaped ``{key: message}`` (FRED series →
    error) or ``{view: {key: message}}`` (EDGAR view → ticker/phrase →
    error). Every leaf becomes one gap; a leaf key that is a universe ticker
    is attributed to it.
    """
    gaps: list[Gap] = []
    if not isinstance(errors, dict):
        return gaps
    for key, value in errors.items():
        if isinstance(value, dict):
            for sub_key, message in value.items():
                gaps.append(_one_gap(source, str(key), str(sub_key), str(message), universe))
        else:
            gaps.append(_one_gap(source, None, str(key), str(value), universe))
    return gaps


def _one_gap(
    source: str, view: str | None, key: str, message: str, universe: frozenset[str]
) -> Gap:
    """One gap entry; ticker-keyed errors name the ticker, the rest the unit."""
    ticker = key if key in universe else None
    if ticker is not None:
        detail = f"{view}: {message}" if view else message
    else:
        detail = f"{key}: {message}" if view is None else f"{view}:{key}: {message}"
    return Gap(source=source, ticker=ticker, detail=detail)


def group_signals(signals: Sequence[Signal]) -> dict[str, list[Signal]]:
    """Group signals by ticker; market-wide signals (ticker None) go to MARKET."""
    groups: dict[str, list[Signal]] = {}
    for signal in signals:
        key = MARKET_GROUP if signal.ticker is None else signal.ticker
        groups.setdefault(key, []).append(signal)
    return groups


def primary_signals(members: list[Signal]) -> dict[str, Signal]:
    """One vote per agent per the ladder: an agent's first signal in the group."""
    selected: dict[str, Signal] = {}
    for signal in members:
        if signal.agent not in selected:
            logger.debug(
                "primary_signal_selected", extra={"agent": signal.agent, "of": len(members)}
            )
            selected[signal.agent] = signal
    return selected


def score_group(ticker: str, members: list[Signal]) -> ScoredGroup:
    """Aggregate one group through the opinion ladder."""
    return ScoredGroup(
        ticker=ticker,
        result=aggregate(primary_signals(members)),
        signals=list(members),
    )


def rank_groups(groups: dict[str, list[Signal]]) -> list[ScoredGroup]:
    """Market-wide group first, then ticker ideas by score desc (ticker asc ties)."""
    scored = [score_group(ticker, members) for ticker, members in groups.items()]
    market = [group for group in scored if group.ticker == MARKET_GROUP]
    ideas = sorted(
        (group for group in scored if group.ticker != MARKET_GROUP),
        key=lambda group: (-group.result["score"], group.ticker),
    )
    return [*market, *ideas]


def validate_signals(state: PipelineState) -> dict[str, Any]:
    """Schema-conform every raw signal; invalid output never reaches scoring.

    Analysts already emit pydantic-validated models, but this boundary is
    where a future external signal source would enter — the dump/revalidate
    round-trip re-runs every rule (evidence URLs, bounds, UTC coercion) and
    anything that fails becomes a gap entry instead of a score input.
    """
    valid: list[Signal] = []
    gaps: list[Gap] = []
    for item in state["raw_signals"]:
        try:
            payload = item.model_dump(mode="json") if isinstance(item, Signal) else item
            valid.append(Signal.model_validate(payload))
        except ValidationError as exc:
            if isinstance(item, Signal):
                source = item.agent
            elif isinstance(item, dict) and item.get("agent"):
                # Attribution survives validation failure — never guess "unknown".
                source = str(item["agent"])
            else:
                source = "unknown"
            gaps.append(Gap(source=source, detail=f"invalid signal dropped: {exc}"))
            logger.warning(
                "signal_validation_failed",
                extra={"source": source, "error": str(exc)},
            )
    return {"signals": valid, "gaps": gaps}


def score_node(state: PipelineState) -> dict[str, Any]:
    """Group validated signals by ticker and aggregate each through the ladder."""
    return {"scored": rank_groups(group_signals(state["signals"]))}


# ── Brief rendering (pure) ──────────────────────────────────────────────────


def format_score(score: float) -> str:
    """Fixed-sign, three-decimal rendering (aggregate() rounds to 3 places)."""
    return f"{score:+.3f}"


def render_group_line(group: ScoredGroup, label: str) -> str:
    """The scored-group headline: rank, ticker, score, leader, red-flag mark."""
    leader = group.result["led_by"] or ALL_NEUTRAL_LEADER
    flag = " · RED FLAG" if group.result["red_flag"] else ""
    return (
        f"{label} **{group.ticker}** — score **{format_score(group.result['score'])}**, "
        f"leader: {leader}{flag}"
    )


def render_signal(signal: Signal) -> str:
    """One signal with its thesis and URL-carrying evidence bullets."""
    lines = [
        f"  - `{signal.agent}` (tier {signal.tier}, {signal.signal.value}, "
        f"strength {signal.strength:.2f}, confidence {signal.confidence:.2f}): {signal.thesis}",
        "    - Evidence:",
    ]
    for item in signal.evidence:
        lines.append(f'      - [{item.source}]({item.url}) — "{item.excerpt}"')
    return "\n".join(lines)


def render_gaps(gaps: Sequence[Gap]) -> str:
    """Every missing source/ticker as one bullet; 'None' line when complete."""
    if not gaps:
        return "None — every source returned data."
    return "\n".join(
        f"- {gap.source}" + (f" · {gap.ticker}" if gap.ticker else "") + f" — {gap.detail}"
        for gap in gaps
    )


def render_brief(
    *,
    run_date: str,
    universe: Sequence[str],
    scored: Sequence[ScoredGroup],
    gaps: Sequence[Gap],
    pm_synthesis: str,
) -> str:
    """The full brief: header → PM synthesis → market regime → ranked ideas → gaps."""
    market = next((group for group in scored if group.ticker == MARKET_GROUP), None)
    ideas = [group for group in scored if group.ticker != MARKET_GROUP]

    parts = [
        f"# Pre-Market Brief — {run_date}",
        "",
        f"**Universe:** {', '.join(universe)}",
        "",
        "## Portfolio Manager synthesis",
        "",
        pm_synthesis.strip(),
        "",
    ]
    if market is not None:
        parts.append("## Market regime")
        parts.append("")
        parts.append(render_group_line(market, "-"))
        for signal in market.signals:
            parts.append(render_signal(signal))
        parts.append("")
    parts.append("## Ranked ideas")
    parts.append("")
    if ideas:
        for position, group in enumerate(ideas, start=1):
            parts.append(render_group_line(group, f"{position}."))
            for signal in group.signals:
                parts.append(render_signal(signal))
            parts.append("")
    else:
        parts.append("No ticker-level ideas this run — every desk was silent or absent.")
        parts.append("")
    parts.append("## Gaps")
    parts.append("")
    parts.append(render_gaps(gaps))
    parts.append("")
    return "\n".join(parts)


def group_payload(group: ScoredGroup) -> dict[str, Any]:
    """The PM's per-group payload: score, leader, and full signal records."""
    return {
        "ticker": None if group.ticker == MARKET_GROUP else group.ticker,
        "score": group.result["score"],
        "led_by": group.result["led_by"],
        "signals": [signal.model_dump(mode="json") for signal in group.signals],
    }


def synthesis_payload(state: PipelineState) -> dict[str, Any]:
    """What the PM sees: mechanical scores, evidence, and the gap list."""
    market = next((group for group in state["scored"] if group.ticker == MARKET_GROUP), None)
    ideas = [group for group in state["scored"] if group.ticker != MARKET_GROUP]
    return {
        "run_date": state["run_date"],
        "universe": state["universe"],
        "market_regime": group_payload(market) if market is not None else None,
        "ranked_ideas": [group_payload(group) for group in ideas],
        "gaps": [asdict(gap) for gap in state["gaps"]],
    }


# ── Nodes (closured over injectable dependencies) ───────────────────────────


def _fetch_universe_node(universe: Sequence[str]) -> NodeFn:
    def fetch_universe(state: PipelineState) -> dict[str, Any]:
        normalized = sorted({ticker.strip().upper() for ticker in universe if ticker.strip()})
        if not normalized:
            raise PipelineError("universe is empty — set UNIVERSE in the environment/config")
        return {"universe": normalized}

    return fetch_universe


def _analyst_node(agent: BaseAgent) -> NodeFn:
    def analyze(state: PipelineState) -> dict[str, Any]:
        tickers = state["universe"]
        gaps: list[Gap] = []
        try:
            data = agent.collect(tickers)
        except Exception as exc:
            # The gap rule: a connector/agent failure is recorded, never raised
            # across the node boundary — one dead source never halts the run.
            gaps.append(Gap(source=agent.name, detail=f"collection failed: {exc}"))
            logger.warning("collector_failed", extra={"agent": agent.name, "error": str(exc)})
            data = {}
        gaps.extend(gaps_from_payload(agent.name, data.get("errors"), frozenset(tickers)))
        try:
            signals = agent.analyze(tickers, data)
        except Exception as exc:
            gaps.append(Gap(source=agent.name, detail=f"analysis failed: {exc}"))
            logger.warning("analyst_failed", extra={"agent": agent.name, "error": str(exc)})
            signals = []
        return {"raw_signals": signals, "gaps": gaps}

    return analyze


def _persist_node(repo: SignalRepository) -> NodeFn:
    def persist(state: PipelineState) -> dict[str, Any]:
        records = [
            ScoreRecord(
                ticker=None if group.ticker == MARKET_GROUP else group.ticker,
                score=group.result["score"],
                led_by=group.result["led_by"],
                red_flag=group.result["red_flag"],
                votes=group.result["votes"],
            )
            for group in state["scored"]
        ]
        signal_rows = repo.save_signals(state["run_id"], state["run_date"], state["signals"])
        score_rows = repo.save_score_records(state["run_id"], state["run_date"], records)
        logger.info(
            "audit_trail_persisted",
            extra={"run_id": state["run_id"], "signals": signal_rows, "score_records": score_rows},
        )
        return {}

    return persist


def _synthesize_node(pm: PortfolioManagerAgent) -> NodeFn:
    def synthesize(state: PipelineState) -> dict[str, Any]:
        return {"pm_synthesis": pm.synthesize(compact_json(synthesis_payload(state)))}

    return synthesize


def _write_brief_node(briefs_dir: Path) -> NodeFn:
    def write_brief(state: PipelineState) -> dict[str, Any]:
        text = render_brief(
            run_date=state["run_date"],
            universe=state["universe"],
            scored=state["scored"],
            gaps=state["gaps"],
            pm_synthesis=state["pm_synthesis"],
        )
        path = briefs_dir / f"{state['run_date']}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        logger.info("brief_written", extra={"path": str(path)})
        return {"brief": text, "brief_path": str(path)}

    return write_brief


# ── Graph assembly ──────────────────────────────────────────────────────────


def build_premarket_graph(
    *,
    universe: Sequence[str],
    analysts: Sequence[BaseAgent],
    pm: PortfolioManagerAgent,
    repo: SignalRepository,
    briefs_dir: Path,
    run_id: str,
    run_date: str,
) -> Any:
    """Compile the daily DAG; nodes run in-process with the injected deps."""
    builder = StateGraph(PipelineState)
    builder.add_node("fetch_universe", _fetch_universe_node(universe))
    for agent in analysts:
        name = f"analyze_{agent.name}"
        builder.add_node(name, _analyst_node(agent))
        builder.add_edge("fetch_universe", name)  # fan-out: analysts run in PARALLEL
        builder.add_edge(name, "validate_signals")  # fan-in
    builder.add_node("validate_signals", validate_signals)
    builder.add_node("score", score_node)
    builder.add_node("persist", _persist_node(repo))
    builder.add_node("synthesize", _synthesize_node(pm))
    builder.add_node("write_brief", _write_brief_node(briefs_dir))

    builder.add_edge(START, "fetch_universe")
    builder.add_edge("validate_signals", "score")
    builder.add_edge("score", "persist")
    builder.add_edge("persist", "synthesize")
    builder.add_edge("synthesize", "write_brief")
    builder.add_edge("write_brief", END)
    return builder.compile()


@dataclass(frozen=True)
class PremarketResult:
    """Everything a caller needs after one cycle."""

    run_id: str
    brief_path: Path
    brief: str
    scored: list[ScoredGroup]
    gaps: list[Gap]


def run_premarket(
    *,
    universe: Sequence[str],
    analysts: Sequence[BaseAgent],
    pm: PortfolioManagerAgent,
    repo: SignalRepository,
    briefs_dir: Path,
    run_id: str | None = None,
    run_date: str | None = None,
) -> PremarketResult:
    """One daily cycle: analysts → validate → score → persist → PM → brief."""
    resolved_id = run_id or uuid4().hex
    resolved_date = run_date or datetime.now(UTC).date().isoformat()
    graph = build_premarket_graph(
        universe=universe,
        analysts=analysts,
        pm=pm,
        repo=repo,
        briefs_dir=briefs_dir,
        run_id=resolved_id,
        run_date=resolved_date,
    )
    final = graph.invoke(
        {
            "run_id": resolved_id,
            "run_date": resolved_date,
            "universe": [],
            "raw_signals": [],
            "gaps": [],
            "signals": [],
            "scored": [],
            "pm_synthesis": "",
            "brief": "",
            "brief_path": "",
        }
    )
    return PremarketResult(
        run_id=resolved_id,
        brief_path=Path(final["brief_path"]),
        brief=final["brief"],
        scored=final["scored"],
        gaps=final["gaps"],
    )


# ── CLI ─────────────────────────────────────────────────────────────────────


def main() -> int:
    """Run one daily cycle against the configured environment.

    The composition root: the only place concrete SDK clients (Anthropic,
    Redis) and connectors are built. Settings provide the universe, model
    IDs, and DB/Redis URLs; the brief lands in settings.briefs_dir.
    """
    import anthropic
    from agents.anthropic_client import AnthropicLLM
    from agents.research.fundamentals import FundamentalsAgent
    from agents.research.macro_rates import MacroRatesAgent
    from agents.research.sec_filings import SecFilingsAgent
    from data.connectors.base import redis_client_from_url
    from data.connectors.edgar import EdgarConnector
    from data.connectors.fmp import FmpConnector
    from data.connectors.fred import FredConnector

    settings = Settings()
    llm = AnthropicLLM(anthropic.Anthropic())
    redis_client = redis_client_from_url(settings.redis_url)
    analysts: list[BaseAgent] = [
        MacroRatesAgent(
            llm=llm, settings=settings, fred=FredConnector(settings, redis_client=redis_client)
        ),
        SecFilingsAgent(
            llm=llm, settings=settings, edgar=EdgarConnector(settings, redis_client=redis_client)
        ),
        FundamentalsAgent(
            llm=llm,
            settings=settings,
            fmp=FmpConnector(settings, redis_client=redis_client) if settings.fmp_api_key else None,
        ),
    ]
    result = run_premarket(
        universe=settings.universe,
        analysts=analysts,
        pm=PortfolioManagerAgent(llm=llm, settings=settings),
        repo=PostgresSignalRepository.from_database_url(settings.database_url),
        briefs_dir=Path(settings.briefs_dir),
    )
    print(f"Pre-market brief written to {result.brief_path}")  # CLI output, not a log
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
