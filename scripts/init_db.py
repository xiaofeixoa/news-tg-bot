"""Create the SQLite schema and register the configured sources.

    python scripts/init_db.py               # create tables + seed sources
    python scripts/init_db.py --reset       # drop and recreate (destroys news!)
    python scripts/init_db.py --seed-user 123456789
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_config                       # noqa: E402
from app.database import repository as repo             # noqa: E402
from app.database.database import get_engine, init_db, session_scope  # noqa: E402
from app.database.models import Base                    # noqa: E402
from app.logging_setup import setup_logging             # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="drop all tables first")
    parser.add_argument("--seed-user", type=int, help="register a Telegram chat id")
    args = parser.parse_args(argv)

    config = get_config()
    setup_logging(config.settings.log_path, level=config.settings.log_level)
    engine = get_engine()

    if args.reset:
        print(f"dropping every table in {config.settings.sqlalchemy_url}")
        Base.metadata.drop_all(engine)
    init_db()

    with session_scope() as session:
        for source in config.sources:
            repo.get_or_create_source(
                session,
                name=source.get("name"),
                type_=source.get("type", "rss"),
                url=source.get("url") or source.get("rss_url"),
                quality=source.get("quality", "C"),
                category=source.get("category"),
            )
            registered = repo.get_or_create_source(session, name=source.get("name"))
            registered.enabled = bool(source.get("enabled", True))
        if args.seed_user:
            user = repo.get_or_create_user(session, args.seed_user,
                                           timezone=config.settings.timezone)
            print(f"registered chat_id={user.telegram_chat_id} daily={user.daily_time} "
                  f"evening={user.evening_time}")
        sources = repo.all_sources(session)
        session.commit()

    enabled = [s for s in sources if s.enabled]
    print(f"database ready : {config.settings.sqlalchemy_url}")
    print(f"tables         : {', '.join(sorted(Base.metadata.tables))}")
    print(f"sources        : {len(sources)} registered, {len(enabled)} enabled")
    by_type: dict[str, int] = {}
    for source in enabled:
        by_type[source.type] = by_type.get(source.type, 0) + 1
    for type_, count in sorted(by_type.items()):
        print(f"  {type_:<12} {count}")
    if not config.settings.telegram_bot_token:
        print("reminder: set TELEGRAM_BOT_TOKEN and ALLOWED_CHAT_IDS in .env")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
