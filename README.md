# YurHedgeFundAI

A research-first, multi-agent hedge fund intelligence platform. Specialist analyst agents (Macro & Rates, SEC Filings, Fundamentals) research a fixed universe of tickers, an 'opinion ladder' of tiered expertise aggregates their views into a score, and a Portfolio Manager agent publishes a daily, evidence-backed markdown brief.

**Paper-trading only. This system produces research, never trades.**

## Principles
- **Evidence or it doesn't exist.** Every signal must cite at least one URL-backed source; the schema rejects signals without evidence.
- **The opinion ladder decides.** Tiers 1–7 weight agents by expertise; a Tier 3 red flag (restatement, fraud language, going concern) overrides the score to maximum bearish.
- **Free data first.** FRED, SEC EDGAR, and GDELT work with no paid keys. Financial Modeling Prep (250 req/day free) is optional, behind a config flag.

## Status
Under construction — Phases 1–2. See docs/build-spec-summary.md for the approved scope. Roadmap: scaffold/CI (in progress) → Signal schema + ladder scorer → FRED/EDGAR/GDELT/FMP connectors → three analyst agents → PM daily brief.

## Architecture
universe (config) -> Macro·T2 (FRED), SEC Filings·T3 (EDGAR), Fundamentals·T4 (FMP) in parallel -> validate (Pydantic) -> opinion-ladder score (core/scoring.py) -> PM brief .md (Opus).

Orchestration: LangGraph (deterministic daily pipeline). Models: Opus 5 (PM), Sonnet 5 (analysts) — model IDs in config, never hardcoded. Storage: Postgres + pgvector (signals, scores, raw payloads), Redis (cache, rate limiting).

## Quickstart
cp .env.example .env   # add ANTHROPIC_API_KEY, FRED_API_KEY (optional: FMP_API_KEY)
docker compose up -d   # Postgres+pgvector, Redis
pip install -e '.[dev]'
pytest                 # full suite runs offline — fixtures + stub LLM, no keys needed
python -m pipelines.premarket   # run one daily cycle (requires API keys)

## Docs
- docs/research-notes.md — cited external facts: model pricing, EDGAR/FRED/GDELT endpoints and rate limits, provider free tiers
- docs/build-spec-summary.md — Phases 1–2 scope, acceptance criteria, CI design

## Source of truth
'YurHedgeFundAI — Master Plan.pdf' (27 agents, opinion ladder, gates, roadmap) plus the Build Spec Blueprint. The scoring rules in core/scoring.py implement the plan's code exactly: TIERS, WEIGHTS, CAP_CONFIDENCE=0.7, DAMPEN=0.25, red-flag override, no-flip past the leader.

---
*Built with [Obvious](https://app.obvious.ai). Human author: Yuriy Deyneka.*
