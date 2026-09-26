"""Hacker News collector (design doc section 4.2).

Uses the public Algolia search API: no key, stable, and it exposes the point
count that feeds the community_heat score.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.collectors.base import BaseCollector, CollectorError, describe_error, register
from app.logging_setup import get_logger
from app.processing.normalize import clean_text

log = get_logger("collector")

ALGOLIA = "https://hn.algolia.com/api/v1"


@register
class HackerNewsCollector(BaseCollector):
    type = "hackernews"

    @property
    def min_points(self) -> int:
        return int(self.source.get("min_points", 60))

    async def fetch(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        queries = [str(q) for q in (self.source.get("queries") or [])] or ["AI"]
        if self.source.get("front_page", True):
            items.extend(await self._search({"tags": "front_page"}, seen))
        for query in queries:
            params = {
                "query": query,
                "tags": "story",
                "numericFilters": f"points>={self.min_points},created_at_i>{int(self.since(48).timestamp())}",
                "hitsPerPage": min(30, self.limit()),
            }
            items.extend(await self._search(params, seen))
        return items

    async def _search(self, params: dict[str, Any], seen: set[str]) -> list[dict[str, Any]]:
        try:
            response = await self.get(f"{ALGOLIA}/search", params=params)
        except Exception as exc:
            raise CollectorError(f"Hacker News search failed - {describe_error(exc)}") from exc
        out: list[dict[str, Any]] = []
        for hit in response.json().get("hits", []):
            story_id = str(hit.get("objectID") or "")
            url = clean_text(hit.get("url")) or f"https://news.ycombinator.com/item?id={story_id}"
            if not story_id or story_id in seen:
                continue
            seen.add(story_id)
            title = clean_text(hit.get("title")) or "(untitled)"
            if not self._relevant(title, hit):
                continue
            points = int(hit.get("points") or 0)
            comments = int(hit.get("num_comments") or 0)
            created = hit.get("created_at")
            out.append(
                {
                    "title": title,
                    "url": url,
                    "author": clean_text(hit.get("author")),
                    # Link-only stories have no body of their own: the headline is
                    # the content, and the discussion stats live in meta (heat).
                    "content": clean_text(hit.get("story_text")) or title,
                    "published_at": _parse_iso(created),
                    "community_heat": points,
                    "meta": {
                        "points": points,
                        "comments": comments,
                        "hn_id": story_id,
                        "hn_discussion": f"https://news.ycombinator.com/item?id={story_id}",
                        "external_url": clean_text(hit.get("url")),
                    },
                }
            )
        return out

    def _relevant(self, title: str, hit: dict[str, Any]) -> bool:
        if self.source.get("require_keyword", True) is False:
            return True
        return self.wants_keyword(f"{title} {' '.join((hit.get('_tags') or [])[:5])}")


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed
