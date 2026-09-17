"""GDELT 2.0 DOC API connector — keyless news/article search.

Research notes §5: the DOC API needs no key but is rate-limited — community
guidance is ~1 request per 5 seconds per IP, with HTTP 429 on excess and no
pagination past 250 records (split queries by time window instead). This
client paces at 5 s minimum between calls and caps results at 250 per query.

GDELT occasionally answers HTTP 200 with non-JSON or non-article bodies, so
``parse_articles`` treats anything without an ``articles`` array as no results.
"""

import json
import logging
from typing import Final, TypedDict

from data.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

GDELT_DOC_URL: Final[str] = "https://api.gdeltproject.org/api/v2/doc/doc"
# Community-documented guidance (~1 req/5s, research notes §5); the 429
# backoff in the shared base is the safety net either way.
GDELT_MIN_INTERVAL_SECONDS: Final[float] = 5.0
GDELT_MAX_RECORDS: Final[int] = 250
GDELT_CACHE_TTL_SECONDS: Final[float] = 900.0  # 15 min — news moves fast


class GdeltArticle(TypedDict):
    """One artlist row, flattened to the fields the analysts need."""

    title: str
    url: str
    seendate: str
    source: str
    language: str
    source_country: str


def parse_articles(text: str, *, cap: int = GDELT_MAX_RECORDS) -> list[GdeltArticle]:
    """Parse an artlist JSON body defensively; anything odd becomes ``[]``.

    GDELT's ``domain`` field is surfaced as ``source`` (the brief's naming).
    """
    try:
        payload = json.loads(text)
    except ValueError:
        logger.warning("gdelt: non-JSON response body treated as no results")
        return []
    if not isinstance(payload, dict):
        return []
    articles = payload.get("articles")
    if not isinstance(articles, list):
        return []
    parsed: list[GdeltArticle] = []
    for entry in articles:
        if not isinstance(entry, dict):
            continue
        parsed.append(
            GdeltArticle(
                title=str(entry.get("title", "")),
                url=str(entry.get("url", "")),
                seendate=str(entry.get("seendate", "")),
                source=str(entry.get("domain", "")),
                language=str(entry.get("language", "")),
                source_country=str(entry.get("sourcecountry", "")),
            )
        )
    return parsed[:cap]


class GdeltConnector(BaseConnector):
    """GDELT DOC API client: 5 s pacing, 250-record cap, per-query windows."""

    name = "gdelt"
    default_cache_ttl = GDELT_CACHE_TTL_SECONDS
    min_request_interval = GDELT_MIN_INTERVAL_SECONDS
    request_jitter = 0.0  # the 5 s pacing IS the spread — no extra jitter

    def search_articles(
        self,
        query: str,
        *,
        timespan: str | None = None,
        max_records: int = GDELT_MAX_RECORDS,
    ) -> list[GdeltArticle]:
        """Search articles; ``timespan`` is a DOC-API window ("24h", "3d", "1w").

        Results are capped at 250 (the API's hard cap) — narrower windows, not
        pagination, are the way to reach deeper history.
        """
        cap = min(max_records, GDELT_MAX_RECORDS)
        params = {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "maxrecords": str(cap),
        }
        if timespan is not None:
            params["timespan"] = timespan
        text = self._get_text(GDELT_DOC_URL, params=params)
        return parse_articles(text, cap=cap)


__all__ = [
    "GDELT_DOC_URL",
    "GDELT_MAX_RECORDS",
    "GDELT_MIN_INTERVAL_SECONDS",
    "GdeltArticle",
    "GdeltConnector",
    "parse_articles",
]
