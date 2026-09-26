"""Reddit collector (design doc section 4.4).

Deliberately isolated: Reddit blocks datacentre IPs and changes shape often, so
every failure path returns an empty list or raises CollectorError - the run
continues either way. Disabled by default in sources.yaml.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.collectors.base import BaseCollector, CollectorError, register
from app.logging_setup import get_logger
from app.processing.normalize import clean_text, strip_html

log = get_logger("collector")

BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


@register
class RedditCollector(BaseCollector):
    type = "reddit"

    @property
    def subreddit(self) -> str:
        return clean_text(self.source.get("subreddit") or self.source.get("sub") or "").strip("/r").strip("/")

    async def fetch(self) -> list[dict[str, Any]]:
        sub = self.subreddit
        if not sub:
            raise CollectorError("reddit collector needs `subreddit:`")
        listings = [str(x) for x in (self.source.get("listings") or ["hot"])]
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for listing in listings:
            url = f"https://www.reddit.com/r/{sub}/{listing}.json"
            try:
                response = await self.get(
                    url,
                    params={"limit": min(50, self.limit()), "raw_json": 1, "t": "all"},
                    headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
                    attempts=2,
                )
            except Exception as exc:
                # 403/429 is Reddit being Reddit: log and move on.
                raise CollectorError(f"r/{sub}/{listing} unavailable: {exc}") from exc
            for child in (response.json().get("data") or {}).get("children", []):
                post = child.get("data") or {}
                if not _accept(post, self.source, sub):
                    continue
                key = str(post.get("id") or post.get("permalink"))
                if key in seen:
                    continue
                seen.add(key)
                items.append(_to_item(post, sub))
        return items[: self.limit()]


def _accept(post: dict[str, Any], source: dict[str, Any], subreddit: str) -> bool:
    if post.get("stickied") or post.get("promoted") or post.get("is_created_from_ads_ui"):
        return False
    if str(post.get("subreddit") or "").lower() != subreddit.lower():
        return False
    if int(post.get("ups") or 0) < int(source.get("min_upvotes", 50)):
        return False
    if int(post.get("num_comments") or 0) < int(source.get("min_comments", 0)):
        return False
    ignored = {"automoderator"} | {str(a).strip().lower() for a in (source.get("ignore_authors") or [])}
    if str(post.get("author") or "").lower() in ignored:
        return False
    return bool(clean_text(post.get("title")))


def _to_item(post: dict[str, Any], subreddit: str) -> dict[str, Any]:
    title = clean_text(post.get("title"))
    body = strip_html(clean_text(post.get("selftext"))) or ""
    permalink = f"https://www.reddit.com{post.get('permalink', '')}"
    external = clean_text(post.get("url"))
    is_self_post = bool(post.get("is_self")) or external.startswith("https://www.reddit.com")
    score = int(post.get("score") or post.get("ups") or 0)
    comments = int(post.get("num_comments") or 0)
    created = post.get("created_utc")
    content = body or f"Community discussion on r/{subreddit}: {title}"
    if not is_self_post:
        content = f"Shared link: {external}\n\n{content}"
    return {
        "title": title[:500],
        # Self posts are keyed by their permalink; link posts by the article URL
        # so an article also covered by a feed dedups against it.
        "url": permalink if is_self_post else external,
        "author": clean_text(post.get("author")),
        "content": content[:12000],
        "published_at": datetime.fromtimestamp(float(created), tz=timezone.utc) if created else None,
        "community_heat": score,
        "meta": {
            "subreddit": f"r/{subreddit}",
            "upvotes": score,
            "comments": comments,
            "discussion": permalink,
            "external_url": None if is_self_post else external,
            "flair": clean_text(post.get("link_flair_text")),
        },
    }
