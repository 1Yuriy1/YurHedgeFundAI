# Build Spec Summary — Phases 1–2

> **Source of truth:** the Blueprint artifact **"YurHedgeFundAI Build Spec — Phases 1–2"** (Obvious project artifact `art_DF0KPiY9`, v8, September 17, 2026). This page is a repo-friendly synopsis; the Blueprint is authoritative, and the Master Plan PDF ("YurHedgeFundAI — Master Plan.pdf" — 27 agents, opinion ladder, gates, roadmap) remains the design authority it defers to.

## Scope

**In this increment**

- Repo scaffold and docs baseline (this PR)
- Docker Compose foundation: Postgres + pgvector, Redis
- Shared Pydantic `Signal` schema with evidence-mandatory validation (`core/schemas.py`)
- `BaseAgent` contract (`agents/base.py`)
- Connectors: FRED, SEC EDGAR (submissions + EFTS), GDELT, optional FMP (`data/connectors/`)
- Three analyst agents: Macro & Rates (T2), SEC Filings (T3), Fundamentals (T4) (`agents/research/`)
- Opinion-ladder scorer — all seven tiers, plan rules 1–3 (`core/scoring.py`)
- PM daily-brief pipeline via LangGraph (`pipelines/premarket.py`, `agents/leadership/portfolio_manager.py`)
- pytest suite and CI

**Out (later phases)**

- Sentiment / X / Reddit agents, Technical, Stat Arb, quant desk
- Risk Manager / Devil's Advocate / Compliance / IC gates as LLM agents
- Streamlit dashboard, alerts, paper-trade execution, weight tuning

The scoring module accepts a `validation` dict from day one so the quant layer slots in later without rework.

## The six components

1. **Shared signal schema — `core/schemas.py`.** The plan's Signal JSON as a Pydantic v2 model; the evidence rule is a validator, not a convention: a signal with no evidence item, or an evidence item with no URL, raises at construction. Naive datetimes coerce to UTC.
2. **Agent base — `agents/base.py`.** One abstract base: LLM client (model chosen per role from config), a versioned prompt loaded from `prompts/`, optional data-fetch tools, and a two-step contract — `collect()` (pure data, no LLM) then `analyze()` (raw data in, schema-validated Signals out).
3. **Connectors — `data/connectors/`.** Thin HTTP clients returning plain dicts, with the research notes' pinned behavior baked in: FRED observations (string values, `"."` → None), EDGAR submissions + defensively-parsed EFTS with a descriptive User-Agent and ≤5 req/s, GDELT DOC API (no key, 5 s pacing, 250-record cap), optional FMP (250 req/day budget). Shared Redis caching, exponential backoff on 429/403, per-connector rate limits.
4. **Analysts — `agents/research/`.** Tier 2 `macro_rates` (FRED regime classification, market-wide signals), Tier 3 `sec_filings` (8-K/10-Q/10-K summaries, red-flag capable), Tier 4 `fundamentals` (FMP, or yfinance fallback with lower confidence). Malformed LLM output is retried once, then dropped and logged — a dropped signal is better than an invalid one.
5. **Opinion-ladder scorer — `core/scoring.py`.** The plan's aggregation code adopted verbatim in behavior — TIERS, WEIGHTS, CAP_CONFIDENCE=0.7, DAMPEN=0.25, red-flag override, no-flip past the leader — with the plan's tier-level double-counting bug fixed: votes accumulate per tier, then the tier is weighted.
6. **PM pipeline — `pipelines/premarket.py` + `agents/leadership/portfolio_manager.py`.** LangGraph orchestrates one deterministic pre-market cycle: fetch universe → three analysts in parallel → collect Signals → `aggregate()` → the PM (the only Opus call) writes `output/briefs/YYYY-MM-DD.md` with ranked ideas, evidence links, and an explicit gaps section. Signals and scores persist to Postgres; Redis holds cache/rate-limit state.

## Acceptance criteria

| Deliverable | Acceptance criterion | Verified by |
|---|---|---|
| Signal schema | A signal with empty evidence or a URL-less evidence item raises `ValidationError`; naive datetimes are coerced to UTC; all fields from the plan's JSON schema are present | Unit tests, `tests/test_schemas.py` |
| Scoring | Red-flag override forces −1.0; cap 0.7 + dampen 0.25 behave per the plan's worked example; opposite lower-tier votes cannot flip past the leader; equal-tier agents average; empty input returns a documented sentinel, not a crash | Exhaustive unit tests, `tests/test_scoring.py`, including the plan's own example numbers |
| Connectors | Each client parses a recorded fixture correctly: FRED string/`"."` values, EDGAR submissions columnar arrays, EFTS `hits.hits[]`, GDELT article list. 429/403 triggers backoff; FRED `file_type=json` always sent; EDGAR UA header present and ≤5 req/s enforced | Unit tests with `responses`/`respx` fixtures, `tests/test_connectors/` |
| Analysts | Given fixture data and a stubbed LLM, each agent returns schema-valid Signals; malformed LLM output is retried once then dropped with a log entry; prompts are loaded from `prompts/` | Unit tests, `tests/test_agents/` |
| PM pipeline | An offline end-to-end run (fixtures + stub LLM) produces `output/briefs/<date>.md` containing: ranked ideas with scores matching `aggregate()` output, one evidence URL per claim, and a gaps section when a fixture connector fails | Integration test, `tests/test_premarket.py` |
| Foundation | `docker compose up` starts Postgres+pgvector and Redis; the app's config module reads all settings from environment with a documented `.env.example` | CI job validates compose config; smoke test touches both services |

## CI design

One workflow, two parallel jobs, **no live-network integration tests in CI**:

- **quality** (blocking, ~1–2 min): ruff lint + format check, then mypy, then pytest — fail-fast ordering.
- **security** (non-blocking initially, flip to blocking post-first-release): pip-audit dependency scan.
- **Caching from day one:** pip cache configured now; the Docker layer cache lands with the first Dockerfile.
- **Excluded:** live EDGAR/FRED/GDELT integration (rate limits, flakiness); migration validation (no schema migrations until the pgvector DDL phase).

## Locked decisions

1. **LangGraph orchestrates; the Anthropic SDK calls the model inside nodes.** The Agent SDK is wrong-shaped for a deterministic daily DAG (research notes §2).
2. **Model per role:** PM → Opus 5, analysts → Sonnet 5, bulk sentiment (later) → Haiku 4.5. Model IDs live in config, never hardcoded.
3. **The plan's scoring code is law** — TIERS, WEIGHTS, CAP_CONFIDENCE 0.7, DAMPEN 0.25, red-flag override — with the tier-aggregation fix documented in the spec.
4. **Evidence-mandatory schema validation** in Pydantic; no URL, no signal.
5. **Free-data-first:** FRED + EDGAR + GDELT work without paid keys; FMP optional (250 req/day); Alpha Vantage rejected (25 req/day).
6. **Paper-trading only.** No order execution, no alerts, no dashboard in this increment.
7. **CI runs offline** — recorded fixtures and stub LLMs only.

## Open items (flagged in research, resolved at implementation time)

- **a.** Exact Anthropic API model-ID strings (`claude-opus-5` etc.) — confirm on the models overview page before the first API call; config makes this a one-line change.
- **b.** GDELT rate-limit figure is community-documented; the 5 s pacing is safe either way.
- **c.** EFTS full-text-search response schema is unofficial and changeable; parsing is deliberately defensive.
- **d.** Secrets (`ANTHROPIC_API_KEY`, `FRED_API_KEY`, optional `FMP_API_KEY`, `EDGAR_USER_AGENT`) are requested via the secrets flow before the first live run; unit tests never need them.
