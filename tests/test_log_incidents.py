"""`scripts/log_incidents.py` 的测试：按"事件"归组，而不是按 Traceback 行数。

存在的理由是一个真实的误读：部署后我用 `grep -c Traceback` 核对"没有新增异常"，
`scheduler.log` 连着几天都是 4 —— 而那 4 块全部来自 2026-09-26 10:20:56 的同一次事故。
一个只增不减的数字回答不了"现在还好吗"，能回答的是每一类错误的**最后一次时刻**。
"""

from __future__ import annotations

import importlib.util
import pathlib
from datetime import datetime

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
