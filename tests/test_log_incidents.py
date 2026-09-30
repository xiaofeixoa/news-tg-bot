"""`scripts/log_incidents.py` 的测试：按"事件"归组，而不是按 Traceback 行数。

存在的理由是一个真实的误读：部署后我用 `grep -c Traceback` 核对"没有新增异常"，
`scheduler.log` 连着几天都是 4 —— 而那 4 块全部来自 2026-09-26 10:20:56 的同一次事故。
一个只增不减的数字回答不了"现在还好吗"，能回答的是每一类错误的**最后一次时刻**。
"""

from __future__ import annotations

import importlib.util
import pathlib
from datetime import datetime, timedelta

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "log_incidents", ROOT / "scripts" / "log_incidents.py")
li = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(li)

INCIDENT = """2026-09-26 10:20:56,763 ERROR   [news.scheduler] jobs.py:120 - job process failed: rolled back
Traceback (most recent call last):
  File "app/scheduler/jobs.py", line 120, in run
sqlite3.IntegrityError: UNIQUE constraint failed: article_tags.article_id, article_tags.tag_id
(Background on this error at: https://sqlalche.me/e/20/gkpj)
2026-09-26 10:20:56,770 ERROR   [news.scheduler] jobs.py:120 - job process failed: rolled back
Traceback (most recent call last):
  File "app/scheduler/jobs.py", line 120, in run
sqlalchemy.exc.PendingRollbackError: This Session's transaction has been rolled back
2026-09-26 10:20:56,780 INFO    [news.scheduler] jobs.py:495 - health check: 1200 article(s) in db
"""

REDDIT = """2026-09-30 23:05:00,100 WARNING [news.collector] base.py:412 - https://www.reddit.com/r/LocalLLaMA/hot/.rss?limit=50 -> HTTP 429
2026-09-30 23:35:00,100 WARNING [news.collector] base.py:412 - https://www.reddit.com/r/OpenAI/hot/.rss?limit=40 -> HTTP 429
2026-10-01 00:05:00,100 WARNING [news.collector] base.py:412 - https://x.com/feed -> HTTP 429
"""


def _write(tmp_path: pathlib.Path, **files) -> pathlib.Path:
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (logs / f"{name}.log").write_text(body, encoding="utf-8")
    return logs


def _groups(logs_dir: pathlib.Path, min_level: str = "WARNING"):
    return li.parse_logs(sorted(logs_dir.glob("*.log")), min_level=min_level)


def test_tracebacks_are_grouped_by_root_cause_with_first_and_last_seen(tmp_path):
    logs = _write(tmp_path, scheduler=INCIDENT)
    groups = _groups(logs)
    causes = {key.split(" ", 2)[-1] for key in groups}
    assert any("IntegrityError" in c for c in causes), causes
    assert any("PendingRollbackError" in c for c in causes), causes
    assert len(groups) == 2, dict(groups)          # INFO 那行不算
    for key, entry in groups.items():
        assert entry["count"] == 1
        assert entry["first"] == entry["last"] == datetime(2026, 9, 26, 10, 20, 56), key


def test_the_same_class_of_failure_collapses_into_one_event(tmp_path):
    """三次 reddit 429（不同 subreddit、不同 limit）是同一类问题，不是三类。"""
    logs = _write(tmp_path, collector=REDDIT)
    groups = _groups(logs)
    assert len(groups) == 1, dict(groups)
    entry = next(iter(groups.values()))
    assert entry["count"] == 3
    assert entry["first"] == datetime(2026, 9, 30, 23, 5)
    assert entry["last"] == datetime(2026, 10, 1, 0, 5)


def test_a_clean_window_says_so_instead_of_staying_silent(tmp_path):
    logs = _write(tmp_path, scheduler=INCIDENT)
    groups = _groups(logs)
    text = li.render(groups, logs_dir=logs, since_hours=6, now=datetime(2026, 9, 27, 0, 0))
    assert "没有任何新的这一类记录" in text, text
    assert "09-26 10:20:56" not in text, "历史事故不该出现在'最近 6 小时'里"
    recent = li.render(groups, logs_dir=logs, since_hours=6 * 24 * 30,
                       now=datetime(2026, 9, 27, 0, 0))
    assert "IntegrityError" in recent and "次数 1" in recent, recent


def test_info_lines_are_only_counted_when_asked(tmp_path):
    logs = _write(tmp_path, scheduler=INCIDENT)
    quiet = _groups(logs)
    loud = _groups(logs, min_level="INFO")
    assert all(not key.startswith("INFO") for key in quiet), list(quiet)
    assert any(key.startswith("INFO") for key in loud), list(loud)


def test_a_trailing_traceback_with_no_next_line_is_still_reported(tmp_path):
    """块尾没有下一条日志时也不能丢：那正是"进程当场死了"的形状。"""
    logs = _write(tmp_path, app='2026-09-30 08:00:00,000 ERROR   [news] main.py:9 - boom\n'
                                 "Traceback (most recent call last):\n"
                                 '  File "app/main.py", line 9, in <module>\n'
                                 "RuntimeError: socket closed\n")
    groups = _groups(logs)
    assert len(groups) == 1, dict(groups)
    assert "RuntimeError" in next(iter(groups))


def test_a_missing_log_directory_is_reported_not_crashed(tmp_path, capsys):
    assert li.main(["--logs", str(tmp_path / "nope")]) == 1
    assert "找不到日志目录" in capsys.readouterr().out


# 真机量过的形状（2026-10-01, anr-jump）：同一批日志、同一句 `--since 6`，
# 进程钟是北京时间时列出 15 类（最早 5.25 小时前，正确），换成 UTC 就变成 21 类、
# 最早 12.6 小时前——问"最近 6 小时"答出 12.6 小时。
WINDOW = """2026-10-01 00:05:00,100 WARNING [news.collector] base.py:41 - recent outage A: HTTP 429
2026-09-30 16:39:14,100 WARNING [news.scheduler] jobs.py:77 - old trouble B: something else
"""


def _window_groups(tmp_path):
    logs = _write(tmp_path, collector=WINDOW)
    return logs, _groups(logs)


def test_the_window_is_measured_on_the_log_clock_even_for_a_utc_caller(tmp_path):
    """调用者的钟不能决定窗口：日志里的时刻是服务写下的墙上时间。"""
    from datetime import timezone

    logs, groups = _window_groups(tmp_path)
    beijing_now = datetime(2026, 10, 1, 5, 12)              # 日志时区的此刻
    same_in_utc = datetime(2026, 9, 30, 21, 12, tzinfo=timezone.utc)   # 同一个瞬间

    text = li.render(groups, logs_dir=logs, since_hours=6, now=beijing_now,
                     tz_name="Asia/Shanghai")
    utc_caller = li.render(groups, logs_dir=logs, since_hours=6, now=same_in_utc,
                           tz_name="Asia/Shanghai")
    assert "recent outage A" in text and "old trouble B" not in text, text
    assert utc_caller == text, "换个机器跑，答案不该变"
    # 旧行为正是把 UTC 的钟当北京时间用：窗口自己变成 6+8=14 小时
    widened = li.render(groups, logs_dir=logs, since_hours=6,
                        now=beijing_now - timedelta(hours=8), tz_name="Asia/Shanghai")
    assert "old trouble B" in widened, "去掉时区换算后，这条必须重新被错误地收进来"


def test_the_header_reports_the_windowed_count_not_just_the_total(tmp_path):
    """`命中 99 类` 配 15 条列表：那个 99 根本不是这句问题的答案。"""
    logs, groups = _window_groups(tmp_path)
    text = li.render(groups, logs_dir=logs, since_hours=6,
                     now=datetime(2026, 10, 1, 5, 12), tz_name="Asia/Shanghai")
    assert "日志里共 2 类" in text, text
    assert "最近 6 小时内还在出现的 1 类" in text, text
    assert "窗口按 Asia/Shanghai 的 10-01 05:12 起算" in text, text


def test_since_wants_hours_and_says_so_in_chinese(tmp_path, capsys):
    """我自己就喂过 `--since 21:02`，收到的回答是 `invalid float value`。"""
    import argparse

    logs = _write(tmp_path, scheduler=WINDOW)
    assert li._hours("6") == 6.0
    assert li._hours("0.5") == 0.5
    for bad in ("21:02", "昨天", "0", "-3"):
        with pytest.raises(argparse.ArgumentTypeError) as excinfo:
            li._hours(bad)
        assert "小时" in str(excinfo.value), bad
    with pytest.raises(SystemExit) as exitinfo:
        li.main(["--logs", str(tmp_path), "--since", "21:02"])
    assert exitinfo.value.code == 2
    err = capsys.readouterr().err
    # 只认我这句话：`[--since 小时]` 那段的 metavar 里也有"小时"，光看它会被骗过
    assert "不是时刻" in err, err
    assert "invalid float value" not in err, err
    assert li.main(["--logs", str(logs), "--since", "6"]) == 0
    assert "窗口按 Asia/Shanghai" in capsys.readouterr().out


def test_the_log_zone_matches_the_clock_the_service_writes_with():
    """unit 里的 `TZ=` 与 settings.timezone 必须同源，否则这个窗口又是猜的。"""
    import re as _re

    from app.config import get_config

    zone = get_config().settings.timezone
    units = sorted((ROOT / "deploy").glob("*.service"))
    assert units, "deploy/ 里找不到 systemd 单元"
    for unit in units:
        stamps = _re.findall(r"^Environment=TZ=(\S+)$", unit.read_text(encoding="utf-8"), _re.M)
        assert stamps and stamps[0] == zone, (unit.name, stamps, zone)

