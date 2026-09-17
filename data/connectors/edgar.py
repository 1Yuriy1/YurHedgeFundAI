"""SEC EDGAR connector — submissions JSON + EFTS full-text search.

Research notes §3:
* Submissions: ``https://data.sec.gov/submissions/CIK{cik}.json`` returns up
  to 1,000 recent filings as **columnar arrays** (one array per field);
  ``parse_submissions`` zips them back into row dicts.
* EFTS full-text search (``https://efts.sec.gov/LATEST/search-index``) is
  effectively **undocumented** — the SEC reserves the right to change it —
  so ``parse_efts_hits`` is deliberately defensive: ``hits.hits[]`` may be
  missing, empty, or malformed, and no key is assumed.
* Every request must carry a descriptive User-Agent (format
  ``Organization/Project Contact-email``) and stay under the official
  10 req/s cap — this client enforces 5 req/s with jitter client-side.
"""

import logging
from typing import Any, Final, TypedDict

from core.config import Settings

from data.connectors.base import BaseConnector, ConnectorError, DisabledConnectorError

logger = logging.getLogger(__name__)

EDGAR_SUBMISSIONS_URL: Final[str] = "https://data.sec.gov/submissions"
EFTS_SEARCH_URL: Final[str] = "https://efts.sec.gov/LATEST/search-index"

# Half the official 10 req/s cap (research notes §3), plus small per-call
# jitter so bursts don't tick in lockstep.
EDGAR_MAX_REQUESTS_PER_SECOND: Final[float] = 5.0
EDGAR_REQUEST_JITTER_SECONDS: Final[float] = 0.05
EDGAR_CACHE_TTL_SECONDS: Final[float] = 3600.0

SUBMISSION_COLUMNS: Final[tuple[str, ...]] = (
    "accessionNumber",
    "filingDate",
    "reportDate",
    "form",
    "primaryDocument",
)


class FilingRecord(TypedDict):
    """One row of EDGAR's columnar recent-filings arrays."""

    accession_number: str
    filing_date: str
    report_date: str | None
    form: str
    primary_document: str


class EftsHit(TypedDict):
    """One EFTS full-text-search hit, defensively flattened."""

    id: str
    score: float | None
    form: str
    file_date: str
    accession: str
    ciks: list[str]
    display_names: list[str]


def normalize_cik(cik: str) -> str:
    """Zero-pad any CIK spelling ("320193", "0000320193", "CIK0000320193") to 10 digits."""
    digits = cik.strip().lower().removeprefix("cik")
    if not digits.isdigit():
        raise ConnectorError(f"invalid CIK: {cik!r}")
    return digits.zfill(10)


def parse_submissions(payload: dict[str, Any]) -> list[FilingRecord]:
    """Zip EDGAR's columnar arrays into row dicts.

    ``zip`` stops at the shortest array, so unequal-length columns degrade to
    fewer rows instead of a crash.
    """
    filings = payload.get("filings")
    recent = filings.get("recent") if isinstance(filings, dict) else None
    if not isinstance(recent, dict):
        return []
    arrays = [recent.get(column, []) for column in SUBMISSION_COLUMNS]
    records: list[FilingRecord] = []
    # strict=False on purpose: zip stops at the shortest column, so
    # unequal-length arrays degrade to fewer rows instead of a crash.
    for accession, filing_date, report_date, form, primary_document in zip(*arrays, strict=False):
        records.append(
            FilingRecord(
                accession_number=str(accession),
                filing_date=str(filing_date),
                report_date=str(report_date) if report_date else None,
                form=str(form),
                primary_document=str(primary_document),
            )
        )
    return records


def parse_efts_hits(payload: dict[str, Any]) -> list[EftsHit]:
    """Defensively flatten EFTS hits.

    The EFTS schema is unofficial and changeable (research notes §3): missing
    ``hits``, a non-dict ``hits``, a non-list ``hits.hits``, or malformed
    entries all degrade to ``[]`` / skipped rows — never a KeyError.
    """
    hits = payload.get("hits")
    if not isinstance(hits, dict):
        return []
    entries = hits.get("hits")
    if not isinstance(entries, list):
        return []
    parsed: list[EftsHit] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        source = entry.get("_source")
        source = source if isinstance(source, dict) else {}
        raw_score = entry.get("_score")
        score = float(raw_score) if isinstance(raw_score, int | float) else None
        ciks = source.get("ciks")
        names = source.get("display_names")
        parsed.append(
            EftsHit(
                id=str(entry.get("_id", "")),
                score=score,
                form=str(source.get("form", "")),
                file_date=str(source.get("file_date", "")),
                accession=str(source.get("adsh", "")),
                ciks=[str(c) for c in ciks] if isinstance(ciks, list) else [],
                display_names=[str(n) for n in names] if isinstance(names, list) else [],
            )
        )
    return parsed


def filing_document_url(cik: str, accession_number: str, primary_document: str) -> str:
    """Canonical Archives URL for a filing's primary document (for evidence links)."""
    if not cik or not accession_number or not primary_document:
        return ""
    return (
        f"https://www.sec.gov/Archives/edgar/data/{int(normalize_cik(cik))}"
        f"/{accession_number.replace('-', '')}/{primary_document}"
    )


class EdgarConnector(BaseConnector):
    """SEC EDGAR client with the mandatory descriptive User-Agent and 5 req/s cap."""

    name = "edgar"
    default_cache_ttl = EDGAR_CACHE_TTL_SECONDS
    min_request_interval = 1.0 / EDGAR_MAX_REQUESTS_PER_SECOND
    request_jitter = EDGAR_REQUEST_JITTER_SECONDS

    def __init__(self, settings: Settings, **options: Any) -> None:
        if not settings.edgar_user_agent:
            raise DisabledConnectorError(
                "EDGAR_USER_AGENT is not set — SEC blocks undeclared automated tools; "
                "set it to 'Organization/Project Contact-email' (see .env.example)"
            )
        # Must be set before super().__init__ — the base constructor builds
        # headers through the overridden _default_headers(), which reads it.
        self._user_agent: str = settings.edgar_user_agent
        super().__init__(**options)

    def _default_headers(self) -> dict[str, str]:
        """Override the generic UA with the env-configured descriptive one."""
        return {**super()._default_headers(), "User-Agent": self._user_agent}

    def fetch_submissions(self, cik: str) -> list[FilingRecord]:
        """Fetch and parse a company's recent submissions (columnar arrays)."""
        padded = normalize_cik(cik)
        payload = self._get_json(f"{EDGAR_SUBMISSIONS_URL}/CIK{padded}.json")
        if not isinstance(payload, dict):
            raise ConnectorError(f"edgar: unexpected submissions response for CIK {padded}")
        return parse_submissions(payload)

    def full_text_search(
        self,
        query: str,
        *,
        forms: list[str] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        ciks: list[str] | None = None,
        limit: int = 10,
    ) -> list[EftsHit]:
        """EFTS full-text search; dates are YYYY-MM-DD, CIKs are zero-padded."""
        params: dict[str, str] = {"q": query, "size": str(max(1, limit))}
        if forms:
            params["forms"] = ",".join(forms)
        if start_date is not None:
            params["startdt"] = start_date
        if end_date is not None:
            params["enddt"] = end_date
        if ciks:
            params["ciks"] = ",".join(normalize_cik(c) for c in ciks)
        payload = self._get_json(EFTS_SEARCH_URL, params=params)
        if not isinstance(payload, dict):
            return []  # Non-object body from an unofficial API — treat as no hits.
        return parse_efts_hits(payload)


__all__ = [
    "EDGAR_MAX_REQUESTS_PER_SECOND",
    "EFTS_SEARCH_URL",
    "EdgarConnector",
    "EftsHit",
    "FilingRecord",
    "filing_document_url",
    "normalize_cik",
    "parse_efts_hits",
    "parse_submissions",
]
