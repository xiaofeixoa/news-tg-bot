"""Breaking-news gate (design doc sections 11.3 + 16.3).

"Interrupt the reader" is a different question from "is this a good article", and
the two modes answer it with different evidence.

With an LLM the judgement lives in `importance_score`, so the configured bar of
90 works. Without one it cannot: measured on the live box over 7 days and 564
unfiltered rows, the best article scored 78 and the best first-hand blog post
76.9, because rule-mode importance/relevance are capped keyword-hit counts. At
bar 90 the feature had produced `sum(is_breaking) = 0` - designed for, deployed,
and dead.

Rule mode therefore gates on three things the numbers can actually support:
  1. an event word in the headline (announced / acquired / lawsuit / breach /
     "now available" / "$11.6 billion") - what separates news from good reading;
  2. a first-hand source (`min_source_quality`), because the same words appear
     in Show HN self-promotion and Reddit build posts;
  3. freshness, so a backfilled old post cannot wake anybody up.

Tuned against that same 7-day corpus: 21 matches (~3 per Beijing day, inside the
5/day cap), and each of them is a real event - "Introducing GPT-6 Sol and Luna",
"Anthropic to pay Akamai $11.6 billion over seven years in cloud deal", "Court
rules Trump can blacklist Anthropic...". A plain score cut at the same volume
selected version bumps and AWS how-to posts instead.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.config import AppConfig, as_float, get_config

_PATTERN_CACHE: dict[tuple[str, ...], re.Pattern[str]] = {}


def _combined(patterns: Any) -> re.Pattern[str] | None:
    """Compile once per pattern list; `re`'s own cache is not enough here."""
    items = tuple(str(p) for p in (patterns or []) if str(p).strip())
    if not items:
        return None
    cached = _PATTERN_CACHE.get(items)
    if cached is None:
        try:
            cached = re.compile("|".join(f"(?:{p})" for p in items), re.I)
        except re.error:
            # One bad regex in settings.yaml must not disable breaking news.
            valid = [p for p in items if _valid(p)]
            if not valid:
                return None
            cached = re.compile("|".join(f"(?:{p})" for p in valid), re.I)
        _PATTERN_CACHE[items] = cached
    return cached


def _valid(pattern: str) -> bool:
    try:
        re.compile(pattern)
    except re.error:
        return False
    return True


def event_trigger(title: str | None, config: AppConfig | None = None) -> str | None:
    """The event phrase in this headline, or None if it reports no event."""
    if not title:
        return None
    config = config or get_config()
    excluded = _combined(config.get("breaking.rule.exclude_titles", []))
    if excluded and excluded.search(title):
        return None
    rx = _combined(config.get("breaking.rule.event_patterns", []))
    if rx is None:
        return None
    hit = rx.search(title)
    return hit.group(0) if hit else None


def _blocked_source(source_name: str | None, config: AppConfig) -> bool:
    rx = _combined(config.get("breaking.rule.exclude_sources", []))
    return bool(rx and source_name and rx.search(source_name))


def gate(article: Any, *, config: AppConfig | None = None, ai_enabled: bool | None = None,
         user_threshold: float | None = None,
         at: datetime | None = None) -> tuple[bool, str]:
    """(is_breaking, why) for one scored article.

    `article` is anything with final_score/source_quality/published_at/title -
    the ORM row during processing and the same row when the sender re-checks it.
    """
    config = config or get_config()
    score = as_float(getattr(article, "final_score", None), 0.0)
    if ai_enabled is None:
        ai_enabled = config.ai_enabled
    if ai_enabled:
        # The model's importance score is the whole judgement here.
        bar = as_float(config.get("breaking.threshold", config.settings.breaking_news_threshold), 90.0)
        if user_threshold:
            bar = float(user_threshold)
        return (score >= bar, f"score {score:.0f} {'≥' if score >= bar else '<'} AI-mode bar {bar:.0f}")

    source_name = getattr(article, "source_name", None)
    if _blocked_source(source_name, config):
        return False, f"community source {source_name!r} is not a publisher"
    quality = as_float(getattr(article, "source_quality", None), 0.0)
    min_quality = as_float(config.get("breaking.rule.min_source_quality"), 80.0)
    if quality < min_quality:
        return False, f"source quality {quality:.0f} below {min_quality:.0f}"
    min_score = as_float(config.get("breaking.rule.min_score"), 45.0)
    if score < min_score:
        return False, f"score {score:.0f} below {min_score:.0f}"
    published = getattr(article, "published_at", None)
    max_age = as_float(config.get("breaking.rule.max_age_hours"), 24.0)
    if isinstance(published, datetime) and max_age > 0:
        age_hours = ((at or datetime.utcnow()) - published).total_seconds() / 3600.0
        if age_hours > max_age:
            return False, f"published {age_hours:.0f}h ago, older than {max_age:.0f}h"
    trigger = event_trigger(getattr(article, "title", None), config)
    if not trigger:
        return False, "no event in the headline"
    return True, f"event “{trigger}” from a first-hand source (score {score:.0f})"


def emoji_bars(config: AppConfig | None = None, *, ai_enabled: bool | None = None) -> tuple[float, float, float]:
    """(🔥, ⭐, 🔹) cut-offs for briefing rows - the whole ladder, not just the top.

    🔥 has to stay reachable. Under the AI bar of 90 no rule-mode article can
    show it (measured top score: 78), and hard-coding the lower steps at 75/60
    made ⭐ rarer than 🔥, which reads as a broken legend instead of a ranking.
    """
    config = config or get_config()
    if ai_enabled is None:
        ai_enabled = config.ai_enabled
    if ai_enabled:
        return (as_float(config.get("breaking.threshold",
                                    config.settings.breaking_news_threshold), 90.0), 75.0, 60.0)
    return (as_float(config.get("breaking.rule.hot_score"), 72.0),
            as_float(config.get("breaking.rule.star_score"), 62.0),
            as_float(config.get("breaking.rule.dot_score"), 52.0))


def describe(config: AppConfig | None = None, *, ai_enabled: bool | None = None) -> str:
    """Chinese, honest account of what currently counts as 突发 (for /设置)."""
    config = config or get_config()
    if not bool(config.get("breaking.enabled", True)) or not config.settings.breaking_news_enabled:
        return "已在配置里关闭"
    if ai_enabled is None:
        ai_enabled = config.ai_enabled
    if ai_enabled:
        bar = as_float(config.get("breaking.threshold", config.settings.breaking_news_threshold), 90.0)
        return f"评分 ≥ {bar:.0f}"
    return "标题里有大事件 + 一手来源 + 24 小时内"
