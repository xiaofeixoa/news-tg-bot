"""每天上限与简报的"今天"必须落在读者的日界上，不是服务器的 UTC 日界。

线上实测（2026-09-30）：#1497 在北京时间 05:22–07:52 被 `daily cap reached (5/5)` 连拒
16 次，而他自己的那个北京日那时只发过 2 条；08:02:40 UTC 日界一翻，它立刻补发成功。
日志里 26 条上限拒绝全落在 05:00–08:00 北京这个"两种日界说法不一"的带里。
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.config import local_day_start, quiet_window
from app.database import repository as repo
from app.database import session_scope
from app.database.models import Article, PushLog
from app.processing.normalize import build_article
from app.services.digest import DigestService

# 2026-09-29 21:22 UTC = 2026-09-30 05:22 北京 = 09-29 14:52 洛杉矶
INSTANT = datetime(2026, 9, 29, 21, 22, tzinfo=timezone.utc)
NAIVE = INSTANT.replace(tzinfo=None)


def _freeze(monkeypatch, moment: datetime) -> None:
    monkeypatch.setattr("app.config._now_utc", lambda: moment.replace(tzinfo=None))


def _qualified_row(session, title: str, url: str) -> int:
    stored = repo.save_article(session, build_article(
        title=title, url=url, source_name="OpenAI", source_type="rss",
        content=title + ". The company says it works. " * 6))
    row = session.get(Article, stored.id)
    row.final_score = 96
    row.source_quality = 95
    row.is_processed = True
    row.category = "AI Models"
    session.commit()
    return int(stored.id)


def _reader(chat: int, zone_name: str) -> int:
    with session_scope() as s:
        user = repo.get_or_create_user(s, chat)
        user.timezone = zone_name
        s.commit()
        return int(user.id)


def _pushes(user_id: int, kind: str, stamps: list[datetime]) -> None:
    with session_scope() as s:
        for i, stamp in enumerate(stamps):
            entry = PushLog(user_id=user_id, kind=kind, created_at=stamp)
            s.add(entry)
        s.commit()


# ------------------------------------------------------------------ the helper
def test_the_day_starts_on_the_readers_clock():
    got = {z: local_day_start(z, at=NAIVE) for z in
           ("Asia/Shanghai", "UTC", "Asia/Kolkata", "America/Los_Angeles")}
    assert got["Asia/Shanghai"] == datetime(2026, 9, 29, 16, 0), "北京日的 00:00 = 前一天 16:00 UTC"
    assert got["UTC"] == datetime(2026, 9, 29, 0, 0)
    # 09-29 21:22 UTC = 09-30 02:52 IST，所以他的"今天"从 09-29 18:30 UTC 起
    assert got["Asia/Kolkata"] == datetime(2026, 9, 29, 18, 30), "半小时偏移的区也得算对"
    assert got["America/Los_Angeles"] == datetime(2026, 9, 29, 7, 0)
    assert all(value.tzinfo is None for value in got.values()), "账本用 naive UTC"


def test_every_zone_gets_a_day_that_contains_this_instant():
    for zone_name in ("Asia/Shanghai", "UTC", "Pacific/Kiritimati", "Pacific/Midway",
                      "Asia/Kathmandu", "Australia/Lord_Howe"):
        start = local_day_start(zone_name, at=NAIVE)
        assert timedelta(0) <= NAIVE - start < timedelta(days=1), (zone_name, NAIVE - start)
        local = start.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(zone_name))
        assert (local.hour, local.minute) == (0, 0), zone_name


# ------------------------------------------------------------------ the caps
def test_yesterdays_five_alerts_do_not_fill_today_in_the_readers_zone(session, monkeypatch):
    """UTC 日界把 08:00 之前的早上算进"昨天"——那正是 #1497 被卡住的一小时。"""
    chat = 1990000001
    user_id = _reader(chat, "Asia/Shanghai")
    # 同一个 UTC 日里 5 条；其中 16:05 与 20:02 那两条已经跨进他的 09-30 北京日
    _pushes(user_id, "breaking", [datetime(2026, 9, 29, 12, 39), datetime(2026, 9, 29, 14, 8),
                                  datetime(2026, 9, 29, 16, 5), datetime(2026, 9, 29, 20, 2),
                                  datetime(2026, 9, 29, 21, 0)])
    art = _qualified_row(session, "OpenAI unveils the next model", "https://openai.com/day-new")
    _freeze(monkeypatch, INSTANT)

    service = DigestService()
    ok, why = service.can_send_breaking(chat, article_id=art, respect_cooldown=False)
    assert ok, f"他自己的北京日 09-30 才 2 条，该发：{why}"

    _reader(chat, "UTC")
    ok2, why2 = service.can_send_breaking(chat, article_id=art, respect_cooldown=False)
    assert not ok2 and "daily cap" in why2, f"同一瞬间 UTC 读者那天确实满了：{why2}"


@pytest.mark.parametrize("zone_name,expected_allowed", [("Asia/Shanghai", True), ("UTC", False)])
def test_the_promo_cap_rolls_on_the_same_day_as_the_breaking_cap(session, monkeypatch, zone_name,
                                                                 expected_allowed):
    """限免的"每人每天 4 条"以前也按 UTC 日算，与突发不是一套。"""
    from app.services.free_alerts import FreeAlertService

    chat = 1990000002
    user_id = _reader(chat, zone_name)
    # 四条都在 UTC 09-29 这一天内，也就是北京 09-29 白天：对上海读者是"昨天"
    _pushes(user_id, "free_offer", [datetime(2026, 9, 29, 1, 0), datetime(2026, 9, 29, 3, 0),
                                    datetime(2026, 9, 29, 5, 0), datetime(2026, 9, 29, 7, 0)])
    _freeze(monkeypatch, INSTANT)
    service = FreeAlertService()
    service._sender = object()
    ok, reason = service._may_send(chat)
    assert ok is expected_allowed, (zone_name, reason)
    if not expected_allowed:
        assert "daily cap" in reason, reason


def test_the_quiet_window_and_the_cap_use_one_clock(config, monkeypatch):
    """同一瞬间：静默窗口与日界必须都认读者的钟，否则两条规则互相拉扯。"""
    _freeze(monkeypatch, INSTANT)
    cfg = dataclasses.replace(
        config, raw={**config.raw, "breaking": {**config.raw.get("breaking", {}),
                                               "quiet_hours": "23:00-07:00"}})
    # 北京 05:22：静默中，日界是 09-30 的 00:00（=09-29 16:00 UTC）
    assert quiet_window(cfg, "Asia/Shanghai") and "静默" in quiet_window(cfg, "Asia/Shanghai")
    assert local_day_start("Asia/Shanghai") == datetime(2026, 9, 29, 16, 0)
    # UTC 14:22：既不静默，日界也是"今天 00:00 UTC"
    assert quiet_window(cfg, "UTC") == ""
    assert local_day_start("UTC") == datetime(2026, 9, 29, 0, 0)
