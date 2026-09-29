"""批量核对工具自身的行为：它写库，所以得有测试，不能只在生产上第一次跑。"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import get_config
from app.database import repository as repo
from app.database.database import session_scope
from app.database.models import Article, User
from app.processing.normalize import build_article

SPEC = importlib.util.spec_from_file_location(
    "reconcile_db", Path("scripts/reconcile_db.py"))
reconcile = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(reconcile)


def _article(session, title: str, url: str, *, source: str = "TechCrunch AI",
             meta: dict | None = None, filtered: bool = False) -> int:
    data = build_article(title=title, url=url, source_name=source,
                         content=f"{title}. The company says the new model improves reasoning.",
                         published_at=datetime.utcnow() - timedelta(hours=2), meta=meta or {})
    row = repo.save_article(session, data)
    row.filtered_out = filtered
    row.is_processed = True
    session.commit()
    return int(row.id)


@pytest.mark.parametrize("raw", ["0", 0, 0.0])
def test_zero_stars_is_not_read_as_missing(raw):
    """`meta.stars` 就是可能等于 0，而 `0 or -1` 会把它读成"没有这个键"。"""
    assert reconcile._stars_of(Article(meta={"stars": raw})) == 0
    assert reconcile._stars_of(Article(meta={})) is None
    assert reconcile._stars_of(Article(meta={"stars": "abc"})) is None


def test_reconcile_apply_fixes_the_four_ledgers(session):
    from app.database.models import PushLog  # noqa: F401  (ghost 步骤会查账本表)

    cfg = get_config()
    revived = _article(session, "Modulate raises $25M for its voice models and analysis suite",
                       "https://example.org/regate", filtered=True)
    spam_id = _article(session, "someone/zero-star-repo (0 stars)", "https://example.org/spam",
                       source="GitHub Trending", meta={"stars": 0})
    kept = _article(session, "someone/real-repo (8,000 stars)", "https://example.org/kept",
                    source="GitHub Trending", meta={"stars": 8000})
    with session_scope() as s:
        ghost_user = User(telegram_chat_id=reconcile.PHANTOM_CHAT, display_name="ghost")
        s.add(ghost_user)
        row = s.get(Article, kept)
        row.processed_at = None
        s.commit()
        ghost_id = ghost_user.id

    assert reconcile.main(["--apply"]) == 0

    with session_scope() as s:
        r = s.get(Article, revived)
        assert r.filtered_out is False and r.is_processed is False, "词表放行后要让处理轮重看"
        assert (r.meta or {}).get("regated"), "要留下为什么被放回"
        assert s.get(Article, spam_id).filtered_out is True, "0 星的 Trending 行要标掉"
        assert s.get(Article, kept).filtered_out is False, "有星的不能顺手标掉"
        assert s.get(Article, kept).processed_at is not None, "processed_at 要补上"
        assert (s.get(Article, kept).meta or {}).get("processed_at_backfilled"), "补的要能认出来"
        assert s.get(User, ghost_id) is None, "假订阅者该删掉"


def test_dry_run_writes_nothing(session):
    spam_id = _article(session, "someone/other-zero-star (0 stars)", "https://example.org/spam2",
                       source="GitHub Trending", meta={"stars": 0})
    assert reconcile.main([]) == 0
    with session_scope() as s:
        s.expire_all()
        assert s.get(Article, spam_id).filtered_out is False, "默认必须是干跑"
