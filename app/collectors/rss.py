"""RSS / Atom collector - the MVP workhorse (design doc section 4.1).

Works for any feed URL, including RSSHub bridges: adding a feed is a
config/sources.yaml edit, never a code change.
"""

from __future__ import annotations

import asyncio
import calendar
from datetime import datetime, timezone
from typing import Any

import feedparser
from bs4 import BeautifulSoup  # noqa: F401  (kept for feed body cleaning via strip_html)

from app.collectors.base import BaseCollector, CollectorError, register
from app.logging_setup import get_logger
from app.processing.normalize import clean_text, strip_html

log = get_logger("collector")

CONTENT_FIELDS = ("content", "summary", "description", "subtitle")


def _entry_datetime(entry: dict[str, Any]) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed", "lastblddate_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
            except (ValueError, OverflowError, TypeError):
                continue
    return None


def _entry_content(entry: dict[str, Any]) -> tuple[str, str | None]:
    """Return (html_text, link_to_full_article)."""
    parts: list[str] = []
    link = None
    for field in CONTENT_FIELDS:
        value = entry.get(field)
        if isinstance(value, list) and value:
            parts.extend(str(item.get("value", "")) for item in value if isinstance(item, dict))
        elif value:
            parts.append(str(value))
    enclosure = entry.get("enclosures") or []
    if enclosure and isinstance(enclosure[0], dict):
        parts.append(f"[media] {enclosure[0].get('href', '')}")
    html = "\n\n".join(p for p in parts if p)
    return html, link


@register
class RSSCollector(BaseCollector):
    type = "rss"

    async def fetch(self) -> list[dict[str, Any]]:
        url = self.source.get("rss_url") or self.source.get("url")
        if not url:
            raise CollectorError(f"source {self.name!r} has no url")
        try:
            response = await self.get(url, attempts=int(self.source.get("attempts", 3)),
                                      headers={"Accept": "application/rss+xml, application/atom+xml, "
                                                         "application/xml, text/xml, */*"})
        except CollectorError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported as a source failure
            # httpx leaves some transport errors with an empty message, so the
            # class name and URL have to be in the text or the log is useless.
            raise CollectorError(f"{url}: {type(exc).__name__}: {exc}") from exc

        raw = response.content
        parsed = await asyncio.to_thread(feedparser.parse, raw)
        if parsed.bozo and not parsed.entries:
            raise CollectorError(f"unparsable feed ({getattr(parsed, 'bozo_exception', 'unknown')})")

        items: list[dict[str, Any]] = []
        for entry in parsed.entries[: self.limit()]:
            html, _ = _entry_content(entry)
            text = strip_html(html)
            if not text:
                text = clean_text(entry.get("title", ""))
            items.append(
                {
                    "title": entry.get("title") or "(untitled)",
                    "url": entry.get("link") or (entry.get("links") or [{}])[0].get("href"),
                    "author": _author(entry),
                    "content": text[:12000],
                    "published_at": _entry_datetime(entry),
                    "meta": {
                        "feed_title": clean_text(getattr(parsed.feed, "title", "") or "") or self.name,
                        "feed_url": url,
                        "categories": [
                            str(t.get("term") if isinstance(t, dict) else t)
                            for t in (entry.get("tags") or [])
                        ][:8],
                        "guid": entry.get("id") or entry.get("guid"),
                    },
                }
            )
        return items


def _author(entry: dict[str, Any]) -> str | None:
    author = entry.get("author")
    if author:
        return clean_text(str(author))[:250]
    author_detail = entry.get("author_detail") or {}
    if isinstance(author_detail, dict) and author_detail.get("name"):
        return clean_text(str(author_detail["name"]))[:250]
    return None


def parse_feed_bytes(raw: bytes) -> Any:  # pragma: no cover - test helper
    return feedparser.parse(raw)
