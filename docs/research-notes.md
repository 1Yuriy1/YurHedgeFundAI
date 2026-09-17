# YurHedgeFundAI Research Notes

Verified external facts for the build spec, each with a source URL. Research date: **September 17, 2026**. Everything below was retrieved from live web sources this session unless explicitly flagged as unverified.

---

## 1. Anthropic API — model IDs and pricing

Intent mapping from the Master Plan: **strongest → portfolio-manager agent**, **mid-tier → analyst agents**, **cheap → bulk sentiment scoring**. Current lineup (as of September 2026):

| Intent | Model | Model ID | Input / 1M tokens | Output / 1M tokens | Context |
|---|---|---|---|---|---|
| Strongest (PM) | Claude Opus 5 | `claude-opus-5` | $5.00 | $25.00 | 1M / 128K max output |
| Mid-tier (analysts) | Claude Sonnet 5 | `claude-sonnet-5` | $2.00 | $10.00 | 1M / 128K max output |
| Cheap bulk (sentiment) | Claude Haiku 4.5 | `claude-haiku-4.5` | $1.00 | $5.00 | 200K / 64K max output |

- Official models overview (lineup, context windows, per-model pricing table): https://platform.claude.com/docs/en/models/overview
- Official pricing page (per-MTok rates incl. cache read/write): https://platform.claude.com/docs/en/about-claude/pricing
- Sonnet 5's introductory $2/$10 pricing was made **permanent** (edit dated Aug 10, 2026): https://www.anthropic.com/news/claude-sonnet-5
- Prompt-caching discounts apply (e.g. Haiku 4.5 cache read $0.10/MTok) — relevant for the repeated daily brief prompts: https://claude.com/pricing
- Batch processing gives 50% cost savings on Haiku 4.5 for bulk sentiment jobs: https://www.anthropic.com/claude/haiku

⚠️ **Flag (partially verified):** the exact API model-ID strings above (`claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4.5`) match the current docs lineup naming, but the retrieved snippets did not all show the literal API ID string for Opus 5 / Sonnet 5. Before pinning them in the spec, confirm the literal IDs on the models overview page: https://platform.claude.com/docs/en/models/overview

---

## 2. Claude Agent SDK (Python) vs LangGraph

**Package:** `claude-agent-sdk` (PyPI), install: `pip install claude-agent-sdk`, requires Python 3.10+. The Claude Code CLI is bundled with the package automatically.
- Repo: https://github.com/anthropics/claude-agent-sdk-python
- Docs: https://code.claude.com/docs/en/agent-sdk/python and quickstart: https://code.claude.com/docs/en/agent-sdk/quickstart
- Current release example (0.2.x line): https://github.com/anthropics/claude-agent-sdk-python/releases

**Minimal agent with tools** (pattern from the official docs/README):

```python
import anyio
from claude_agent_sdk import query, ClaudeAgentOptions


async def main():
    async for message in query(
        prompt="Summarize today's FRED releases and flag anomalies",
        options=ClaudeAgentOptions(
            allowed_tools=["Read", "Bash", "Grep"],
            permission_mode="acceptEdits",
        ),
    ):
        print(message)


anyio.run(main)
```

Source for the pattern: https://github.com/anthropics/claude-agent-sdk-python and https://code.claude.com/docs/en/agent-sdk/quickstart

**Recommendation for this project: LangGraph.** For a structured, deterministic daily pipeline with explicit stages, shared state, and auditability, LangGraph is the better fit and is more comprehensively documented (graph API, workflows-vs-agents guidance, quickstart, API reference). The Claude Agent SDK is oriented toward autonomous agent loops and Claude Code-style tool use — better for exploratory research agents than a fixed daily DAG.
- LangGraph repo and docs: https://github.com/langchain-ai/langgraph
- Workflows vs agents guidance: https://docs.langchain.com/oss/python/langgraph/workflows-agents
- Graph API overview: https://docs.langchain.com/oss/python/langgraph/graph-api
- Quickstart: https://docs.langchain.com/oss/python/langgraph/quickstart

They are not mutually exclusive: LangGraph orchestrates the deterministic pipeline; the Anthropic SDK (or the Agent SDK, scoped to a single node) provides the model calls inside nodes.

---

## 3. SEC EDGAR

### Submissions JSON API
- Endpoint: `https://data.sec.gov/submissions/CIK##########.json` where `##########` is the 10-digit zero-padded CIK (e.g. Apple: `https://data.sec.gov/submissions/CIK0000320193.json`).
- Official doc page: https://www.sec.gov/search-filings/edgar-application-programming-interfaces (also https://www.sec.gov/about/developer-resources)
- Response shape: company metadata (current/former name, ticker, exchange, SIC) plus a `filings.recent` object holding up to 1,000 of the most recent filings in columnar arrays (form, filingDate, accessionNumber, primaryDocument, …); older filings are paginated into additional JSON files referenced from the response.
- Data portal home: https://data.sec.gov/

### Full-text search API (EFTS)
- Endpoint: `https://efts.sec.gov/LATEST/search-index?q=<query>` with parameters `q`, `forms` (e.g. `10-K`), `startdt` / `enddt` (YYYY-MM-DD), `ciks` (comma-separated zero-padded CIKs), `from` / `size` for pagination.
- Example: `https://efts.sec.gov/LATEST/search-index?q=%22material+weakness%22&forms=10-K&startdt=2026-01-01&enddt=2026-06-30`
- Response shape: Elasticsearch-style — `hits.total.value`, `hits.hits[]`, each hit with `_id`, `_score`, and `_source` containing `ciks`, `display_names`, `form`, `file_date`, `adsh` (accession number), `file_type`, plus `highlight`.
- SEC's FAQ for full-text search: https://www.sec.gov/edgar/search/efts-faq.html
- Documented response-field breakdowns (third-party, since SEC does not publish a schema): https://orthogonal.info/sec-edgar-full-text-search-api-efts-python/ and https://themineworks.com/blog/sec-edgar-full-text-search-api/

⚠️ **Flag:** the EFTS `search-index` endpoint is effectively **undocumented by the SEC** — the SEC reserves the right to change it without notice (noted by https://edgarkit.com/learn/edgar-full-text-search). The spec should pin the response parsing loosely (defensive parsing of `hits.hits[]`) and avoid depending on unstated fields.

### User-Agent policy (required)
- Every request must carry a descriptive `User-Agent` header declaring the requester; undeclared automated tools get blocked. The SEC's sample format for a declared bot: **`Sample Company Name AdminContact@<sample company domain>.com`** — i.e. `Organization/Project-name Version Contact-email`.
- Documented at: https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data and https://www.sec.gov/Archives/edgar/data/1131312/000113131206000109/faq3.htm ("Your Request Originates from an Undeclared Automated Tool")

### Rate limits
- **Maximum 10 requests per second**, regardless of the number of machines/IPs; exceeding it can block the IP (typically auto-lifted after ~10 minutes). Sources: https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data and https://www.sec.gov/filergroup/announcements-old/new-rate-control-limits
- Practical guidance: stay well under the cap (e.g. ≤5 req/s with jitter), and back off on HTTP 429/403.

---

## 4. FRED (Federal Reserve Economic Data)

- **API key registration:** free account + key at https://fredaccount.stlouisfed.org/apikeys (documented via https://fred.stlouisfed.org/docs/api/api_key.html). Key is a 32-char alphanumeric string, issued immediately.
- **Observations endpoint:**
  ```
  GET https://api.stlouisfed.org/fred/series/observations?series_id=UNRATE&api_key=YOUR_KEY&file_type=json&observation_start=2020-01-01
  ```
  Official endpoint doc: https://fred.stlouisfed.org/docs/api/fred/series_observations.html
- Parameters: `series_id` (required), `api_key` (required), `file_type` (`json` — default response is XML, so always pass `file_type=json`), `observation_start` / `observation_end` (YYYY-MM-DD), `limit`, `offset`, `sort_order`, `units`, `frequency`, `aggregation_method`, `realtime_start` / `realtime_end`.
- Response shape: metadata block plus `observations[]`, each item `{"realtime_start": "...", "realtime_end": "...", "date": "YYYY-MM-DD", "value": "..."}` — **values are strings**; missing data is `"."`. (Shape documented across the endpoint doc above and https://fred-py-api.readthedocs.io/en/stable/series.html)

---

## 5. GDELT — free programmatic news/event access

The free, no-key method is the **GDELT 2.0 DOC API** (article-level full-text search over worldwide online news, ~100 languages, rolling ~3-month window):

- Endpoint: `https://api.gdeltproject.org/api/v2/doc/doc`
- Example query URL:
  ```
  https://api.gdeltproject.org/api/v2/doc/doc?query=("Federal Reserve" OR "rate cut")&mode=artlist&maxrecords=250&timespan=1week&format=json
  ```
- Key parameters: `query` (supports quoted phrases, OR, operators), `mode` (`artlist` for articles, `timelinevol` etc. for volume timelines), `maxrecords` (hard cap **250** per request), `timespan` (e.g. `24h`, `3d`, `1w`), `format` (`json`).
- Official debut post with example URLs: https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
- Project data-access overview: https://gdeltproject.org/data.html and https://gdelt.github.io/
- **No API key required**; rate-limited — community-documented guidance is roughly **1 request per 5 seconds per IP**, with HTTP 429 on excess and no pagination cursor past 250 records (split queries by time window instead): https://github.com/cyanheads/gdelt-mcp-server and https://github.com/Query-farm/vgi-news
- ⚠️ **Flag:** the 1 req/5s figure is community-documented, not on an official GDELT spec page; the official statement is that hosted APIs have rate-limited quotas (https://blog.gdeltproject.org/behind-the-scenes-api-quotas-the-impact-of-a-fraction-of-a-qps/). Build the client with 5s pacing + backoff either way.
- RSS-style output exists via DOC API formats (e.g. `format=rssarchive`); if the pipeline prefers RSS ingestion, verify the exact format parameter against live behavior before pinning — not independently confirmed this session.

---

## 6. Fundamentals prototyping — free-tier limits

| Provider | Free tier | Source |
|---|---|---|
| Financial Modeling Prep (FMP) | **250 requests/day** (resets every 24h, perpetual free plan), 300 calls/minute burst, ~5 years of statements, end-of-day data | https://site.financialmodelingprep.com/developer/docs/pricing and https://site.financialmodelingprep.com/faqs |
| Alpha Vantage | **25 requests/day** and 5 requests/minute | https://www.alphavantage.co/support/ and https://www.macroption.com/alpha-vantage-api-limits/ |

- FMP's free plan is US-focused with limited endpoint coverage — fine for fundamentals prototyping, tight for a multi-ticker universe: https://site.financialmodelingprep.com/education/data/when-financial-modeling-prep-is-the-right-tool--and-when-it-isnt
- Alpha Vantage's 25/day cap means a 50-ticker daily fundamentals sweep will not fit in one day's quota even with caching — plan FMP as primary and Alpha Vantage as a secondary/manual source, or cache aggressively (30-day TTL makes a 50-symbol portfolio average ~2 req/day: https://git.lerch.org/lobo/zfin/src/commit/8b6e10ea100359245c8710301153b45c898f711b/README.md).

---

## 7. Open-source multi-agent trading references (architecture comparison)

1. **TradingAgents — Tauric Research** (`TauricResearch/TradingAgents`): multi-agent LLM financial trading framework modeling a trading firm — analyst agents (fundamentals, sentiment, technical, news) feeding researcher/debate agents, a trader, and risk management, mirroring the PM/analyst split in the Master Plan. Comes with an arXiv paper ("TradingAgents: Multi-Agents LLM Financial Trading Framework").
   - Repo: https://github.com/TauricResearch/TradingAgents/
   - Docs site: https://tauricresearch.github.io/TradingAgents/
2. **ai-hedge-fund — virattt** (`virattt/ai-hedge-fund`): open-source collection of hedge-fund-style analyst agents (each an investing persona with its own strategy) that produce signals aggregated into a portfolio view; educational, paper-trading oriented. Useful as a lighter-weight reference for the analyst-agent → signal-aggregation pattern.
   - Repo: https://github.com/virattt/ai-hedge-fund
   - Architecture overview: https://deepwiki.com/virattt/ai-hedge-fund

---

## Unverified / open items (do not pin in spec without confirmation)

1. Exact literal Anthropic API model-ID strings for Opus 5 / Sonnet 5 (see flag in §1) — confirm on https://platform.claude.com/docs/en/models/overview
2. GDELT's exact published rate-limit number (1 req/5s is community-documented, not official) — see §5.
3. GDELT RSS format parameter (`rssarchive`) — verify against live behavior before using RSS ingestion.
4. EFTS full-text search response schema is unofficial and changeable — parse defensively (§3).

