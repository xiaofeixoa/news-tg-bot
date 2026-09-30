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

...with one measured exception: `min_community_heat` lets a row through the
source-quality and score bars, never through the event word or the freshness limit. Over 7 days on the live box four rows
carried an event word, were collected within 1.4h of publishing and scored 70-78 -
"Revealing the details of how OpenAI agents hacked Hugging Face" (645 upvotes),
"U.S. appeals court upholds designation of Anthropic as supply-chain risk" (480) -
and none of them alerted, because they arrived through Hacker News (quality 65).
Same week, 418 of 431 rows have heat 0 and only 13 reach 100, so the bar cannot flood.

Heat is also the *lagging* signal here, and that cost the feature its own examples. This
gate runs once, minutes after a row is collected, while `community_heat` keeps being
refreshed upward every time a collector sees the same URL again (`repository._merge_duplicate`).
Measured 2026-09-25..29 on the live box: #581 was scored at 26 upvotes and holds 741 now,
#509 90 -> 495, #1182 47 -> 593 - three stories that crossed the bar after the only minute
in which anybody asked. A rejection that is *only* about heat inside the freshness window
therefore comes back as 等待全站热度 so the retry queue re-asks it, and ages out with a
recorded reason when the window closes.

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

from app.config import AppConfig, as_float, get_config, quiet_hours_text

_PATTERN_CACHE: dict[tuple[str, ...], re.Pattern[str]] = {}
DEFERRAL_KEY = "breaking_defer"
# Fallback for the freshness bar, and the window the retry queue reads itself by.
MAX_AGE_DEFAULT = 24.0
# Marks a rejection that a *later* round can overturn because the signal behind it
# keeps arriving. `digest.deferral_worthwhile` matches this text.
HEAT_WATCH = "等待全站热度"


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


def _age_hours(article: Any, *, at: datetime | None = None) -> float | None:
    """Hours since publication, or None when the row carries no usable timestamp.

    None also means "no freshness window to wait inside", which is why the retry
    queue never books a row for heat without a timestamp: it would be re-asked
    forever, since the gate can never age it out.
    """
    published = getattr(article, "published_at", None)
    if not isinstance(published, datetime):
        return None
    return ((at or datetime.utcnow()) - published).total_seconds() / 3600.0


def _may_wait(heat_bar: float, age: float | None, max_age: float) -> bool:
    return (heat_bar > 0 and age is not None and max_age > 0 and (max_age - age) > 0)


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
    # Event first, then who reported it: "no event in the headline" is the more
    # useful reason than "source quality 50 below 80" when both are true, and the
    # funnel in the logs is how this gate gets audited.
    trigger = event_trigger(getattr(article, "title", None), config)
    if not trigger:
        return False, "no event in the headline"
    quality = as_float(getattr(article, "source_quality", None), 0.0)
    min_quality = as_float(config.get("breaking.rule.min_source_quality"), 80.0)
    min_score = as_float(config.get("breaking.rule.min_score"), 45.0)
    # Heat is the one signal that says "everybody is reading this", and it arrives
    # through community surfaces: the OpenAI-agent-hacked-a-government story scored
    # 76 with 645 upvotes and was rejected for `source_quality 65`. It bypasses the
    # quality and score bars only - an event word, freshness and the explicit source
    # blocklist still have to hold, so a Show HN post cannot buy its way in with karma.
    heat = as_float(getattr(article, "community_heat", None), 0.0)
    heat_bar = as_float(config.get("breaking.rule.min_community_heat"), 0.0)
    rescued = heat_bar > 0 and heat >= heat_bar
    max_age = as_float(config.get("breaking.rule.max_age_hours"), MAX_AGE_DEFAULT)
    age = _age_hours(article, at=at)
    # A row that fails the quality/score bars *while it is still inside the freshness
    # window* has not failed for good: the number that would rescue it keeps growing
    # after this minute. Say what is being waited for, and keep the shortfall in the
    # same line so the audit funnel does not lose which bar turned it away.
    wait_note = (f"{HEAT_WATCH}：此刻热度 {heat:.0f} / 门槛 {heat_bar:.0f}，"
                 f"{max_age - age:.0f} 小时内还会再问一次") if _may_wait(heat_bar, age, max_age) else ""
    if quality < min_quality and not rescued:
        return False, (f"{wait_note}（来源质量 {quality:.0f} 低于 {min_quality:.0f}）" if wait_note
                       else f"source quality {quality:.0f} below {min_quality:.0f}")
    if score < min_score and not rescued:
        return False, (f"{wait_note}（评分 {score:.0f} 低于 {min_score:.0f}）" if wait_note
                       else f"score {score:.0f} below {min_score:.0f}")
    if age is not None and max_age > 0 and age > max_age:
        return False, f"published {age:.0f}h ago, older than {max_age:.0f}h"
    if rescued:
        return True, (f"event “{trigger}” 全站热度 {heat:.0f}（社区来源破例，"
                      f"来源质量 {quality:.0f}、评分 {score:.0f}）")
    return True, f"event “{trigger}” from a first-hand source (score {score:.0f})"


def emoji_bars(config: AppConfig | None = None, *, ai_enabled: bool | None = None) -> tuple[float, float, float]:
    """(🔥, ⭐, 🔹) cut-offs for briefing rows - the whole ladder, not just the top.

    The three fallbacks below must match `breaking.rule.*_score` in settings.yaml,
    which carries the measurement they were set from (2026-09-27: p90/p60/p25 of a
    110-row live window = 62/54/49). They used to sit at 72/62/52 because the
    highest score anyone could measure was 78 - but that 78 was the tier cap being
    slammed through by raw heat counts (v1.44), i.e. 46 rows were tied at it and
    every one of them was a trending repo. Calibrating "how hot is this news"
    against a scoring artifact made 🔥 mean "GitHub" and left 47% of a day's
    eligible pool rendering as ▫️ "not important".
    """
    config = config or get_config()
    if ai_enabled is None:
        ai_enabled = config.ai_enabled
    if ai_enabled:
        return (as_float(config.get("breaking.threshold",
                                    config.settings.breaking_news_threshold), 90.0), 75.0, 60.0)
    return (as_float(config.get("breaking.rule.hot_score"), 62.0),
            as_float(config.get("breaking.rule.star_score"), 54.0),
            as_float(config.get("breaking.rule.dot_score"), 49.0))


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
    hours = as_float(config.get("breaking.rule.max_age_hours"), MAX_AGE_DEFAULT)
    heat_bar = as_float(config.get("breaking.rule.min_community_heat"), 0.0)
    rescued = (f"，或全站热度 ≥{heat_bar:.0f} 的大事件（社区来源也可破例；"
               f"只差热度的会在 {hours:.0f} 小时内每轮再问一次）" if heat_bar > 0 else "")
    # The reader is told *when* it may arrive, not only what counts: a rule that
    # decides whether to wake him belongs on the same line as the rule that decides
    # whether it is news.
    quiet = quiet_hours_text(config)
    quiet_text = f"；{quiet} 静默，出窗口自动补发" if quiet else ""
    return f"标题里有大事件 + 一手来源 + {hours:.0f} 小时内{rescued}{quiet_text}"


def note_deferral(row: Any, reason: str) -> int:
    """Remember that a qualified 突发 was turned away by a gate that reopens.

    `send_breaking` is only ever offered the ids the current round processed, and a
    row rejected for cooldown is already `is_processed=True` - so without a marker
    on the row the rejection is final. Measured 2026-09-28: 3 rows cleared the gate,
    #1101 was rejected with `cooldown 29 min left` at 22:12 and never appeared again,
    while its freshness window ran until 22:00 the next day.

    Returns the attempt count so the caller can say how long it has been waiting.
    """
    meta = dict(row.meta or {})
    info = dict(meta.get(DEFERRAL_KEY) or {})
    tries = int(info.get("tries") or 0) + 1
    info.update({"tries": tries, "reason": reason})
    meta[DEFERRAL_KEY] = info
    row.meta = meta
    return tries


def clear_deferral(row: Any) -> dict[str, Any]:
    """Drop the marker once the story is delivered or the reason is final.

    Returns what was recorded ({} when there was nothing to clear), so the caller
    can log why a deferred story finally went away instead of a silent disappearance.
    """
    meta = dict(row.meta or {})
    info = meta.get(DEFERRAL_KEY)
    if info is None:
        return {}
    meta.pop(DEFERRAL_KEY)
    row.meta = meta
    return dict(info)
