"""Engine + session management (SQLite, WAL, foreign keys on)."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_config
from app.database.models import Base
from app.logging_setup import get_logger

log = get_logger("db")

_engines: dict[str, Engine] = {}
_factories: dict[str, sessionmaker[Session]] = {}


def _sqlite_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_conn, _record):  # pragma: no cover - driver callback
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


def get_engine(url: str | None = None) -> Engine:
    url = url or get_config().settings.sqlalchemy_url
    if url not in _engines:
        if url.startswith("sqlite") and ":memory:" not in url:
            path = url.split("sqlite:///")[-1]
            if path:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            url,
            future=True,
            echo=False,
            pool_pre_ping=True,
            connect_args={"check_same_thread": False} if url.startswith("sqlite") else {},
        )
        if url.startswith("sqlite"):
            _sqlite_pragmas(engine)
        _engines[url] = engine
    return _engines[url]


def get_session_factory(url: str | None = None) -> sessionmaker[Session]:
    url = url or get_config().settings.sqlalchemy_url
    if url not in _factories:
        _factories[url] = sessionmaker(bind=get_engine(url), expire_on_commit=False, future=True)
    return _factories[url]


def init_db(url: str | None = None) -> None:
    """Create tables and indexes, then add columns introduced after first run.

    A personal deployment should survive a `git pull` without an alembic dance:
    new nullable columns are ALTERed in, everything else keeps create_all semantics.
    """
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    added = _ensure_columns(engine)
    if added:
        log.info("schema upgraded, added columns: %s", ", ".join(added))
    log.info("database ready at %s", engine.url)


# (table, column, sqlite type) added after the initial release.
_LATE_COLUMNS = (
    ("articles", "title_zh", "VARCHAR(512)"),
    ("articles", "summary_zh", "TEXT"),
    ("articles", "translated_by", "VARCHAR(16)"),
    ("articles", "is_free_offer", "BOOLEAN DEFAULT 0"),
    ("articles", "free_offer_tool", "VARCHAR(64)"),
    ("articles", "free_offer", "JSON"),
    ("articles", "free_offer_sent_at", "TIMESTAMP"),
)


def _ensure_columns(engine: Engine) -> list[str]:
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    added: list[str] = []
    for table, column, sqltype in _LATE_COLUMNS:
        if not inspector.has_table(table):  # pragma: no cover - fresh installs
            continue
        existing = {row["name"] for row in inspector.get_columns(table)}
        if column in existing:
            continue
        with engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sqltype}"))
        added.append(f"{table}.{column}")
    return added


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error, always logged."""
    session = get_session_factory(url)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        log.exception("database transaction failed")
        raise
    finally:
        session.close()


def new_session(settings: Settings | None = None) -> Session:
    return get_session_factory(settings.sqlalchemy_url if settings else None)()
