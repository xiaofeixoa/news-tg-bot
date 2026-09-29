"""Chinese summary generation (design doc section 13).

The model returns structured JSON; when it is unavailable the article still
gets a usable one-line digest built from the feed text, so news never vanishes
because a provider had a bad afternoon (section 25).
"""

from __future__ import annotations

import re
from typing import Any

from app.config import AppConfig, get_config
from app.logging_setup import get_logger
from app.processing.normalize import shorten, strip_feed_boilerplate, unescape_entities

log = get_logger("app")

SENTENCE_RE = re.compile(r"(?<=[.!?\u3002\uff01\uff1f])\s+")


# Collector-generated filler: never a "summary" of the story itself.
SYNTHETIC_PREFIXES = (
    "discussed on", "shared link:", "community discussion on", "release ",
    "language:", "topics:", "created:", "posted by", "[media]",
)


def _sentences(body: str) -> list[str]:
    out: list[str] = []
    for sentence in SENTENCE_RE.split(body):
        text = sentence.strip()
        lowered = text.lower()
        if len(text) < 30 or lowered.startswith(SYNTHETIC_PREFIXES):
            continue
        out.append(text)
    return out


def compose_why_it_matters(article: Any, event: Any = None, *, config: AppConfig | None = None) -> str:
    """One Chinese line on why this row earned a place, built only from facts.

    Rule mode returned `why_it_matters: ""` for every article, so the card's
    为什么值得关注 heading had never once appeared on a key-less box (measured: 0 of
    423 visible rows in a 14-day window). Rather than translate a template about
    nothing, this writes a line only when there is a real anchor - the story is
    being reported by several sources, or a first-hand publisher made an
    announcement - and community heat may then strengthen that line but never
    stands on its own. Without an anchor the field stays empty: an honest blank
    beats padding, and "社区热度 407" as a justification for a Show HN self-promo
    post was exactly the filler this rule now refuses.
    """
    config = config or get_config()
    from app.processing import breaking

    trigger = breaking.event_trigger(getattr(article, "title", None), config)
    clauses: list[str] = []
    # 数的是**别家媒体**，不是转载行数：一个事件里 7 行都来自同一家时，"另有 6 家来源
    # 报道"是假的（线上量到 #659 就是这样：7 行、2 个来源）。这与 v1.60 卡片那次同族。
    names = [str(name) for name in (getattr(event, "source_names", None) or []) if name]
    outlets = list(dict.fromkeys(names))
    members = int(getattr(event, "member_count", 0) or 0) if event is not None else 0
    others = [name for name in outlets if name != getattr(article, "source_name", None)][:3]
    if members > 1 and others:
        clauses.append(f"同一事件另有 {len(others)} 家来源报道（{'、'.join(others)}）")
    if not trigger and not clauses:
        return ""
    heat = float(getattr(article, "community_heat", 0) or 0)
    heat_bar = float(config.get("breaking.rule.min_community_heat", 250) or 250) / 2
    if heat >= max(50.0, heat_bar):
        clauses.append(f"社区热度 {heat:.0f}（{getattr(article, 'source_name', '') or '社区'}）")
    quality = float(getattr(article, "source_quality", 0) or 0)
    if trigger and quality >= float(config.get("breaking.rule.min_source_quality", 80)):
        clauses.append(f"由 {getattr(article, 'source_name', '') or '一手来源'} 自己发布的事件性消息")
    return "；".join(clauses)[:280]


def fallback_summary(article: dict[str, Any], *, language: str = "en") -> dict[str, Any]:
    # A summary is display text and never a URL, so entities are decoded here
    # even though `clean_text` deliberately leaves them alone: feeds ship
    # double-escaped bodies and `Now&#160;` in a briefing line reads as junk.
    body = strip_feed_boilerplate(unescape_entities(re.sub(
        r"\s+", " ", str(article.get("content") or article.get("title") or ""))))
    title = unescape_entities(str(article.get("title") or "")).strip()
    sentences = _sentences(body)
    # A line that is basically the headline adds nothing; find real prose instead.
    sentences = [s for s in sentences if s.lower()[:70] != title.lower()[:70]] or sentences
    if not sentences:
        return {
            "summary": shorten(title, 120),
            "key_points": [],
            "why_it_matters": "",
            "tags": [],
            "importance_score": 0,
            "method": "fallback",
        }
    first = sentences[0]
    # `[:160]` on a single long sentence (release notes are usually one) used to
    # store a summary that stops inside a quoted word; shorten() lands on a
    # clause boundary and marks the cut.
    if language == "zh":
        summary, key_points = shorten(first, 60), [shorten(s, 120) for s in sentences[1:3]]
    else:
        summary = shorten(title, 120) if len(first) < 40 else shorten(first, 160)
        key_points = [shorten(s, 160) for s in sentences[1:3]]
    return {
        "summary": summary,
        "key_points": key_points,
        "why_it_matters": "",
        "tags": [],
        "importance_score": 0,
        "method": "fallback",
    }


async def summarize(
    article: dict[str, Any],
    *,
    config: AppConfig | None = None,
    llm: Any = None,
) -> dict[str, Any]:
    config = config or get_config()
    content = str(article.get("content") or "").strip()
    if not content or len(content) < 40:
        # Title-only items (arXiv listings, thin feeds) gain nothing from a model call.
        return fallback_summary(article, language=article.get("language") or "en")
    if llm is None or not getattr(llm, "enabled", False):
        result = fallback_summary(article, language=article.get("language") or "en")
        result["method"] = "no-llm"
        return result
    try:
        data = await llm.summarize({**article, "published_at": _iso(article.get("published_at"))})
    except Exception as exc:
        log.warning("summary failed for %r: %s", article.get("title"), exc)
        result = fallback_summary(article, language=article.get("language") or "en")
        result["method"] = "fallback"
        result["error"] = str(exc)[:200]
        return result
    summary = str(data.get("summary") or "").strip()
    if not summary:
        result = fallback_summary(article, language=article.get("language") or "en")
        result["method"] = "fallback"
        return result
    return {
        "summary": summary[:600],
        "key_points": list(data.get("key_points") or []),
        "why_it_matters": str(data.get("why_it_matters") or "").strip()[:800],
        "tags": list(data.get("tags") or []),
        "importance_score": float(data.get("importance_score") or 0),
        "method": "ai",
    }


def _iso(value: Any) -> str:
    return value.isoformat(timespec="minutes") if hasattr(value, "isoformat") else str(value or "")
