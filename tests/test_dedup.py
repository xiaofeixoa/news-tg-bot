"""Dedup tests: same URL, tracking params, same title, similar titles, cross-source.

design doc sections 10, 36.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.database.models import Article, Event
from app.database import repository as repo
from app.processing import deduplicate
from app.processing.normalize import build_article, normalize_url, title_key


def article(title: str, url: str, *, source: str = "TechCrunch", hours: int = 1) -> dict:
    return build_article(
        title=title, url=url, source_name=source,
        content=f"{title} — body text about a new AI model and its benchmark results.",
        published_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours),
    )


# ------------------------------------------------------------ URL identity
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://OpeNAI.com/index/gpt-5/?utm_source=x&utm_campaign=y", "https://openai.com/index/gpt-5"),
        ("http://www.example.com/a/b/#frag", "https://example.com/a/b"),
        ("https://example.com:443/path//double/?ref=tw", "https://example.com:443/path/double"),
        ("https://news.ycombinator.com/item?id=1", "https://news.ycombinator.com/item?id=1"),
        ("https://site.com/post?a=1&b=2&utm_medium=newsletter", "https://site.com/post?a=1&b=2"),
    ],
)
def test_normalise_url_strips_tracking_and_fragments(raw, expected):
    assert normalize_url(raw) == expected


def test_identical_url_with_different_tracking_shares_one_hash():
    a = normalize_url("https://ex.com/article/?utm_source=nl&utm_medium=email")
    b = normalize_url("https://ex.com/article")
    assert a == b
    from app.processing.normalize import url_hash

    assert url_hash("https://ex.com/a?fbclid=abc") == url_hash("https://ex.com/a")


@pytest.mark.asyncio
async def test_exact_url_is_not_stored_twice(session, article_factory):
    data = article("OpenAI ships GPT-5", "https://openai.com/gpt-5")
    assert repo.save_article(session, dict(data)) is not None
    session.commit()
    again = await deduplicate.resolve_duplicate(session, dict(data), deduplicate.RecentIndex())
    assert again.duplicate and again.method == "url"
    assert repo.save_article(session, dict(data)) is None
    session.commit()
    assert session.scalar(select(func.count(Article.id))) == 1


# -------------------------------------------------------- title similarity
@pytest.mark.parametrize(
    "left,right,expect_dup",
    [
        ("OpenAI releases GPT-5 for everyone", "OpenAI releases GPT-5 for everyone", True),
        ("OpenAI releases GPT-5", "Anthropic releases Claude 4.6", False),
        ("Anthropic releases Claude Opus 4.5", "Anthropic launched Claude Opus 4.5", True),
        ("NVIDIA announces B200 GPU", "NVIDIA B200 GPU announced", True),
        ("GPT-4 gets a bigger context window", "GPT-5 gets a bigger context window", False),
    ],
)
def test_title_similarity_threshold_decides(left, right, expect_dup):
    duplicate, score = deduplicate.is_same_headline(left, right)
    assert duplicate is expect_dup, f"{left!r} vs {right!r} scored {score:.2f}"


@pytest.mark.asyncio
async def test_similar_title_from_other_source_merges_into_one_event(session):
    first = article("OpenAI releases GPT-5 with 400k context", "https://openai.com/gpt-5",
                    source="OpenAI")
    stored = repo.save_article(session, dict(first))
    session.commit()
    assert stored is not None

    index = deduplicate.RecentIndex.from_articles([stored])
    second = article("OpenAI releases GPT-5 featuring a 400k context window",
                     "https://techcrunch.com/gpt-5", source="TechCrunch")
    result = await deduplicate.resolve_duplicate(session, second, index)
    assert result.duplicate and result.method == "title"
    assert result.matched_id == stored.id


@pytest.mark.asyncio
async def test_different_story_same_source_is_not_a_duplicate(session):
    stored = repo.save_article(session, dict(article("OpenAI releases GPT-5", "https://openai.com/gpt-5")))
    session.commit()
    index = deduplicate.RecentIndex.from_articles([stored])
    other = article("NVIDIA reports record GPU revenue", "https://nvidia.com/news/1", source="NVIDIA")
    result = await deduplicate.resolve_duplicate(session, other, index)
    assert not result.duplicate


@pytest.mark.asyncio
async def test_full_collect_run_stores_one_row_per_story(session, article_factory):
    """Pipeline-level check: three headlines about one event -> one digest line."""
    from app.config import get_config
    from app.processing.pipeline import collect

    class Feed:
        source_name = "Wires"

        async def collect(self):
            return [
                article("OpenAI releases GPT-5", "https://openai.com/gpt-5", source="OpenAI"),
                article("OpenAI has released GPT-5", "https://theverge.com/gpt-5-2", source="The Verge"),
                article("A totally different GPU story", "https://nvidia.com/other", source="NVIDIA"),
            ]

    before = session.scalar(select(func.count(Article.id))) or 0
    stats = await collect(session, [Feed()], config=get_config(), llm=None)
    session.commit()
    assert stats.stored + stats.duplicates == 3
    assert stats.duplicates >= 1

    events = session.scalars(select(Event).order_by(Event.id.desc()).limit(5)).all()
    merged = [e for e in events if (e.member_count or 0) >= 2]
    assert merged, "two headlines about one story should share an event"
    # Digest queries must collapse the event to a single row.
    from app.services.news import get_news_service

    items = get_news_service().latest(limit=20, min_score=0, hours=48)
    titles = [i.title for i in items]
    assert sum("GPT-5" in t or "GPT-5" in t.upper() for t in titles) <= 1


@pytest.mark.asyncio
async def test_numbers_in_titles_block_false_merges(session):
    stored = repo.save_article(session, dict(article("Claude 4.5 beats GPT-5 on coding",
                                                      "https://a.com/1")))
    session.commit()
    index = deduplicate.RecentIndex.from_articles([stored])
    other = article("Claude 4.6 beats GPT-6 on coding", "https://b.com/2")
    result = await deduplicate.resolve_duplicate(session, other, index)
    assert not result.duplicate


@pytest.mark.asyncio
async def test_recollecting_the_same_feeds_is_idempotent(session, article_factory):
    """Running collection twice must not error and must not duplicate rows."""
    from app.config import get_config
    from app.processing.pipeline import collect
    from sqlalchemy import func, select

    items = [
        article("OpenAI releases GPT-5", "https://openai.com/gpt-5", source="OpenAI"),
        article("Anthropic ships Claude 4.6", "https://anthropic.com/claude-46", source="Anthropic"),
        article("NVIDIA B200 ships to partners", "https://nvidia.com/b200", source="NVIDIA"),
    ]

    class Feed:
        source_name = "Wires"

        async def collect(self):
            return [dict(item) for item in items]

    first = await collect(session, [Feed()], config=get_config(), llm=None)
    session.commit()
    after_first = session.scalar(select(func.count(Article.id)))
    assert first.stored == 3 and first.errors == []

    second = await collect(session, [Feed()], config=get_config(), llm=None)
    session.commit()
    assert second.errors == [], f"re-collect raised: {second.errors}"
    assert second.stored == 0
    assert second.duplicates == 3
    assert session.scalar(select(func.count(Article.id))) == after_first


def test_title_key_is_order_and_case_insensitive():
    assert title_key("NVIDIA B200 GPU announced") == title_key("Announced: NVIDIA B200 GPU")
    assert "4.5" in title_key("Claude Opus 4.5")  # version numbers survive intact
