"""YouTube collector (design doc section 4.6).

Phase 2 approach on purpose: channel RSS needs no API key, no quota and no
review process. Transcripts / summarising video content are out of MVP scope.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any

import feedparser

from app.collectors.base import BaseCollector, CollectorError, register
from app.logging_setup import get_logger
from app.processing.normalize import clean_text

log = get_logger("collector")

FEED = "https://www.youtube.com/feeds/videos.xml"
CHANNEL_ID_RE = re.compile(r"channel/([A-Za-z0-9_-]{6,})")


@register
class YouTubeCollector(BaseCollector):
    type = "youtube"

    async def fetch(self) -> list[dict[str, Any]]:
        channels = _channel_ids(self.source)
        if not channels:
            raise CollectorError("youtube collector needs `channels:` with ids or handles")
        items: list[dict[str, Any]] = []
        for channel in channels:
            try:
                response = await self.get(FEED, params={"channel_id": channel["id"]}, attempts=2)
            except Exception as exc:
                log.debug("youtube channel %s unavailable: %s", channel.get("name"), exc)
                continue
            parsed = await asyncio.to_thread(feedparser.parse, response.content)
            for entry in parsed.entries[: min(10, self.limit())]:
                title = clean_text(entry.get("title"))
                link = entry.get("link")
                media = entry.get("media_customer") or entry.get("media_group") or {}
                description = clean_text(entry.get("media_description") or entry.get("summary"))
                if not title or not link:
                    continue
                if self.source.get("require_keyword", True) and not self.wants_keyword(f"{title}. {description}"):
                    continue
                items.append(
                    {
                        "title": f"[video] {title}"[:500],
                        "url": link,
                        "author": channel.get("name") or clean_text(entry.get("author")),
                        "content": (description or title)[:12000],
                        "published_at": _parse(entry.get("published")),
                        "meta": {
                            "channel": channel.get("name"),
                            "channel_id": channel["id"],
                            "video_id": link.rsplit("v=", 1)[-1],
                            "thumbnail": _thumbnail(entry),
                            "kind": "video",
                        },
                    }
                )
        return items[: self.limit()]


def _channel_ids(source: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in source.get("channels") or []:
        if isinstance(entry, dict):
            cid = clean_text(entry.get("id"))
            handle = clean_text(entry.get("handle") or entry.get("url"))
            if not cid and handle:
                found = CHANNEL_ID_RE.search(handle)
                cid = found.group(1) if found else ""
            if cid:
                out.append({"id": cid, "name": clean_text(entry.get("name")) or handle or cid})
        elif isinstance(entry, str) and entry.strip():
            out.append({"id": entry.strip(), "name": entry.strip()})
    return out


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed


def _thumbnail(entry: dict[str, Any]) -> str | None:
    for key in ("media_thumbnail", "media_customer_thumbnail"):
        value = entry.get(key)
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value[0].get("url")
        if isinstance(value, dict):
            return value.get("url")
    return None
