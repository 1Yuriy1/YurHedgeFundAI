"""SEC Filings analyst — tier 3, company-level hard facts from EDGAR.

Collects two views per universe company: recent submissions (the filings
delta) and an EFTS full-text sweep for red-flag phrases. The sweep is the
platform's auditable red-flag detection — the exact trigger phrases live in
``RED_FLAG_PHRASES`` and are repeated in the prompt — and any ticker whose
recent filings matched gets ``red_flag=True`` enforced on its signals in
code, not left to the model's discretion. Every signal links the filing
document URL as evidence.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Final

from core.config import Settings
from core.schemas import Signal
from data.connectors.base import ConnectorError
from data.connectors.edgar import (
    EdgarConnector,
    EftsHit,
    FilingRecord,
    filing_document_url,
    normalize_cik,
)

from agents.base import BaseAgent, LLMClient, compact_json

logger = logging.getLogger(__name__)

# The auditable red-flag trigger phrases, swept via EFTS full-text search and
# listed verbatim in prompts/sec_filings_v1.md so detection can be audited.
RED_FLAG_PHRASES: Final[tuple[str, ...]] = (
    "going concern",
    "restatement",
    "material weakness",
    "fraud",
    "substantial doubt",
    "SEC investigation",
)

# How far back the filings delta reaches; the EFTS window matches it.
DEFAULT_LOOKBACK_DAYS: Final[int] = 30

EVIDENCE_MAX_FILINGS: Final[int] = 5
EVIDENCE_MAX_HITS: Final[int] = 10


def recent_filings(records: list[FilingRecord], since_date: str) -> list[FilingRecord]:
    """Filings filed on/after ``since_date`` (ISO dates compare as strings)."""
    return [row for row in records if row["filing_date"] >= since_date]


def efts_hit_document_url(hit: EftsHit) -> str:
    """Canonical Archives URL for an EFTS hit, or "" when the id is unusable."""
    document = hit["id"].partition(":")[2]
    ciks = hit["ciks"]
    if not document or not ciks:
        return ""
    return filing_document_url(ciks[0], hit["accession"], document)


def attribute_hits(
    hits: list[EftsHit], ticker_by_cik: dict[str, str], *, phrase: str
) -> dict[str, list[dict[str, Any]]]:
    """Group EFTS hits onto universe tickers by CIK; off-universe hits drop."""
    attributed: dict[str, list[dict[str, Any]]] = {}
    for hit in hits:
        for cik in hit["ciks"]:
            ticker = ticker_by_cik.get(normalize_cik(cik))
            if ticker is None:
                continue
            attributed.setdefault(ticker, []).append(
                {
                    "phrase": phrase,
                    "form": hit["form"],
                    "file_date": hit["file_date"],
                    "accession_number": hit["accession"],
                    "url": efts_hit_document_url(hit),
                }
            )
    return attributed


def filing_payload_rows(cik: str, records: list[FilingRecord]) -> list[dict[str, Any]]:
    """Recent-filing rows with canonical evidence URLs for the LLM payload."""
    return [
        {
            "form": row["form"],
            "filing_date": row["filing_date"],
            "report_date": row["report_date"],
            "accession_number": row["accession_number"],
            "url": filing_document_url(cik, row["accession_number"], row["primary_document"]),
        }
        for row in records
    ]


class SecFilingsAgent(BaseAgent):
    """Tier 3: reads recent EDGAR filings; sets red flags from sweep hits."""

    name: ClassVar[str] = "sec_filings"
    prompt_filename: ClassVar[str] = "sec_filings_v1.md"

    def __init__(
        self,
        *,
        llm: LLMClient,
        settings: Settings,
        edgar: EdgarConnector,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    ) -> None:
        super().__init__(llm=llm, settings=settings)
        self._edgar = edgar
        self._lookback_days = lookback_days

    def collect(self, tickers: list[str]) -> dict[str, Any]:
        """Submissions delta + red-flag phrase sweep; failures become gaps."""
        since_date = (datetime.now(UTC) - timedelta(days=self._lookback_days)).date().isoformat()
        ciks = {ticker: cik for ticker in tickers if (cik := self._settings.edgar_ciks.get(ticker))}
        ticker_by_cik = {normalize_cik(cik): ticker for ticker, cik in ciks.items()}

        filings: dict[str, list[FilingRecord]] = {}
        submission_errors: dict[str, str] = {}
        for ticker, cik in ciks.items():
            try:
                filings[ticker] = recent_filings(self._edgar.fetch_submissions(cik), since_date)
            except ConnectorError as exc:
                submission_errors[ticker] = str(exc)
                logger.warning(
                    "connector_gap",
                    extra={
                        "agent": self.name,
                        "source": "edgar_submissions",
                        "ticker": ticker,
                        "error": str(exc),
                    },
                )

        red_flag_hits: dict[str, list[dict[str, Any]]] = {}
        efts_errors: dict[str, str] = {}
        for phrase in RED_FLAG_PHRASES:
            try:
                hits = self._edgar.full_text_search(phrase, start_date=since_date, limit=50)
            except ConnectorError as exc:
                efts_errors[phrase] = str(exc)
                logger.warning(
                    "connector_gap",
                    extra={
                        "agent": self.name,
                        "source": "edgar_efts",
                        "phrase": phrase,
                        "error": str(exc),
                    },
                )
                continue
            for ticker, rows in attribute_hits(hits, ticker_by_cik, phrase=phrase).items():
                red_flag_hits.setdefault(ticker, []).extend(rows)

        return {
            "since_date": since_date,
            "filings": filings,
            "red_flag_hits": red_flag_hits,
            "errors": {"submissions": submission_errors, "efts": efts_errors},
        }

    def analyze(self, tickers: list[str], data: dict[str, Any]) -> list[Signal]:
        """One filing-summary call; red flags from sweep hits enforced in code."""
        filings: dict[str, list[FilingRecord]] = data.get("filings", {})
        red_flag_hits: dict[str, list[dict[str, Any]]] = data.get("red_flag_hits", {})
        companies: dict[str, Any] = {}
        for ticker in tickers:
            rows = filings.get(ticker, [])
            hits = red_flag_hits.get(ticker, [])
            if not rows and not hits:
                continue
            cik = self._settings.edgar_ciks.get(ticker, "")
            companies[ticker] = {
                "cik": cik,
                "recent_filings": filing_payload_rows(cik, rows)[:EVIDENCE_MAX_FILINGS],
                "red_flag_hits": hits[:EVIDENCE_MAX_HITS],
            }
        if not companies:
            logger.warning(
                "analysis_skipped_no_data",
                extra={"agent": self.name, "reason": "no recent filings or sweep hits collected"},
            )
            return []
        payload = {
            "instructions": (
                "Summarize each company's recent filings with severity, quote the key "
                "language, and set red_flag per the red-flag trigger phrases; cite the "
                "filing URLs provided. Respond per the system prompt."
            ),
            "companies": companies,
            "red_flag_phrases": list(RED_FLAG_PHRASES),
            "errors": data.get("errors", {}),
        }
        signals = self.request_signals(compact_json(payload))
        return self._enforce_red_flags(signals, set(red_flag_hits))

    def _enforce_red_flags(self, signals: list[Signal], swept_tickers: set[str]) -> list[Signal]:
        """Code-enforced red flags: sweep hits upgrade, never the model alone.

        The sweep is the auditable detection path; if the model produced a
        signal for a swept ticker without flagging it, the flag is set here so
        scoring's rule 3 (red flag overrides everything) cannot be missed.
        """
        enforced: list[Signal] = []
        for signal in signals:
            if signal.ticker in swept_tickers and not signal.red_flag:
                logger.warning(
                    "red_flag_enforced",
                    extra={"agent": self.name, "ticker": signal.ticker},
                )
                signal = signal.model_copy(update={"red_flag": True})
            enforced.append(signal)
        return enforced


__all__ = [
    "DEFAULT_LOOKBACK_DAYS",
    "RED_FLAG_PHRASES",
    "SecFilingsAgent",
    "attribute_hits",
    "efts_hit_document_url",
    "filing_payload_rows",
    "recent_filings",
]
