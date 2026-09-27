"""Main-content extraction for feeds that only ship a stub (design doc §6).

The tech stack names BeautifulSoup4 for 正文提取, but until now it was only used
to clean feed descriptions. The measured cost: Google DeepMind and Hugging Face
deliver 36-288 characters per item, so the keyword-derived importance of the
rule pipeline could not clear the briefing bar - 100 first-party announcements
in seven days produced **zero** digest-eligible rows, while 3.7 KB Reddit
threads cleared it easily. Fetching the page fixes the score and the summary
with the same text.

Everything here is optional work: no budget, no URL, a cold host or a failed
fetch leaves the stored stub untouched.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import AppConfig, as_int, get_config
from app.logging_setup import get_logger
from app.processing.normalize import clean_text

log = get_logger("pipeline")

DROP_TAGS = ("script", "style", "noscript", "svg", "form", "nav", "aside",
             "footer", "header", "figure", "iframe", "button")
# Comment bullets and legal boilerplate read as body text and pollute summaries.
JUNK_RE = re.compile(r"(subscribe|sign up|log in|cookie|all rights reserved|"
                     r"share this|related (posts|articles)|terms of service)", re.I)
MIN_PARAGRAPH = 60


class Budget:
    """Fetches still allowed this round, so a slow news cycle cannot stall."""

    def __init__(self, allowed: int) -> None:
        self.left = max(0, allowed)
        self.used = 0

    def spend(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        self.used += 1
        return True


def extract(html_body: str, *, max_chars: int = 12000) -> str:
    """The cheapest article-text heuristic that actually works."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_body or "", "html.parser")
    for tag in soup(list(DROP_TAGS)):
        tag.decompose()
    node = soup.find("article") or soup.find("main") or soup.body or soup
    parts = [clean_text(p.get_text(" ", strip=True))
             for p in node.find_all(["p", "li", "h2", "h3"])]
    text = "\n".join(p for p in parts if len(p) >= MIN_PARAGRAPH and not JUNK_RE.search(p))
    return text[:max_chars].strip()


async def fetch(url: str, *, max_chars: int = 12000, timeout: float = 6.0) -> str:
    """Get one page's body text. Failure is reported as an empty string.

    A host that refuses us is parked through the collector cooldown, so a 403
    costs one request per hour instead of one per article.
    """
    from app.collectors.base import cool_down, cooling, get_client

    if not str(url).startswith("http") or cooling(url):
        return ""
    try:
        client = await get_client()
        response = await client.get(url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - any transport failure means "use the stub"
        cool_down(url, 3600)
        log.debug("enrich fetch failed for %s: %s", url, exc)
        return ""
    status = int(getattr(response, "status_code", 200) or 200)
    if status >= 400:
        cool_down(url, 6 * 3600 if status in (401, 403, 429) else 1800)
        log.debug("enrich refused %s -> HTTP %s", url, status)
        return ""
    return extract(getattr(response, "text", "") or "", max_chars=max_chars)


def drop_stale_translation(article: Any) -> None:
    """A translation of the old text is not a translation of the new text.

    Measured on the deployed box: an enriched Hugging Face row kept the Chinese
    summary that had been translated from its 46-character teaser -
    "Foundry 托管计算上的拥抱脸部模型" - while the briefing prefers the Chinese line,
    so the reader saw the stale guess instead of the improved English text.
    """
    if getattr(article, "summary_zh", None) or getattr(article, "translated_by", None):
        article.summary_zh = None
        article.translated_by = None


def enabled(config: AppConfig) -> bool:
    return bool(config.get("enrich.enabled", True))


def min_chars(config: AppConfig) -> int:
    return as_int(config.get("enrich.min_chars", 600), 600)


async def maybe_enrich(data: dict[str, Any], article: Any, config: AppConfig,
                       *, budget: Budget) -> bool:
    """Replace a stub with the real article text when the page is reachable.

    True means `data`/`article` now carry more text, so scoring, tagging and
    the summary all read the article instead of a one-line teaser. Enrichment
    only ever *adds* signal: the caller re-derives keyword hits from the new
    text but never un-filters an item the stub already passed.
    """
    content = str(data.get("content") or "")
    floor = min_chars(config)
    if not enabled(config) or len(content) >= floor or not budget.spend():
        return False
    body = await fetch(str(data.get("url") or ""),
                       max_chars=as_int(config.get("enrich.max_chars", 12000), 12000))
    if len(body) > len(content):
        data["content"] = body
        article.content = body
        drop_stale_translation(article)
        data["meta"] = {**(data.get("meta") or {}), "enriched": True}
        article.meta = data["meta"]
        return True
    # Record the failed attempt too: `requeue_stubs` uses it to stop knocking on
    # a blocked host at every boot.
    data["meta"] = {**(data.get("meta") or {}), "enriched": False}
    article.meta = data["meta"]
    return False


def requeue_stubs(session: Session, *, config: AppConfig | None = None,
                  within_hours: int = 36, limit: int = 20) -> int:
    """Queue recent stub rows for another pass so old news gets the new text too.

    Enrichment only happens while an article is being processed, so the 328
    stub rows already in the database (51 of them OpenAI) would keep their
    unreachable scores until they aged out of the digest window. Re-queueing
    them through the normal pipeline keeps one code path, and `attach_tags` is
    idempotent so a second pass is safe.

    Ordered by score descending: the point is to lift items that nearly cleared
    the briefing bar, and a 20-pointer stays invisible whatever we fetch.
    """
    from app.database.models import Article

    config = config or get_config()
    floor = min_chars(config)
    since = datetime.utcnow() - timedelta(hours=within_hours)
    candidates = list(session.scalars(
        select(Article)
        .where(Article.is_processed.is_(True), Article.is_archived.is_(False),
               Article.published_at >= since, func.length(Article.content) < floor)
        .order_by(Article.final_score.desc()).limit(max(limit, 1) * 3)))
    queued = 0
    for row in candidates:
        if "enriched" in (row.meta or {}):
            continue            # already tried; the host refused or had no text
        row.is_processed = False
        row.process_attempts = 0
        # A row that is waiting again must not keep claiming a finish time, or
        # `processed_at IS NOT NULL` stops meaning "this row is settled" - the one
        # thing the backlog and 突发 audits can otherwise rely on.
        row.processed_at = None
        queued += 1
        if queued >= limit:
            break
    return queued


def reset_stale_translations(session: Session, *, config: AppConfig | None = None,
                             within_hours: int = 48, limit: int = 200) -> int:
    """One-time repair for rows enriched before the translation was invalidated.

    Eight rows on the deployed box already had full text plus a Chinese summary
    translated from the teaser they had when they were first processed. The
    `zh_reset` marker keeps this from re-clearing a line the translation pass
    produced *after* the fix, which would be an endless free-MT churn loop.
    """
    from app.database.models import Article

    config = config or get_config()
    floor = min_chars(config)
    since = datetime.utcnow() - timedelta(hours=within_hours)
    candidates = list(session.scalars(
        select(Article)
        .where(Article.published_at >= since, Article.summary_zh.is_not(None),
               Article.is_archived.is_(False), func.length(Article.content) >= floor)
        .order_by(Article.id.desc()).limit(limit)))
    fixed = 0
    for row in candidates:
        meta = row.meta or {}
        if meta.get("enriched") is not True or meta.get("zh_reset"):
            continue
        drop_stale_translation(row)
        row.meta = {**meta, "zh_reset": True}
        fixed += 1
    return fixed
