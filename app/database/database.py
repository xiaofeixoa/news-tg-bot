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
_fresh_files: set[str] = set()          # urls whose sqlite file did not exist when opened


def sqlite_file(url: str | None = None) -> str | None:
    """The file a sqlite URL points at; None for in-memory and non-sqlite URLs.

    Splits on the *first* marker on purpose: `[-1]` would take the last segment of a
    two-value URL and return a clean-looking path, hiding exactly the case the check
    below exists for.
    """
    text = str(url or get_config().settings.sqlalchemy_url)
    if not text.startswith("sqlite:///") or ":memory:" in text:
        return None
    return text.split("sqlite:///", 1)[1] or None


def _check_url(url: str, path: str) -> None:
    """Reject a DSN that carries more than one value.

    Measured 2026-09-30: `grep '^DATABASE_URL=' env | cut -d= -f2-` on a file where
    the key is defined twice yields two lines, and passing that to SQLite does not
    fail - `get_engine` mkdirs the parent, so the app happily builds a directory
    forest inside `data/` and opens a brand-new empty database. Failing here is the
    difference between "the bot is up and says nothing" and a message naming the
    file to fix.
    """
    if "\n" in path or "\r" in path or "sqlite:" in path:
        raise ValueError(
            f"DATABASE_URL 看起来被拼在了一起（{url[:80]!r}）。"
            "如果它来自 env 文件，请确认同一个键没有被定义两次："
            "grep -c '^DATABASE_URL=' /etc/ai-news-radar/env")


def database_is_fresh(url: str | None = None) -> bool:
    """True when this process created the sqlite file it is using."""
    return (url or get_config().settings.sqlalchemy_url) in _fresh_files


def database_size_kb(url: str | None = None) -> int:
    """Size of the sqlite file, or -1 when it cannot be read.

    Four kilobytes means "an empty database we just invented"; the number is what
    makes a silent data-loss boot readable from one line.
    """
    path = sqlite_file(url)
    if not path:
        return -1
    try:
        return int(Path(path).stat().st_size / 1024)
    except OSError:
        return -1


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
        path = sqlite_file(url)
        if path:
            _check_url(url, path)
            if not Path(path).exists():
                _fresh_files.add(url)
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
    key = url or get_config().settings.sqlalchemy_url
    engine = get_engine(key)
    Base.metadata.create_all(engine)
    added = _ensure_columns(engine)
    if added:
        log.info("schema upgraded, added columns: %s", ", ".join(added))
    # The file's own size is the difference between "our database" and "a database
    # we just invented": an empty one answers every question with 0 and looks like a
    # quiet news day. `anr-jump`'s first boot after the 09-27 snapshot rollback - when
    # the whole install, news included, had been restored away - logged this next line
    # at INFO, indistinguishable from the 46 healthy boots around it.
    path = sqlite_file(key)
    size_kb = database_size_kb(key)
    if key in _fresh_files:
        log.warning("数据库是这次启动才新建的：%s（%s KB）。如果这不是第一次安装，那说明 "
                    "DATABASE_URL 指到了一个不存在的路径（或被回滚过）：库里 0 条新闻，"
                    "简报会安静地什么都不发，旧数据如果在别处并不在这里。", path, size_kb)
    else:
        log.info("database ready at %s (%s KB)", key, size_kb)


# (table, column, sqlite type) added after the initial release.
_LATE_COLUMNS = (
    ("articles", "title_zh", "VARCHAR(512)"),
    ("articles", "summary_zh", "TEXT"),
    ("articles", "translated_by", "VARCHAR(16)"),
    ("articles", "is_free_offer", "BOOLEAN DEFAULT 0"),
    ("articles", "free_offer_tool", "VARCHAR(64)"),
    ("articles", "free_offer", "JSON"),
    ("articles", "free_offer_sent_at", "TIMESTAMP"),
    ("articles", "processed_at", "TIMESTAMP"),
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
