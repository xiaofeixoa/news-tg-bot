"""arXiv collector (design doc section 4.5).

Fetches the Atom API for the configured categories. Papers are never pushed
 wholesale: the keyword gate here plus the AI relevance score downstream decide
what survives.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import feedparser

from app.collectors.base import BaseCollector, CollectorError, describe_error, register
from app.logging_setup import get_logger
from app.processing.normalize import clean_text

log = get_logger("collector")

API = "https://export.arxiv.org/api/query"


@register
class ArxivCollector(BaseCollector):
    type = "arxiv"

    async def fetch(self) -> list[dict[str, Any]]:
        cats = [str(c) for c in (self.source.get("categories") or ["cs.AI", "cs.CL", "cs.LG", "cs.CV"])]
        query = " OR ".join(f"cat:{c}" for c in cats)
        if extra := clean_text(self.source.get("query")):
            query = f"({query}) AND {extra}"
        params = {
            "search_query": query,
            "start": 0,
            "max_results": min(100, self.limit()),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        try:
            response = await self.get(API, params=params, attempts=2)
        except CollectorError:
            raise
        except Exception as exc:
            raise CollectorError(f"arXiv query failed - {describe_error(exc, API)}") from exc
        parsed = await asyncio.to_thread(feedparser.parse, response.content)
        items: list[dict[str, Any]] = []
        require_keyword = bool(self.source.get("require_keyword", True))
        for entry in parsed.entries:
            title = clean_text(entry.get("title"))
            abstract = clean_text(entry.get("summary"))
            link = entry.get("link") or _first_link(entry)
            if not title or not link:
                continue
            if require_keyword and not self.wants_keyword(f"{title}. {abstract}"):
                continue
            authors = [clean_text((a or {}).get("name")) for a in (entry.get("authors") or []) if isinstance(a, dict)]
            items.append(
                {
                    "title": title[:500],
                    "url": link,
                    "author": ", ".join(a for a in authors if a)[:250] or None,
                    "content": abstract[:12000],
                    "published_at": _parse(entry.get("published")),
                    "meta": {
                        "arxiv_id": _extract_id(entry.get("id")),
                        "primary_category": ((entry.get("arxiv_primary_category") or {}).get("term")
                                             if isinstance(entry.get("arxiv_primary_category"), dict) else None),
                        "categories": [
                            t.get("term") for t in (entry.get("tags") or []) if isinstance(t, dict)
                        ][:8],
                        "authors": authors[:12],
                        "abs_url": _first_link(entry) or link,
                        "pdf_url": _pdf_link(entry),
                        "kind": "paper",
                    },
                }
            )
        return items[: self.limit()]


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed


def _first_link(entry: dict[str, Any]) -> str | None:
    for link in entry.get("links") or []:
        if isinstance(link, dict) and link.get("type") != "application/pdf" and "abstract" in str(link.get("href", "")):
            return link["href"]
    for link in entry.get("links") or []:
        if isinstance(link, dict) and link.get("rel") == "alternate":
            return link["href"]
    return None


def _pdf_link(entry: dict[str, Any]) -> str | None:
    for link in entry.get("links") or []:
        if isinstance(link, dict) and (link.get("type") == "application/pdf" or str(link.get("title", "")).lower() == "pdf"):
            return link["href"]
    return None


def _extract_id(value: Any) -> str | None:
    text = str(value or "")
    return text.rsplit("/", 1)[-1] if text else None
