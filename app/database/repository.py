"""Data access layer. All SQL lives here; services own the business rules."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.database.models import Article, Event, PushLog, Source, Tag, User, UserInterest, article_tags
from app.logging_setup import get_logger

log = get_logger("db")

# Collectors hand us extra hints (quality, feed metadata); only real columns
# may reach the ORM constructor.
_ARTICLE_COLUMNS = {column.key for column in Article.__table__.columns}


# ---------------------------------------------------------------- sources
def get_or_create_source(session: Session, name: str, type_: str = "rss", url: str | None = None,
                         quality: str = "C", category: str | None = None,
                         fetch_interval: int | None = None, *, enabled: bool = True) -> Source:
    source = session.scalar(select(Source).where(Source.name == name))
    if source is None:
        source = Source(
            name=name, type=type_, url=url, quality=quality, category=category,
            fetch_interval=fetch_interval, enabled=enabled,
        )
        session.add(source)
        session.flush()
    else:
        source.type = source.type or type_
        if url:
            source.url = url
        if quality:
            source.quality = quality
    return source


def sync_sources(session: Session, configured: Sequence[dict[str, Any]]) -> int:
    """Mirror `config/sources.yaml` into the sources table, and say how many rows moved.

    Rows used to be created lazily by the collector loop with `enabled=True` and
    never revisited, so switching a feed off in the config left the database
    claiming it was on: VentureBeat AI still reads `enabled=1, error_count=114`
    long after it was turned off, and the six-hourly maintenance job kept
    announcing failures for a source nobody was collecting any more.

    Turning a source off also clears its failure state. A counter carried over
    from before the switch would otherwise be reported as "currently broken" the
    moment it is re-enabled.
    """
    changed = 0
    known = {str(s.get("name")) for s in configured if s.get("name")}
    for entry in configured:
        name = str(entry.get("name") or "")
        if not name:
            continue
        wanted = bool(entry.get("enabled", True))
        source = session.scalar(select(Source).where(Source.name == name))
        if source is None:
            source = get_or_create_source(
                session, name, type_=str(entry.get("type") or "rss"),
                url=entry.get("url"), quality=str(entry.get("quality") or "C"),
                category=entry.get("category"), fetch_interval=entry.get("fetch_interval"),
                enabled=wanted)
            changed += 1
            continue
        if source.enabled is not wanted:
            source.enabled = wanted
            changed += 1
            if not wanted:
                source.error_count = 0
                source.last_error = None
        if entry.get("url") and source.url != entry.get("url"):
            source.url = str(entry["url"])
            changed += 1
        if entry.get("quality") and source.quality != str(entry["quality"]):
            source.quality = str(entry["quality"])
            changed += 1
        if str(entry.get("type") or "") and source.type != str(entry["type"]):
            source.type = str(entry["type"])
            changed += 1
    # Config deleted but the DB still remembers it (and would keep being reported):
    for source in list(session.scalars(select(Source))):
        if source.name not in known and source.enabled:
            source.enabled = False
            source.error_count = 0
            source.last_error = None
            changed += 1
    return changed


def mark_source_fetch(session: Session, source_id: int, *, ok: bool, error: str | None = None,
                      items: int = 0) -> None:
    source = session.get(Source, source_id)
    if source is None:
        return
    now = datetime.utcnow()
    source.last_fetch_at = now
    if ok:
        source.last_success_at = now
        source.last_error = None
        source.error_count = 0
        source.item_count = (source.item_count or 0) + items
    else:
        source.error_count = (source.error_count or 0) + 1
        source.last_error = (error or "unknown error")[:500]
        log.warning("source %s failed (%s): %s", source.name, source.error_count, source.last_error)


def all_sources(session: Session) -> list[Source]:
    return list(session.scalars(select(Source).order_by(Source.name)))


# --------------------------------------------------------------- articles
def article_exists(session: Session, url_hash: str) -> bool:
    return session.scalar(select(Article.id).where(Article.url_hash == url_hash)) is not None


def save_article(session: Session, data: dict[str, Any]) -> Article | None:
    """Insert one normalised article. Returns None when it was a duplicate."""
    tag_names = list(data.pop("tags", []) or [])
    payload = {k: v for k, v in data.items() if k in _ARTICLE_COLUMNS}
    missing = {"url_hash", "hash", "title", "url"} - set(payload)
    if missing:
        raise ValueError(f"article payload missing required fields: {sorted(missing)}")
    existing = session.scalar(select(Article).where(Article.url_hash == payload["url_hash"]))
    if existing is not None:
        _merge_duplicate(session, existing, payload)
        return None
    if article_exists_by_hash(session, payload["hash"]):
        return None
    article = Article(**payload)
    session.add(article)
    try:
        session.flush()
    except IntegrityError:
        # A concurrent collector wrote the same URL first: not an error.
        log.debug("duplicate article skipped: %s", data.get("normalized_url"))
        session.expunge_all()
        return None
    if tag_names:
        attach_tags(session, article, tag_names)
    return article


def article_exists_by_hash(session: Session, hash_: str) -> bool:
    return session.scalar(select(Article.id).where(Article.hash == hash_)) is not None


def _merge_duplicate(session: Session, article: Article, data: dict[str, Any]) -> None:
    """A URL we already have: refresh anything the later source knows better."""
    changed = False
    if not article.content and data.get("content"):
        article.content = data["content"]
        changed = True
    heat = float(data.get("community_heat") or 0)
    if heat > (article.community_heat or 0):
        article.community_heat = heat
        changed = True
    if changed:
        article.updated_at = datetime.utcnow()


def attach_tags(session: Session, article: Article, names: Iterable[str]) -> None:
    """Idempotent: `article_tags` has a composite primary key.

    A second pass over the same row - the degraded-row AI re-run, or a requeued
    article - used to append links that were already there and die on an
    IntegrityError deep inside the processing loop.

    The already-linked tag ids come from the link table, not from
    `article.tags`: a long-lived scheduler session can hold a collection that a
    concurrent collector has moved on, and then the append duplicates a row the
    database already has. Live boxes hit exactly that five times in one day and
    each hit took the whole processing round down with it (see `process_pending`).
    """
    attached = {tag.id for tag in article.tags}
    if article.id is not None:
        attached |= set(session.scalars(
            select(article_tags.c.tag_id).where(article_tags.c.article_id == article.id)))
    seen: set[int] = set()
    for raw in names:
        name = str(raw).strip()[:64]
        if not name:
            continue
        tag = session.scalar(select(Tag).where(func.lower(Tag.name) == name.lower()))
        if tag is None:
            tag = Tag(name=name)
            session.add(tag)
            session.flush()
        if tag.id in seen or tag.id in attached:
            continue
        article.tags.append(tag)
        seen.add(tag.id)


def unprocessed_articles(session: Session, limit: int = 25) -> list[Article]:
    """Freshest first: after a cold start the user should see today's news in
    the first digest, not the oldest item in the backlog."""
    stmt = (
        select(Article)
        .where(Article.is_processed.is_(False), Article.is_archived.is_(False))
        .order_by(Article.published_at.desc())
        .limit(limit)
        .options(selectinload(Article.tags), selectinload(Article.source))
    )
    return list(session.scalars(stmt))


def sources_that_delivered(session: Session, since: datetime) -> set[str]:
    """Names of sources that actually put news in the database since `since`.

    The configured list lies by omission: 37 sources are in sources.yaml, 20 are
    enabled, and only these names show which ones are really working tonight.
    """
    rows = session.scalars(select(Article.source_name)
                           .where(Article.created_at >= since)
                           .distinct())
    return {str(name) for name in rows if name}


def untranslated_articles(session: Session, limit: int = 120) -> list[Article]:
    """Processed rows still missing a Chinese title **or** summary.

    Highest score first, so the free provider's daily budget is spent on the
    stories that can actually reach a briefing. The summary half matters as much
    as the title one: enrichment and summary repair both clear `summary_zh`, and
    a queue that only looked at `title_zh` left those rows English forever -
    measured with 377 such rows on the deployed box.

    Rows still missing a *headline* translation go first. The daily budget is
    shared, and a 300-row summary backlog must not out-rank the one line every
    reader sees.
    """
    stmt = (
        select(Article)
        .where(Article.is_processed.is_(True), Article.is_archived.is_(False),
               Article.filtered_out.is_(False),
               or_(Article.title_zh.is_(None), Article.summary_zh.is_(None)))
        .order_by(case((Article.title_zh.is_(None), 0), else_=1),
                  Article.final_score.desc(), Article.published_at.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def rows_with_key_points(session: Session, *, limit: int = 200) -> list[Article]:
    """Visible rows that have bullets but no Chinese version of them.

    Score-ordered, because the free provider's leftover budget should go to the
    stories that can reach a briefing or a tap. On the deployed box 295 of the 423
    rows a reader can open carried `key_points` and showed no 核心内容 block at all.
    """
    stmt = (
        select(Article)
        .where(Article.is_processed.is_(True), Article.is_archived.is_(False),
               Article.filtered_out.is_(False), Article.key_points.isnot(None))
        .order_by(Article.final_score.desc(), Article.published_at.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def free_offers(session: Session, *, days: int = 30, limit: int = 20,
                tool: str | None = None, since: datetime | None = None) -> list[Article]:
    """Latest "this is free right now" items, newest first."""
    start = since or (datetime.utcnow() - timedelta(days=max(1, days)))
    stmt = (
        select(Article)
        .where(Article.is_free_offer.is_(True), Article.is_archived.is_(False),
               Article.filtered_out.is_(False), Article.published_at >= start)
        .order_by(Article.published_at.desc(), Article.final_score.desc())
        .limit(limit)
        .options(selectinload(Article.tags))
    )
    if tool:
        stmt = stmt.where(func.lower(Article.free_offer_tool) == tool.lower())
    return list(session.scalars(stmt))


def unannounced_free_offers(session: Session, *, limit: int = 5,
                            min_confidence: float = 0.0,
                            within_days: int = 7,
                            user_id: int | None = None,
                            require_title_subject: bool = False) -> list[Article]:
    """Free offers this subscriber has not been told about yet.

    Per-user on purpose: the article-level `free_offer_sent_at` only says the
    offer was announced to *somebody*, so a second chat would silently never
    hear about it. `push_logs(kind=free_offer)` carries the real per-subscriber
    ledger.

    Ordered by confidence rather than score: a 90%-confidence "X is free until
    Friday" is worth interrupting somebody for, a 45%-confidence maybe is not.
    """
    since = datetime.utcnow() - timedelta(days=within_days)
    stmt = (select(Article)
            .where(Article.is_free_offer.is_(True),
                   Article.is_archived.is_(False),
                   Article.created_at >= since))
    if user_id is None:
        stmt = stmt.where(Article.free_offer_sent_at.is_(None))
    else:
        already = (select(PushLog.article_id)
                   .where(PushLog.kind == "free_offer",
                          PushLog.user_id == user_id,
                          PushLog.article_id.isnot(None)))
        stmt = stmt.where(Article.id.not_in(already))
    rows = list(session.scalars(stmt.order_by(Article.created_at.desc()).limit(limit * 4)))
    picked = [row for row in rows
              if float((row.free_offer or {}).get("confidence") or 0) >= min_confidence
              and (not require_title_subject
                   or (row.free_offer or {}).get("subject_in_title", True))]
    return picked[:limit]


def mark_free_offers_sent(session: Session, article_ids: Sequence[int]) -> None:
    now = datetime.utcnow()
    for article_id in article_ids:
        article = session.get(Article, article_id)
        if article is not None:
            article.free_offer_sent_at = now
            article.is_sent = True
            article.sent_at = article.sent_at or now


def free_offer_tools(session: Session, *, days: int = 30, limit: int = 12) -> list[tuple[str, int]]:
    """Which tools appear most often - used for the /免费 buttons."""
    start = datetime.utcnow() - timedelta(days=max(1, days))
    rows = session.execute(
        select(Article.free_offer_tool, func.count(Article.id))
        .where(Article.is_free_offer.is_(True), Article.is_archived.is_(False),
               Article.filtered_out.is_(False), Article.published_at >= start,
               Article.free_offer_tool.isnot(None))
        .group_by(Article.free_offer_tool)
        .order_by(func.count(Article.id).desc())
        .limit(limit)
    ).all()
    return [(str(name), int(count)) for name, count in rows]


def recent_titles(session: Session, since: datetime, limit: int = 400) -> list[Article]:
    stmt = (
        select(Article)
        .where(Article.published_at >= since, Article.is_archived.is_(False))
        .order_by(Article.published_at.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def get_article(session: Session, article_id: int) -> Article | None:
    return session.get(Article, article_id)


def canonical_ids(session: Session, ids: Sequence[int]) -> list[int]:
    """Collapse an id list to one representative article per event."""
    out: list[int] = []
    seen_events: set[int] = set()
    for article in session.scalars(select(Article).where(Article.id.in_(ids))):
        key = article.event_id or article.id
        if key in seen_events:
            continue
        seen_events.add(key)
        out.append(article.id)
    return out


# ------------------------------------------------------------------ events
def get_or_create_event(session: Session, event_key: str, title: str, article: Article) -> Event:
    event = session.scalar(select(Event).where(Event.event_key == event_key))
    if event is None:
        event = Event(event_key=event_key, title=title, canonical_article_id=article.id,
                      member_count=1, source_names=[article.source_name],
                      final_score=article.final_score or 0)
        session.add(event)
        session.flush()
        return event
    names = list(event.source_names or [])
    if event.member_count == 1 and (article.final_score or 0) > (event.final_score or 0):
        event.canonical_article_id = article.id
        event.title = article.title
    event.member_count = (event.member_count or 1) + 1
    if article.source_name and article.source_name not in names:
        names.append(article.source_name)
        event.source_names = names
    event.final_score = max(event.final_score or 0, article.final_score or 0)
    event.last_seen_at = datetime.utcnow()
    return event


def event_for(session: Session, event_id: int) -> Event | None:
    return session.get(Event, event_id)


def event_members(session: Session, event_id: int) -> list[Article]:
    stmt = select(Article).where(Article.event_id == event_id).order_by(Article.published_at.asc())
    return list(session.scalars(stmt))


# ----------------------------------------------------------------- queries
def query_articles(
    session: Session,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    min_score: float | None = None,
    category: str | None = None,
    subcategory: str | None = None,
    source_name: str | None = None,
    search: str | None = None,
    require_processed: bool = True,
    limit: int = 20,
    offset: int = 0,
    order_by_score: bool = False,
    include_duplicates: bool = False,
    skip_sent: bool = False,
) -> list[Article]:
    stmt = select(Article).where(Article.is_archived.is_(False), Article.filtered_out.is_(False))
    if require_processed:
        stmt = stmt.where(Article.is_processed.is_(True))
    if skip_sent:
        # A briefing that overlaps the day's other briefing must not repeat items
        # it already delivered. `IS NOT 1` rather than `= 0`: rows written before
        # the column had a default carry NULL and must stay eligible.
        stmt = stmt.where(Article.is_sent.is_not(True))
    if since:
        stmt = stmt.where(Article.published_at >= since)
    if until:
        stmt = stmt.where(Article.published_at <= until)
    if min_score is not None:
        stmt = stmt.where(Article.final_score >= min_score)
    if category:
        stmt = stmt.where(Article.category == category)
    if subcategory:
        stmt = stmt.where(Article.subcategory == subcategory)
    if source_name:
        stmt = stmt.where(Article.source_name == source_name)
    if search:
        like = f"%{search.strip()}%"
        # The Chinese columns belong in here: upstream text is English, and every
        # row now carries a translated title/summary, but a query typed in the
        # language he actually reads matched nothing until 2026-09-26.
        stmt = stmt.where(
            or_(
                Article.title.ilike(like),
                Article.content.ilike(like),
                Article.summary.ilike(like),
                Article.why_it_matters.ilike(like),
                Article.title_zh.ilike(like),
                Article.summary_zh.ilike(like),
            )
        )
    order = [Article.final_score.desc(), Article.published_at.desc()] if order_by_score else [
        Article.published_at.desc(), Article.final_score.desc()
    ]
    stmt = stmt.order_by(*order).limit(limit).offset(offset)
    articles = list(session.scalars(stmt.options(selectinload(Article.tags))))
    if include_duplicates:
        return articles
    # De-duplicate events: keep the highest-ranked article of every event.
    seen: set[int] = set()
    out: list[Article] = []
    for article in articles:
        key = article.event_id or article.id
        if key in seen:
            continue
        seen.add(key)
        out.append(article)
    return out


def count_since(session: Session, since: datetime) -> int:
    return session.scalar(select(func.count(Article.id)).where(Article.published_at >= since)) or 0


def trending(session: Session, hours: int = 48, limit: int = 10) -> list[Article]:
    since = datetime.utcnow() - timedelta(hours=hours)
    return query_articles(session, since=since, order_by_score=True, limit=limit, min_score=1)


# ------------------------------------------------------------------- users
def ledger_user(session: Session, chat_id: int | None, *,
                timezone: str = "Asia/Shanghai") -> User | None:
    """The row the push ledger is keyed on - never a global fall-through.

    `pushes_since(user=None)` means "no user filter", which is the last thing a
    per-chat "already sent today" check or a daily cap should ever fall back to:
    one reader's pushes would silence another's briefing. A chat without a row
    yet gets one, so the ledger always belongs to somebody.
    """
    if chat_id is None:
        return None
    return get_or_create_user(session, chat_id, timezone=timezone)


def get_or_create_user(session: Session, chat_id: int, *, user_id: int | None = None,
                       display_name: str | None = None, timezone: str = "Asia/Shanghai",
                       language: str = "zh") -> User:
    user = session.scalar(select(User).where(User.telegram_chat_id == chat_id))
    if user is None:
        user = User(
            telegram_chat_id=chat_id, telegram_user_id=user_id, display_name=display_name,
            timezone=timezone, language=language,
        )
        session.add(user)
        session.flush()
        log.info("registered user chat_id=%s", chat_id)
    else:
        if display_name and user.display_name != display_name:
            user.display_name = display_name
        if user_id:
            user.telegram_user_id = user_id
    return user


def get_user(session: Session, chat_id: int) -> User | None:
    return session.scalar(select(User).where(User.telegram_chat_id == chat_id))


def set_interests(session: Session, user: User, items: list[dict[str, Any]], *, replace: bool = True) -> int:
    if replace:
        session.query(UserInterest).filter_by(user_id=user.id).delete()
        session.flush()
    added = 0
    for item in items:
        value = str(item.get("value", "")).strip()[:120]
        if not value:
            continue
        exists = session.scalar(
            select(UserInterest).where(
                UserInterest.user_id == user.id,
                UserInterest.type == item.get("type", "topic"),
                func.lower(UserInterest.value) == value.lower(),
            )
        )
        if exists:
            continue
        session.add(
            UserInterest(
                user_id=user.id,
                type=item.get("type", "topic"),
                value=value,
                weight=float(item.get("weight", 1.0) or 1.0),
            )
        )
        added += 1
    session.flush()
    return added


def interests_of(session: Session, user: User) -> list[UserInterest]:
    return list(session.scalars(select(UserInterest).where(UserInterest.user_id == user.id)))


def update_user(session: Session, user: User, **fields: Any) -> User:
    for key, value in fields.items():
        if hasattr(user, key) and value is not None:
            setattr(user, key, value)
    user.updated_at = datetime.utcnow()
    session.flush()
    return user


# --------------------------------------------------------------- push log
def record_push(session: Session, *, user: User | None = None, kind: str,
                article_id: int | None = None, event_id: int | None = None,
                user_id: int | None = None) -> PushLog:
    """`user_id` is accepted for callers that only hold the id (a fresh session)."""
    entry = PushLog(user_id=user.id if user else user_id, kind=kind,
                    article_id=article_id, event_id=event_id)
    session.add(entry)
    session.flush()
    return entry


def pushes_since(session: Session, *, user: User | None, kind: str, since: datetime) -> int:
    """How often this reader has been pushed `kind` since `since`.

    A missing user is not "count everybody": that default is how one reader's
    sends can silently satisfy another reader's daily cap or "already sent
    today" check. No caller wants the global count, so there is no way to ask
    for it.
    """
    if user is None:
        return 0
    stmt = (
        select(func.count(PushLog.id))
        .where(PushLog.kind == kind, PushLog.created_at >= since,
               PushLog.user_id == user.id)
    )
    return session.scalar(stmt) or 0


def last_push_of(session: Session, *, user: User | None, kind: str) -> PushLog | None:
    """This reader's most recent `kind` push; None if they have had none.

    Same rule as `pushes_since`: without a reader there is no cooldown to look
    up, and borrowing somebody else's last send would hold one chat's alerts
    back because another chat received one a minute ago.
    """
    if user is None:
        return None
    stmt = (
        select(PushLog)
        .where(PushLog.kind == kind, PushLog.user_id == user.id)
        .order_by(PushLog.created_at.desc())
        .limit(1)
    )
    return session.scalar(stmt)


def mark_sent(session: Session, article_ids: Sequence[int], *, breaking: bool = False) -> None:
    if not article_ids:
        return
    now = datetime.utcnow()
    values: dict[str, Any] = {"is_sent": True, "sent_at": now}
    if breaking:
        values["is_breaking"] = True
    session.execute(update(Article).where(Article.id.in_(list(article_ids))).values(**values))


def archive_older_than(session: Session, days: int) -> int:
    if days <= 0:
        return 0
    cutoff = datetime.utcnow() - timedelta(days=days)
    result = session.execute(
        update(Article).where(Article.published_at < cutoff, Article.is_archived.is_(False)).values(is_archived=True)
    )
    return result.rowcount or 0
