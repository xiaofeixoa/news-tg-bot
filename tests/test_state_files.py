"""状态文件的写入只有一条路（v1.89）。

三份"重启之后接着用"的记忆——GitHub 配额账本、304 的 ETag 缓存、限免公告台账——
原本用 `write_text` 直接覆盖正式文件：磁盘写满一次或进程崩在半路，会把上一份好数据
一起毁掉，而它们的读取侧一律"读不到就当没有"。这个文件钉两件事：
写失败的中间过程不许碰到正式文件；以及**没有第四份实现**。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.config import atomic_write_json

APP_ROOT = Path(__file__).resolve().parent.parent / "app"


def _write_half_then_fail(monkeypatch, keep: int = 12) -> None:
    """模拟磁盘写满：真的打开文件、真的写进去前 `keep` 个字符，然后 ENOSPC。

    第一版我只是 `raise`、一个字节都不写，于是 M1（覆盖式写正式文件）和 M2（失败后不清理
    `.tmp`）都活了——"只抛不写"的假让两种写法看起来一模一样。写半个才分得出高下。
    """

    def fake(self, data, *args, **kwargs):
        with open(self, "w", encoding="utf-8") as handle:
            handle.write(str(data)[:keep])
            handle.flush()
            os.fsync(handle.fileno())
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_text", fake)


def test_a_failed_write_leaves_the_previous_good_state_intact(tmp_path, monkeypatch):
    path = tmp_path / "github_rate.json"
    atomic_write_json(path, {"remaining": 3, "reset": 123.0})
    assert json.loads(path.read_text(encoding="utf-8")) == {"remaining": 3, "reset": 123.0}

    _write_half_then_fail(monkeypatch)
    with pytest.raises(OSError):
        atomic_write_json(path, {"remaining": 0, "reset": 999.0})
    # 旧数据必须还在：读取侧靠它决定"这一小时还能不能再问 GitHub"
    assert json.loads(path.read_text(encoding="utf-8")) == {"remaining": 3, "reset": 123.0}, \
        "半个新文件盖掉了整份旧文件——这正是 write_text 直接覆盖正式文件的后果"


def test_a_failed_write_does_not_leave_a_tmp_file_behind(tmp_path, monkeypatch):
    path = tmp_path / "free_models.json"
    atomic_write_json(path, {"announced": ["旧台账"]})

    _write_half_then_fail(monkeypatch)
    with pytest.raises(OSError):
        atomic_write_json(path, {"announced": ["新"]})
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == [path.name], f"残留 {left}：运维会当半个 .tmp 是真状态读"


def test_the_helper_keeps_the_shape_each_reader_expects(tmp_path):
    """迁移到同一个出口不能改变任何一个读取侧看到的形状：列表、缩进、中文原样。"""
    history = tmp_path / "disk_history.json"
    atomic_write_json(history, [[1800000000, 1450], [1800000600, 1449]])
    assert json.loads(history.read_text(encoding="utf-8")) == [[1800000000, 1450],
                                                                [1800000600, 1449]]
    state = tmp_path / "free_models.json"
    atomic_write_json(state, {"announced": ["千问 免费"]}, indent=1)
    text = state.read_text(encoding="utf-8")
    assert "千问 免费" in text, "ensure_ascii 不许被改回去：那是台账里的中文模型名"
    assert "\n " in text, "free_models.json 是运维会直接看的文件，保留缩进"


def test_no_state_writer_bypasses_the_atomic_helper():
    """第四份实现就是下一个 bug：全仓只允许 `atomic_write_json` 往状态文件落盘。"""
    offenders = []
    for py in sorted(APP_ROOT.rglob("*.py")):
        if py.name == "config.py":
            continue
        for number, line in enumerate(py.read_text(encoding="utf-8").splitlines(), start=1):
            if ".write_text(" in line:
                offenders.append(f"{py.relative_to(APP_ROOT.parent)}:{number}: {line.strip()}")
    assert offenders == [], "状态文件请走 app.config.atomic_write_json：" + "; ".join(offenders)
