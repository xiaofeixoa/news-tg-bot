"""门槛按钮的上限必须够得着（v1.94）。

真机 2026-10-01 只读探针：全库 `final_score >= 80` 是 **0 行**，近 7 天最高 78.0，
近 24 小时最高也是 78.0；而 `config/settings.yaml` 里 2026-09-27 的注释已经写着
"GitHub Trending 有 41 条并列 78.0"。旧代码把 `🔼 提高门槛` clamp 到硬写的 90，
于是他能一路点到 80 / 85 / 90——那三档的实际含义是"明天没有简报"，而且面板不会告诉他。
"""

from __future__ import annotations

import pathlib

from app.config import get_config
from app.services.news import NewsService

REAL_MAX = 78.0   # 真机实测：规则模式能给到的最高分


def test_the_ceiling_comes_from_config_not_from_a_literal():
    ceiling = NewsService(get_config()).score_ceiling()
    declared = float(get_config().get("digest.score_ceiling", 0) or 0)
    assert declared > 0, "上限必须写在 settings.yaml 里，让它是一个可以讨论的数字"
    assert ceiling == declared, (ceiling, declared)


def test_the_ceiling_is_not_above_what_the_engine_can_produce():
    """一个他永远够不到的档位在界面上不存在才是安全的。"""
    assert NewsService(get_config()).score_ceiling() <= REAL_MAX


def test_the_handler_no_longer_clamps_to_the_unreachable_90():
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "app" / "bot" / "handlers" / "settings.py").read_text(encoding="utf-8")
    assert "min(90.0" not in src, "代码里不该再出现那个够不到的上限"
    assert "news.score_ceiling()" in src, "clamp 必须读同一个定义"


def test_a_press_at_the_ceiling_says_so():
    """点到边界那一次不会改数字，也就必须有话说——否则只是"按钮没反应"。"""
    from app.bot.handlers import settings as h

    src = pathlib.Path(h.__file__).resolve().read_text(encoding="utf-8")
    assert "已经到上限" in src and "已经是 0" in src, "两个方向的边界都要有中文说明"
