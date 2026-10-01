"""磁盘要在跌破告警线**之前**说话（v1.97）。

真机 2026-10-01：`anr-vps` 剩 1623MB、`disk_delta_mb=-191`/`disk_span_hours=12.9`
≈ **-355MB/天**，照此 **1.7 天**跌破他自己设的 `alerts.min_free_mb: 1024`，
而当时面板那一行还是普通的 💾。写满在 SQLite 上不是"变慢"，是采集静默停住。
"""

from __future__ import annotations

from app.config import AppConfig, get_config
from app.services import format as fmt

REAL = {"disk_free_mb": 1623, "disk_delta_mb": -191, "disk_span_hours": 12.9}


def _config(threshold_mb: int = 1024) -> AppConfig:
    return AppConfig(settings=get_config().settings, raw={"alerts": {"min_free_mb": threshold_mb}},
                     sources=[])


def test_a_disk_that_is_falling_towards_the_line_warns_in_advance():
    days = fmt.disk_crossing_in_days(REAL, 1024)
    assert days and 1.0 < days < 3.0, days
    line = fmt.disk_line(REAL, config=_config())
    assert "⚠️" in line and "天后跌破" in line, line
    assert "1.7" in line, line
    assert "静默停住" in line, line            # 说清后果，不是只喊一句小心


def test_a_disk_with_plenty_of_runway_stays_quiet():
    roomy = {"disk_free_mb": 26536, "disk_delta_mb": -10, "disk_span_hours": 12.9}
    line = fmt.disk_line(roomy, config=_config())
    assert "⚠️" not in line and "💾" in line, line
    assert "跌破" not in line, line


def test_a_disk_that_is_not_shrinking_gets_no_countdown():
    """回升的那天（logrotate 放回 1GB）不该出现"几天后跌破"。"""
    rising = {"disk_free_mb": 1623, "disk_delta_mb": 1049, "disk_span_hours": 12.9}
    assert fmt.disk_crossing_in_days(rising, 1024) is None
    assert "跌破" not in fmt.disk_line(rising, config=_config())


def test_no_countdown_without_enough_history():
    """样本不够时承认不知道，而不是编一个天数出来。"""
    blind = {"disk_free_mb": 1100, "disk_delta_mb": None, "disk_span_hours": None}
    assert fmt.disk_crossing_in_days(blind, 1024) is None
    line = fmt.disk_line(blind, config=_config())
    assert "跌破" not in line, line


def test_already_below_the_line_keeps_the_original_wording():
    """提前预警不能把"已经越线"那条更重的话挤掉。"""
    line = fmt.disk_line({"disk_free_mb": 800, "disk_delta_mb": -191,
                          "disk_span_hours": 12.9}, config=_config())
    assert "低于 1GB 告警线" in line and "写不进数据库" in line, line
