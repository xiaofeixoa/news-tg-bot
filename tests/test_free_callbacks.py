"""/免费 的筛选按钮：先选时间窗、再点工具，两件事必须同时成立。

`cb_free` 整块（46-67）之前一行没测。它坏掉的形态不是报错，而是"我明明选了近 90 天，
点了个工具之后列表变短了，可 ✅ 还标在 90 天那里"。断言全部落在**面板上的 ✅ 标签**和
**被选中的那几条是否真的出现**上（用每条各自的标题片段，不数工具名出现几次）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.bot.context import store
from app.bot.handlers.free import cb_free, cmd_free
from app.config import get_config
from app.database import repository as repo
from app.database.database import session_scope
from app.database.models import Article
from app.processing.normalize import article_hash, url_hash
from app.services.news import NewsService

CHAT = 111111111
RECENT_A = "Qoder 面向全校开放"
RECENT_B = "Qoder 限免额度提升"
OLD_ONE = "Qoder 早期限免回顾"


class Chat:
    def __init__(self, cid: int) -> None:
        self.id = cid


class Msg:
    def __init__(self, cid: int = CHAT) -> None:
        self.chat = Chat(cid)
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.edited: list[tuple[str, dict[str, Any]]] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.sent.append((text, kwargs))

    async def edit_text(self, text: str, **kwargs: Any) -> None:
        self.edited.append((text, kwargs))

    @property
    def render(self) -> tuple[str, dict[str, Any]]:
        return (self.edited or self.sent)[-1]

    def chip(self, needle: str) -> str:
        markup = self.render[1].get("reply_markup")
        for row in getattr(markup, "inline_keyboard", []):
            for button in row:
                if needle in (button.text or ""):
                    return button.text or ""
        return ""


class Cb:
    def __init__(self, data: str, cid: int = CHAT) -> None:
        self.data = data
        self.message = Msg(cid)
        self.answers: list[str] = []

    async def answer(self, text: str | None = None, **kwargs: Any) -> None:
        self.answers.append(text or "")


def seed_offer(tool: str, title: str, *, days_ago: int) -> int:
    url = f"https://example.org/free/{tool}/{days_ago}/{abs(hash(title))}"
    with session_scope() as s:
        article = Article(
            source_name="Linux.do 福利分类", source_type="rss", title=title, url=url,
            normalized_url=url, url_hash=url_hash(url), hash=article_hash(title, url),
            content=f"{title} 限时免费开放中 free for a limited time",
            summary=f"{title} 限时免费", is_processed=True, final_score=55.0,
            published_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_ago),
            is_free_offer=True, free_offer_tool=tool,
        )
        s.add(article)
        s.commit()
        return article.id


@pytest.fixture
def offers() -> None:
    """三条同工具、不同年龄的限免：7 天只该看到两条，90 天三条都在。"""
    seed_offer("Qoder", RECENT_A, days_ago=3)
    seed_offer("Qoder", RECENT_B, days_ago=5)
    seed_offer("Qoder", OLD_ONE, days_ago=40)


@pytest.fixture(autouse=True)
def _fresh_store() -> Any:
    store.clear()
    yield
    store.clear()


def news_service() -> NewsService:
    return NewsService(get_config())


@pytest.mark.asyncio
async def test_a_tool_chip_keeps_the_window_the_user_chose(offers):
    """旧行为：点工具会把 days 改回默认 30，"近 90 天"这个选择被悄悄丢掉。"""
    news = news_service()
    days90 = Cb("f:d:90")
    await cb_free(days90, news, get_config())
    assert days90.message.chip("近 90 天").startswith("✅"), days90.message.chip("近 90 天")

    tool = Cb("f:t:Qoder")
    await cb_free(tool, news, get_config())
    body, _ = tool.message.render
    assert tool.message.chip("近 90 天").startswith("✅"), "窗口不能因为点了工具就跳回 30 天"
    assert store.saved_days(CHAT) == 90
    assert OLD_ONE in body, "40 天前那条在 90 天窗口里，必须在"
    assert RECENT_A in body and RECENT_B in body


@pytest.mark.asyncio
async def test_a_seven_day_window_really_is_seven_days(offers):
    news = news_service()
    await cb_free(Cb("f:d:7"), news, get_config())
    tool = Cb("f:t:Qoder")
    await cb_free(tool, news, get_config())
    body, _ = tool.message.render
    assert tool.message.chip("近 7 天").startswith("✅")
    assert store.saved_days(CHAT) == 7
    assert OLD_ONE not in body, "40 天前那条不该出现在 7 天窗口里"
    assert RECENT_A in body


@pytest.mark.asyncio
async def test_a_window_that_is_not_on_the_panel_is_refused_loudly(offers):
    """回调数据是客户端能乱填的：既不拿它当可信输入，也不静默兜底。"""
    from app.bot.handlers import free as free_module

    logged: list[str] = []

    class Recorder:
        def debug(self, *args):
            return None

        def info(self, msg, *args):
            logged.append(str(msg % args if args else msg))

        warning = error = exception = info

    original = free_module.log
    free_module.log = Recorder()
    try:
        await cb_free(Cb("f:d:99999"), news_service(), get_config())
        await cb_free(Cb("f:z:whatever"), news_service(), get_config())
    finally:
        free_module.log = original

    assert store.saved_days(CHAT) == 30, "被拒之后落到配置里的默认窗口，而不是客户端填的数"
    warnings = [m for m in logged if "free callback" in m]
    assert len(warnings) == 2, logged
    assert any("days=" in m for m in warnings) and any("unknown free callback payload" in m for m in warnings)


@pytest.mark.asyncio
async def test_a_callback_without_a_message_writes_nothing_for_a_phantom_chat(offers):
    news = news_service()
    for data in ("f:d:7", "f:t:Qoder", "f:x"):
        cb = Cb(data)
        cb.message = None
        await cb_free(cb, news, get_config())
        assert "/免费" in cb.answers[-1] and "不可用" in cb.answers[-1]
    assert store.saved_days(CHAT) is None, "面板都没了就不该记住筛选状态"
    with session_scope() as s:
        assert repo.get_user(s, 0) is None, "user_for(0) 会凭空造一个订阅者"


@pytest.mark.asyncio
async def test_the_command_itself_seeds_the_window_the_chips_reuse(offers):
    class Cmd:
        args = "90天"

    msg = Msg(CHAT)
    await cmd_free(msg, Cmd(), news_service(), get_config())
    assert store.saved_days(CHAT) == 90, "/免费 90天 之后，点工具要还停在 90 天"
    tool = Cb("f:t:Qoder")
    await cb_free(tool, news_service(), get_config())
    assert store.saved_days(CHAT) == 90
    assert OLD_ONE in tool.message.render[0]
