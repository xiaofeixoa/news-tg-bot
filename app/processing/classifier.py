"""Keyword filtering + classification (design doc sections 12, 23 layer 1/2).

Rules run first and cost nothing; the LLM only sees items that passed the
keyword gate, and only to pick from the configured taxonomy.
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable

from app.config import AppConfig, get_config
from app.logging_setup import get_logger
from app.processing.normalize import clean_text, keyword_hits

log = get_logger("app")

_MATCHERS: dict[str, re.Pattern[str]] = {}


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


def _matcher(keyword: str) -> re.Pattern[str]:
    """Whole-word match, cached.

    Substring matching misfiles whole families: "ipo" hits "pivot", "app" hits
    "happen", "ban" hits "company"/"coupon", and a Companies list that needs
    "court"/"ban" starts matching half the corpus.
    """
    key = keyword.lower()
    cached = _MATCHERS.get(key)
    if cached is None:
        try:
            cached = re.compile(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])")
        except re.error:  # pragma: no cover - keywords are plain words
            cached = re.compile(re.escape(key))
        _MATCHERS[key] = cached
    return cached


def _hits(text: str, keywords: Iterable[str]) -> int:
    return sum(1 for kw in keywords if kw and _matcher(kw).search(text))


# "internlm/Intern-Decision 4B and 0.8B", "Qwen3.8-27B": a parameter count is the
# strongest available sign that a post is about a model, and it appears in titles
# that contain no other taxonomy word at all. "$1.5B" is money, not a model, so a
# dollar sign disqualifies the match.
_SIZE_RE = re.compile(r"(?<!\$)(?<![a-z0-9$.])\d+(?:\.\d+)?[bB](?![a-z0-9])")

# `owner/name`, with or without "(37 stars)" / "released in", is a repository
# entry no matter what words the repo happens to have in its name - "ai-system-
# design" is not an AI Applications article. Titles of this shape are a large
# share of the GitHub feeds, so getting them right is most of the column's health.
_REPO_TITLE_RE = re.compile(r"^\s*[a-z0-9_.+-]+/[a-z0-9_.+-]+", re.I)
_STARS_RE = re.compile(r"\(\s*\d[\d,.]*\s*stars?\)", re.I)
_RELEASE_RE = re.compile(r"^\s*v?\d+(?:\.\d+)*\s+released\s+in\s+", re.I)


def _repo_shaped(title: str) -> bool:
    return bool(_REPO_TITLE_RE.search(title) or _STARS_RE.search(title) or _RELEASE_RE.search(title))


def rule_classify(article: dict[str, Any], config: AppConfig | None = None) -> tuple[str | None, str | None, float]:
    """Score every top category; the source's own beat is a prior, not a fallback.

    Four things decide the winner, because raw keyword counts alone scored
    18/49 on live headlines (2026-09-26, now 45/49 on the same set):
      * title hits count double - a column is chosen by the headline, not by an
        incidental word in the first 1200 characters;
      * each category's score is divided by the square root of its list size, so
        "AI Models" with 17 keywords does not beat "AI Infrastructure" with 13
        merely by having more ways to accidentally match;
      * `source_hint` adds weight, because a GitHub trending page is open source
        and an arXiv paper is research even when the title says neither;
      * a parameter count in the title is an AI Models signal of its own;
      * an `owner/name` title is a repository, whatever words its name contains.
    """
    config = config or get_config()
    title = clean_text(article.get("title", "")).lower()
    body = haystack(article)
    scores: dict[str, float] = {}
    for category in config.category_names:
        keywords = config.keywords(category)
        if not keywords:
            continue
        hits = 2 * _hits(title, keywords) + _hits(body, keywords)
        if hits:
            scores[category] = hits / math.sqrt(len(keywords))
    size_weight = float(config.get("classify.model_size_weight", 0.6))
    if size_weight and _SIZE_RE.search(title):
        key = "AI Models" if "AI Models" in config.category_names else config.fallback_category
        scores[key] = scores.get(key, 0.0) + size_weight
    repo_weight = float(config.get("classify.repo_title_weight", 1.0))
    if repo_weight and _repo_shaped(title):
        key = "Open Source" if "Open Source" in config.category_names else config.fallback_category
        scores[key] = scores.get(key, 0.0) + repo_weight
    hint_category, hint_weight = source_hint_with_weight(article, config)
    if hint_category:
        scores[hint_category] = scores.get(hint_category, 0.0) + hint_weight
    if not scores:
        return None, None, 0.0
    best = max(scores, key=lambda k: (scores[k], -len(k)))
    total = sum(scores.values())
    subcategory = _best_subcategory(article, body, config, best)
    return best, subcategory, round(min(scores[best] / total, 1.0), 2) if total else 0.0


def _best_subcategory(article: dict[str, Any], text: str, config: AppConfig, category: str) -> str | None:
    meta = config.category_meta(category) or {}
    subs = meta.get("subcategories") or {}
    title = clean_text(article.get("title", "")).lower()
    best_name, best_hits = None, 0
    for name, keywords in subs.items():
        words = [str(k).lower() for k in (keywords or [])]
        # Title matches weigh more than body matches.
        hits = 2 * _hits(title, words) + _hits(text, words)
        if hits > best_hits:
            best_name, best_hits = name, hits
    return best_name if best_hits >= 2 else None


def source_hint(article: dict[str, Any], config: AppConfig | None = None) -> str | None:
    """An official lab blog is almost always 'AI Models'/'Companies' news."""
    category, _weight = source_hint_with_weight(article, config)
    return category


# Venue sources basically decide the column: every arXiv item is research and
# every GitHub trending/release row is a repository, whatever the title's topic
# words are. A vendor blog is only a leaning - "How to Use AI Agents to Prepare
# 3D Scenes" on the NVIDIA blog is an agent story, and a heavy hint used to
# misfile it into AI Infrastructure.
_VENUE_SOURCES = ("arxiv", "github trending", "github releases", "hugging face")


def source_hint_with_weight(article: dict[str, Any],
                            config: AppConfig | None = None) -> tuple[str | None, float]:
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
        # 社区源不给提示：r/LocalLLaMA 的帖子多在讲模型和显卡，把它当
        # "AI Agent" 会让提示权重压过标题里的实词（实测分错一整族）。
        "reddit": None,
        "hacker news": None,
    }
    for key, value in mapping.items():
        if key in name:
            if not value:
                return None, 0.0
            strong = any(venue in name for venue in _VENUE_SOURCES)
            weight = float(config.get("classify.venue_hint_weight" if strong
                                      else "classify.source_hint_weight", 1.2 if strong else 0.35))
            return value, weight
    return None, 0.0


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
