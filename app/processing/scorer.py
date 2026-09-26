"""Importance scoring (design doc section 11).

final_score = importance*.35 + relevance*.30 + novelty*.15
            + source_quality*.10 + community_heat*.10
Weights are read from config/settings.yaml, never hard-coded.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from app.config import AppConfig, get_config
from app.logging_setup import get_logger

log = get_logger("app")

QUALITY_DEFAULTS = {"A": 95, "B": 80, "C": 65, "D": 45}
# Community signals that different collectors report under different names.
HEAT_FIELDS = ("points", "upvotes", "stars", "stars_today", "comments", "score")
HEAT_WEIGHTS = {"points": 1.0, "upvotes": 1.0, "score": 1.0, "stars": 0.35, "stars_today": 1.5,
                "comments": 0.3}


def source_quality(source: dict[str, Any] | None = None, *, quality: str | None = None,
                   config: AppConfig | None = None) -> float:
    config = config or get_config()
    tier = (quality or (source or {}).get("quality") or "C").upper()[:1]
    table = {**QUALITY_DEFAULTS, **{k.upper(): v for k, v in (config.get("sources_quality", {}) or {}).items()}}
    return float(table.get(tier, 65))


def community_heat(article: dict[str, Any], config: AppConfig | None = None) -> float:
    """Squash wildly different signals (GitHub stars vs HN points) into 0..100."""
    config = config or get_config()
    meta = article.get("meta") or {}
    ceiling = float(config.get("scoring.community_heat_max_signal", 500))
    best = 0.0
    for field in HEAT_FIELDS:
        raw = meta.get(field)
        if raw is None:
            raw = article.get(field)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        weight = HEAT_WEIGHTS.get(field, 1.0)
        # log scaling: 100 points and 1000 points are not 10x apart in meaning
        signal = math.log10(1 + value * weight) / math.log10(1 + ceiling) * 100
        best = max(best, min(100.0, signal))
    # Discussion volume nudges heat up a little on its own.
    comments = float(meta.get("comments") or meta.get("num_comments") or 0)
    if comments >= 50:
        best = min(100.0, best + 8)
    return round(best, 1)


def novelty(age_hours: float, config: AppConfig | None = None) -> float:
    """First report is 100; a story that is two days old is worth much less."""
    if age_hours <= 0:
        return 100.0
    if age_hours <= 6:
        return 95.0
    if age_hours <= 24:
        return 85.0
    if age_hours <= 48:
        return 65.0
    if age_hours <= 96:
        return 45.0
    return max(10.0, 100.0 - age_hours / 6.0)


def interest_bonus(article: dict[str, Any], interests: list[dict[str, Any]],
                   config: AppConfig | None = None) -> float:
    """User preferences shift the score; excluded topics crush it."""
    config = config or get_config()
    ceiling = float(config.get("scoring.interest_bonus_max", 15))
    text = f"{article.get('title', '')} {article.get('content') or ''}".lower()
    tags = [str(t).lower() for t in (article.get("tags") or [])]
    bonus = 0.0
    for interest in interests or []:
        value = str(interest.get("value", "")).lower().strip()
        if not value:
            continue
        weight = float(interest.get("weight", 1.0) or 1.0)
        matched = value in text or value in tags or value == str(article.get("category", "")).lower()
        if interest.get("type") == "exclude":
            if matched:
                bonus -= ceiling * 1.6  # hard penalty, effectively filters it out
        elif matched:
            bonus += ceiling * weight * 0.5
    return round(max(-ceiling * 2, min(ceiling, bonus)), 1)


def tier_cap(quality: str | None, config: AppConfig | None = None) -> float:
    """A random trending repo must not outrank an official announcement.

    Community (C) and personal (D) sources are leads, not confirmations, so their
    final score is capped below the breaking-news threshold.
    """
    config = config or get_config()
    caps = config.get("sources_quality.tier_caps", {"A": 100, "B": 100, "C": 78, "D": 68}) or {}
    return float(caps.get((quality or "C").upper()[:1], 78))


def compute_scores(
    article: dict[str, Any],
    *,
    importance: float | None = None,
    relevance: float | None = None,
    novelty_value: float | None = None,
    quality: str | None = None,
    interests: list[dict[str, Any]] | None = None,
    config: AppConfig | None = None,
) -> dict[str, float]:
    config = config or get_config()
    weights = config.scoring_weights
    published = article.get("published_at") or datetime.utcnow()
    age_hours = (datetime.utcnow() - published).total_seconds() / 3600 if isinstance(published, datetime) else 0

    scores = {
        "importance_score": float(importance if importance not in (None, 0) else article.get("importance_score") or 0),
        "relevance_score": float(relevance if relevance is not None else article.get("relevance_score") or 50),
        "novelty_score": float(novelty_value if novelty_value is not None else article.get("novelty_score")
                               or novelty(age_hours)),
        "source_quality": float(article.get("source_quality") or source_quality(article, quality=quality, config=config)),
        "community_heat": float(article.get("community_heat") or community_heat(article, config)),
    }
    # A story with no importance signal at all is filler: make sure the weighted
    # sum cannot float by on source quality alone.
    if scores["importance_score"] <= 0:
        scores["importance_score"] = round(scores["relevance_score"] * 0.6, 1)

    # Weight names in settings.yaml are short (importance/relevance/...);
    # the column names carry a _score suffix. Map one onto the other.
    components = {
        "importance": scores["importance_score"],
        "relevance": scores["relevance_score"],
        "novelty": scores["novelty_score"],
        "source_quality": scores["source_quality"],
        "community_heat": scores["community_heat"],
    }
    # Sources with no discussion metric (most official blogs) must still be able
    # to reach the breaking threshold, so a missing signal does not silently
    # cost the article its 10% - the remaining weights are renormalised.
    active = {k: v for k, v in components.items() if not (k == "community_heat" and v <= 0)}
    total_weight = sum(weights.get(name, 0.0) for name in active) or 1.0
    weighted = sum(active[name] * weights.get(name, 0.0) / total_weight for name in active)
    scores["weights_renormalised"] = 0.0 if len(active) == len(components) else 1.0
    bonus = interest_bonus(article, interests or [], config)
    cap = tier_cap(quality, config)
    final = max(0.0, min(cap, weighted + bonus))
    scores["final_score"] = round(final, 1)
    scores["interest_bonus"] = bonus
    log.debug("scores %r -> %s", article.get("title"), scores)
    return scores


def is_breaking(scores: dict[str, float], threshold: float) -> bool:
    return float(scores.get("final_score") or 0) >= float(threshold)
