"""Multi-layer deduplication (design doc section 10).

1. URL identity          - exact, handled by normalise + a UNIQUE index
2. Normalised URL        - tracking params stripped before hashing
3. Title similarity      - sequence ratio + token Jaccard inside a time window
4. AI judgement          - optional, only for borderline pairs
5. Event merge           - duplicates share one Event so Telegram sends once
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import AppConfig, get_config
from app.database.models import Article
from app.logging_setup import get_logger
from app.processing.normalize import title_key, tokens_of, version_numbers

log = get_logger("app")

# Mirror of dedup.title_similarity in config/settings.yaml; the YAML wins.
DEFAULT_TITLE_SIMILARITY = 0.79


@dataclass
class DedupResult:
    duplicate: bool = False
    matched_id: int | None = None
    matched_title: str | None = None
    similarity: float = 0.0
    method: str = "new"

    @property
    def event_id(self) -> int | None:
        return self.matched_id


@dataclass
class RecentIndex:
    """Titles we already hold, pre-cached so a run does not re-query per item."""

    entries: list[tuple[int, str, frozenset[str], datetime, int | None, str]] = field(default_factory=list)

    @classmethod
    def from_articles(cls, articles: Iterable[Article]) -> "RecentIndex":
        return cls(
            [
                (a.id, a.title_norm or title_key(a.title), frozenset(tokens_of(a.title)),
                 a.published_at, a.event_id, a.title)
                for a in articles
            ]
        )

    def add(self, article: Article) -> None:
        self.entries.append(
            (article.id, article.title_norm or title_key(article.title),
             frozenset(tokens_of(article.title)), article.published_at, article.event_id, article.title)
        )


def jaccard(a: frozenset[str] | set[str], b: frozenset[str] | set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def sequence_ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def coverage(a: set[str], b: set[str]) -> float:
    """Shared words over the shorter headline - reworded quotes score high here."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def versions_conflict(title_a: str, title_b: str) -> bool:
    """'GPT-4 …' and 'GPT-5 …' share eight words but are two different stories."""
    nums_a = version_numbers(title_a)
    nums_b = version_numbers(title_b)
    if not nums_a or not nums_b:
        return False
    return not (nums_a & nums_b)


def title_similarity(title_a: str, title_b: str) -> float:
    """Blend of token order, token set and literal similarity.

    A version-number clash halves the result so the pairs below stay apart:
    "Claude Opus 4.5" vs "Claude Opus 4.6".
    """
    norm_a, norm_b = title_key(title_a), title_key(title_b)
    if not norm_a or not norm_b:
        return 0.0
    if norm_a == norm_b:
        return 1.0
    tokens_a, tokens_b = tokens_of(title_a), tokens_of(title_b)
    token_seq = SequenceMatcher(None, norm_a.split(), norm_b.split()).ratio()
    char_seq = SequenceMatcher(None, norm_a, norm_b).ratio()
    cov = coverage(tokens_a, tokens_b)
    jac = jaccard(tokens_a, tokens_b)
    score = max(token_seq, char_seq, 0.6 * cov + 0.4 * jac)
    if versions_conflict(title_a, title_b):
        score *= 0.5
    return round(score, 4)


def is_same_headline(title_a: str, title_b: str, config: AppConfig | None = None) -> tuple[bool, float]:
    """The single decision point used by both the index scan and the tests."""
    config = config or get_config()
    threshold = float((config.get("dedup", {}) or {}).get("title_similarity", DEFAULT_TITLE_SIMILARITY))
    score = title_similarity(title_a, title_b)
    shared = len(tokens_of(title_a) & tokens_of(title_b))
    # One-word headlines need a higher bar: "AI" vs "AI" is not a duplicate.
    confident = score >= threshold and (shared >= 3 or score >= 0.95)
    return confident, score


def find_url_duplicate(session: Session, url_hash: str) -> Article | None:
    return session.scalar(select(Article).where(Article.url_hash == url_hash))


def find_title_duplicate(
    index: RecentIndex,
    *,
    title: str,
    published_at: datetime,
    config: AppConfig | None = None,
) -> tuple[int | None, float]:
    """Best-scoring headline already held inside the comparison window."""
    config = config or get_config()
    dedup_cfg = config.get("dedup", {}) or {}
    low = float(dedup_cfg.get("ai_review_low_bound", 0.60))
    window = int(dedup_cfg.get("window_hours", 72))
    cutoff = published_at - timedelta(hours=window)
    tokens = tokens_of(title)
    if not tokens:
        return None, 0.0
    best_id: int | None = None
    best_score = 0.0
    for article_id, _norm, candidate_tokens, article_published, _event, article_title in index.entries:
        if article_published < cutoff or not candidate_tokens:
            continue
        # Cheap set-overlap filter first: SequenceMatcher over thousands of
        # titles per run would dominate the CPU budget.
        if coverage(tokens, set(candidate_tokens)) < min(low, 0.5):
            continue
        score = title_similarity(title, article_title)
        if score > best_score:
            best_id, best_score = article_id, score
    return best_id, best_score


def borderline(low: float, high: float, score: float) -> bool:
    return low <= score < high


def make_event_key(title: str, published_at: datetime | None = None) -> str:
    """Deterministic key for the story behind a headline."""
    basis = title_key(title)[:180] or (title or "").lower()[:180]
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:40]


async def resolve_duplicate(
    session: Session,
    data: dict[str, Any],
    index: RecentIndex,
    *,
    config: AppConfig | None = None,
    llm: Any = None,
    ai_budget: Sequence[int] | None = None,
) -> DedupResult:
    """Full multi-layer check for one not-yet-stored article."""
    config = config or get_config()
    dedup_cfg = config.get("dedup", {}) or {}
    high = float(dedup_cfg.get("title_similarity", DEFAULT_TITLE_SIMILARITY))
    low = float(dedup_cfg.get("ai_review_low_bound", 0.60))

    url_match = find_url_duplicate(session, data["url_hash"])
    if url_match is not None:
        return DedupResult(True, url_match.id, url_match.title, 1.0, "url")

    match_id, score = find_title_duplicate(
        index, title=data["title"], published_at=data["published_at"], config=config
    )
    matched = session.get(Article, match_id) if match_id is not None else None
    if matched is not None and score >= high:
        return DedupResult(True, matched.id, matched.title, score, "title")

    if (
        matched is not None
        and bool(dedup_cfg.get("ai_review_enabled"))
        and borderline(low, high, score)
        and llm is not None
        and getattr(llm, "enabled", False)
        and (ai_budget[0] if ai_budget else 1) > 0
    ):
        if await llm.same_event(data, _as_dict(matched)):
            if ai_budget is not None:
                ai_budget[0] -= 1
            return DedupResult(True, matched.id, matched.title, score, "ai")
    return DedupResult(False, None, None, score, "new")


def _as_dict(article: Article) -> dict[str, Any]:
    return {
        "title": article.title,
        "source_name": article.source_name,
        "content": article.content or article.summary or "",
    }
