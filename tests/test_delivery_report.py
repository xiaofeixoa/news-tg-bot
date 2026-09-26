"""投递对账脚本的判断分支：它要在早上第一眼就能读对。

`scripts/delivery_report.py` 存在的理由是"今天没有 morning 这一行"这句话有四种
完全不同的意思（没到点 / 他自己关了 / 按了 /pause / 真的该发而没有）。所以每个
分支都单独钉一条，全部用冻住的 `now` 问。
"""

from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path


from app.config import get_config
from app.database import repository as repo
from app.database.database import session_scope

_spec = importlib.util.spec_from_file_location(
    "delivery_report", Path(__file__).resolve().parent.parent / "scripts" / "delivery_report.py")
report = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(report)

CHAT = 111111111
TZ = "Asia/Shanghai"


def subscriber(**fields) -> None:
    with session_scope() as s:
        user = repo.get_or_create_user(s, CHAT, timezone=TZ)
        if fields:
            repo.update_user(s, user, **fields)
        s.commit()


def push(created_at: datetime, kind: str = "morning") -> None:
    """记一笔账，然后把它的 UTC 时间挪到我们要问的那一刻。"""
    with session_scope() as s:
        entry = repo.record_push(s, user=repo.get_user(s, CHAT), kind=kind)
        entry.created_at = created_at
        s.commit()


def line(kind: str, *, now: datetime) -> str:
    text = report.render(config=get_config(), hours=48, kinds=(kind,), now=now)
    return next(l for l in text if l.strip().startswith(kind))


# 09-27 09:00 上海 = 01:00 UTC；09-27 06:00 上海 = 09-26 22:00 UTC
LATE_MORNING = datetime(2026, 9, 27, 1, 0, 0)
BEFORE_SLOT = datetime(2026, 9, 26, 22, 0, 0)


def test_a_morning_row_from_earlier_today_reads_as_delivered():
    subscriber()
    push(datetime(2026, 9, 27, 0, 30, 0))       # 本地 08:30 今天
    assert "✅ 今天已记账" in line("morning", now=LATE_MORNING)


def test_the_window_count_and_the_ledger_row_are_both_shown():
    subscriber()
    push(datetime(2026, 9, 27, 0, 30, 0))
    text = line("morning", now=LATE_MORNING)
    assert "窗口内 1 次" in text, "计数与最后一条都要在，不然分不清重复发与漏发"


def test_a_briefing_that_should_have_gone_out_but_did_not_is_the_warning():
    subscriber()
    push(datetime(2026, 9, 26, 0, 30, 0))       # 本地 08:30 昨天
    text = line("morning", now=LATE_MORNING)
    assert "⚠️" in text and "已过计划时间" in text and "计划 08:00" in text


def test_before_the_slot_is_not_an_incident():
    subscriber()
    push(datetime(2026, 9, 25, 0, 30, 0))
    text = line("morning", now=BEFORE_SLOT)
    assert "⏳ 今天还没到点" in text and "⚠" not in text


def test_pause_and_his_own_switch_are_reported_as_the_reason():
    subscriber(paused=True)
    assert "⏸ 没发" in line("morning", now=LATE_MORNING)
    subscriber(paused=False, daily_enabled=False)
    text = line("morning", now=LATE_MORNING)
    assert "🔕 没发" in text and "/设置" in text, "v1.29 的开关要能被认出来"


def test_a_subscriber_with_no_ledger_row_at_all_says_so():
    subscriber()
    text = line("morning", now=LATE_MORNING)
    assert "❓ 库里从来没有 morning 的账本行" in text


def test_evening_uses_its_own_slot_and_switch():
    subscriber(evening_time="21:00", evening_enabled=False)
    text = line("evening", now=LATE_MORNING)
    assert "计划 21:00" in text and "🔕" in text


def test_the_log_tail_only_carries_lines_about_this_chat(tmp_path):
    log = tmp_path / "telegram.log"
    log.write_text(
        "2026-09-27 08:01:51,100 WARNING [news.telegram] sender.py:70 - telegram network error "
        "(1/3) chat_id=111111111: boom\n"
        "2026-09-27 08:02:00,000 INFO [news.telegram] sender.py:60 - digest sent chat_id=999\n"
        "2026-09-27 08:03:00,000 ERROR [news.telegram] sender.py:57 - digest for chat_id=111111111 "
        "stopped at message 2/2; not marking it delivered\n",
        encoding="utf-8")
    hits = report.log_tails(log, CHAT, limit=5)
    assert len(hits) == 2 and all("chat_id=111111111" in h for h in hits)
    assert "stopped at message 2/2" in hits[-1], "v1.28 那半份简报的日志必须被捞得起来"
    assert report.log_tails(tmp_path / "missing.log", CHAT) == []


def test_render_names_both_clock_conventions_once():
    subscriber()
    text = report.render(config=get_config(), kinds=("morning",), now=LATE_MORNING)
    assert any("naive UTC" in l for l in text), "两个时间口径不写在脸上就会被读错 8 小时"
