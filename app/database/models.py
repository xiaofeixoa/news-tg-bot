"""SQLAlchemy models (design doc section 18).

articles / sources / tags / article_tags / users / user_interests are the
documented schema. events and push_logs implement two requirements the spec
asks for but the schema does not carry: multi-source merging (10.5 / 41) and
breaking-news cooldown + daily cap (16.3).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Naive UTC: one clock everywhere, converted at the edge for the user."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


article_tags = Table(
    "article_tags",
    Base.metadata,
    Column("article_id", ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    type: Mapped[str] = mapped_column(String(32), default="rss")
    url: Mapped[str | None] = mapped_column(String(512))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    quality: Mapped[str] = mapped_column(String(2), default="C")
    category: Mapped[str | None] = mapped_column(String(64))

    fetch_interval: Mapped[int | None] = mapped_column(Integer)
    last_fetch_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    item_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    articles: Mapped[list["Article"]] = relationship(back_populates="source")


class Event(Base):
    """One real-world story; several articles can belong to it."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(512))
    # events <-> articles reference each other, so this edge is declared as an
    # alteration: it lets drop_all()/migrations resolve the known cycle.
    canonical_article_id: Mapped[int | None] = mapped_column(
        ForeignKey("articles.id", ondelete="SET NULL", use_alter=True, name="fk_events_canonical_article")
    )
    member_count: Mapped[int] = mapped_column(Integer, default=1)
    source_names: Mapped[list[str] | None] = mapped_column(JSON, default=list)
    summary: Mapped[str | None] = mapped_column(Text)
    final_score: Mapped[float] = mapped_column(Float, default=0)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    articles: Mapped[list["Article"]] = relationship(
        back_populates="event", foreign_keys="Article.event_id"
    )


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (
        UniqueConstraint("url_hash", name="uq_article_url_hash"),
        UniqueConstraint("hash", name="uq_article_hash"),
        Index("ix_articles_published_score", "published_at", "final_score"),
        Index("ix_articles_processed_score", "is_processed", "final_score"),
        Index("ix_articles_category_published", "category", "published_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"), index=True)
    source_name: Mapped[str] = mapped_column(String(120), default="", index=True)
    source_type: Mapped[str] = mapped_column(String(32), default="rss")

    title: Mapped[str] = mapped_column(String(512))
    url: Mapped[str] = mapped_column(String(1024))
    normalized_url: Mapped[str] = mapped_column(String(1024), index=True)
    # sha1(normalized_url) - URL-level dedup, and the `hash` field of the
    # standardised Article object from section 9.
    url_hash: Mapped[str] = mapped_column(String(40))
    hash: Mapped[str] = mapped_column(String(40))
    title_norm: Mapped[str] = mapped_column(String(512), default="", index=True)

    author: Mapped[str | None] = mapped_column(String(255))
    content: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(8), default="en")

    published_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    summary: Mapped[str | None] = mapped_column(Text)
    why_it_matters: Mapped[str | None] = mapped_column(Text)
    key_points: Mapped[list[str] | None] = mapped_column(JSON)

    # 中文输出：上游是英文源时，这里存中文标题/摘要（LLM 或免费 MT）。
    # 原文永远保留，去重仍基于原文；检索两边都查，否则中文问句搜不到东西。
    title_zh: Mapped[str | None] = mapped_column(String(512))
    summary_zh: Mapped[str | None] = mapped_column(Text)
    translated_by: Mapped[str | None] = mapped_column(String(16))

    category: Mapped[str | None] = mapped_column(String(64))
    subcategory: Mapped[str | None] = mapped_column(String(64))
    tags: Mapped[list["Tag"]] = relationship(secondary=article_tags, back_populates="articles")

    importance_score: Mapped[float] = mapped_column(Float, default=0)
    relevance_score: Mapped[float] = mapped_column(Float, default=0)
    novelty_score: Mapped[float] = mapped_column(Float, default=50)
    source_quality: Mapped[float] = mapped_column(Float, default=50)
    community_heat: Mapped[float] = mapped_column(Float, default=0)
    final_score: Mapped[float] = mapped_column(Float, default=0, index=True)

    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    # articles.event_id and events.canonical_article_id are two FKs between the
    # same tables, so every relationship here names its own join key.
    event: Mapped[Event | None] = relationship(
        back_populates="articles", foreign_keys=[event_id]
    )

    # Collector extras: HN points, Reddit ups, GitHub stars, arXiv id, ...
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=dict)

    is_processed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_breaking: Mapped[bool] = mapped_column(Boolean, default=False)

    # /免费：这条新闻是不是"某工具/模型现在免费"的资讯（config/free_offers.yaml）
    is_free_offer: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    free_offer_tool: Mapped[str | None] = mapped_column(String(64), index=True)
    free_offer: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # 主动推送只发一次：没有这一列就只能靠时间戳猜哪些已经通知过。
    free_offer_sent_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    process_error: Mapped[str | None] = mapped_column(Text)
    process_attempts: Mapped[int] = mapped_column(Integer, default=0)
    # 这一列是"这条什么时候被处理完"的唯一真答案：`updated_at` 会被翻译、正文提取、
    # is_sent 等后续写入不断推后，用它推算处理延迟得到过 45% 的假积压。
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    filtered_out: Mapped[bool] = mapped_column(Boolean, default=False)

    source: Mapped[Source | None] = relationship(back_populates="articles")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Article {self.id} [{self.source_name}] {self.title[:60]!r}>"


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    articles: Mapped[list[Article]] = relationship(secondary=article_tags, back_populates="tags")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_chat_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    telegram_user_id: Mapped[int | None] = mapped_column(Integer)
    display_name: Mapped[str | None] = mapped_column(String(120))

    language: Mapped[str] = mapped_column(String(8), default="zh")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")

    daily_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    daily_time: Mapped[str] = mapped_column(String(5), default="08:00")
    evening_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    evening_time: Mapped[str] = mapped_column(String(5), default="20:00")
    breaking_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    breaking_threshold: Mapped[float] = mapped_column(Float, default=90)
    min_score: Mapped[float] = mapped_column(Float, default=45)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    interests: Mapped[list["UserInterest"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserInterest(Base):
    __tablename__ = "user_interests"
    __table_args__ = (UniqueConstraint("user_id", "type", "value", name="uq_interest"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # topic | keyword | company | model | repo | exclude
    type: Mapped[str] = mapped_column(String(24), default="topic")
    value: Mapped[str] = mapped_column(String(120))
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship(back_populates="interests")


class PushLog(Base):
    """Every automatic push, used for breaking-news cooldown and daily caps."""

    __tablename__ = "push_logs"
    __table_args__ = (Index("ix_pushlog_user_kind_time", "user_id", "kind", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # breaking | morning | evening | manual
    kind: Mapped[str] = mapped_column(String(16), default="manual")
    article_id: Mapped[int | None] = mapped_column(ForeignKey("articles.id", ondelete="SET NULL"))
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
