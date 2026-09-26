"""Keyword filtering + classification (design doc sections 12, 23 layer 1/2).

Rules run first and cost nothing; the LLM only sees items that passed the
keyword gate, and only to pick from the configured taxonomy.
"""

from __future__ import annotations

from typing import Any

from app.config import AppConfig, get_config
from app.logging_setup import get_logger
from app.processing.normalize import clean_text, keyword_hits

log = get_logger("app")


def haystack(article: dict[str, Any], *, content_chars: int = 1200) -> str:
    content = (article.get("content") or "")[:content_chars]
    return f"{article.get('title', '')}. {content}"


def rule_filter(article: dict[str, Any], config: AppConfig | None = None) -> tuple[bool, list[str]]:
    """Gate: is this worth a model call at all? Returns (keep, matched keywords).

    A recognised free-offer (config/free_offers.yaml) always passes: promo posts
    on community boards rarely say "LLM" or "inference", yet they are exactly
    what the /免费 command needs.
    """
    config = config or get_config()
    keywords = config.filter_keywords
    text = haystack(article, content_chars=800)
    hits = keyword_hits(text, keywords) if keywords else [""]
    needed = int(config.get("filters.min_keyword_hits", 1))
    if len(hits) >= needed:
        return True, hits
    from app.processing import free_offers

    if free_offers.detect(article.get("title"), article.get("content"), config=config) is not None:
        return True, ["free-offer"]
    return False, hits


def rule_classify(article: dict[str, Any], config: AppConfig | None = None) -> tuple[str | None, str | None, float]:
    """Score every top category by keyword hits; also try subcategories.

    Returns (category, subcategory, confidence 0..1). Confidence is low on
    purpose - it exists to skip the model when a source name alone decides it.
    """
    config = config or get_config()
    text = haystack(article).lower()
    scores: dict[str, int] = {}
    for category in config.category_names:
        hits = sum(1 for kw in config.keywords(category) if kw in text)
        if hits:
            scores[category] = hits
    if not scores:
        return None, None, 0.0
    best = max(scores, key=lambda k: (scores[k], -len(k)))
    total = sum(scores.values())
    confidence = scores[best] / total if total else 0.0
    subcategory = _best_subcategory(article, text, config, best)
    return best, subcategory, round(min(confidence, 1.0), 2)


def _best_subcategory(article: dict[str, Any], text: str, config: AppConfig, category: str) -> str | None:
    meta = config.category_meta(category) or {}
    subs = meta.get("subcategories") or {}
    best_name, best_hits = None, 0
    for name, keywords in subs.items():
        hits = sum(1 for kw in [str(k).lower() for k in (keywords or [])] if kw and kw in text)
        # Title matches weigh more than body matches.
        title = clean_text(article.get("title", "")).lower()
        hits += sum(2 for kw in [str(k).lower() for k in (keywords or [])] if kw and kw in title)
        if hits > best_hits:
            best_name, best_hits = name, hits
    return best_name if best_hits >= 2 else None


def source_hint(article: dict[str, Any], config: AppConfig | None = None) -> str | None:
    """An official lab blog is almost always 'AI Models'/'Companies' news."""
    config = config or get_config()
    name = (article.get("source_name") or "").lower()
    mapping = {
        "openai": "AI Models",
        "anthropic": "AI Models",
        "google deepmind": "Research",
        "google ai": "AI Models",
        "hugging face": "Open Source",
        "nvidia": "AI Infrastructure",
        "aws ml": "AI Infrastructure",
        "github trending": "Open Source",
        "github releases": "Open Source",
        "arxiv": "Research",
        "reddit": "AI Agent",
        "hacker news": None,
    }
    for key, value in mapping.items():
        if key in name:
            return value
    return None


async def classify(
    article: dict[str, Any],
    *,
    interests: list[dict[str, Any]] | None = None,
    config: AppConfig | None = None,
    llm: Any = None,
) -> dict[str, Any]:
    """Merge rule guesses with the model answer; never fails the pipeline."""
    config = config or get_config()
    rule_category, rule_sub, confidence = rule_classify(article, config)
    hint = source_hint(article, config)
    result: dict[str, Any] = {
        "category": rule_category or hint or config.fallback_category,
        "subcategory": rule_sub,
        "relevance_score": 50.0,
        "importance_score": 0.0,
        "novelty_score": 80.0,
        "is_ai_related": True,
        "tags": [],
        "method": "rule",
    }
    if llm is None or not getattr(llm, "enabled", False):
        return result
    try:
        ai = await llm.classify(article, interests or [])
    except Exception as exc:  # LLM hiccups must not lose news (section 25)
        log.warning("classify failed for %r: %s", article.get("title"), exc)
        result["error"] = str(exc)[:200]
        return result
    result["method"] = "ai"
    result["category"] = ai.get("category") or result["category"]
    result["subcategory"] = ai.get("subcategory") or rule_sub
    result["relevance_score"] = ai.get("relevance_score", result["relevance_score"])
    result["importance_score"] = ai.get("importance_score", 0)
    result["novelty_score"] = ai.get("novelty_score", result["novelty_score"])
    result["is_ai_related"] = ai.get("is_ai_related", True)
    result["tags"] = ai.get("tags") or []
    result["reason"] = ai.get("reason", "")
    # A rule saw a clear AI keyword but the model disagreed: trust the model
    # only when it also rated relevance.
    if not result["is_ai_related"] and result["relevance_score"] < 40:
        result["relevance_score"] = min(result["relevance_score"], 25)
    return result
