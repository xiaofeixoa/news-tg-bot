"""/summary <id> - deep analysis of one article (design doc sections 14.1, 23 layer 3)."""

from __future__ import annotations

import re

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.bot.context import store
from app.bot.keyboards import inline as K
from app.config import AppConfig
from app.logging_setup import get_logger
from app.services import format as fmt
from app.services.news import NewsService
from app.services.search import SearchService, ordinal_to_int

log = get_logger("telegram")
router = Router(name="summary")


@router.message(Command("summary"))
async def cmd_summary(message: Message, command: CommandObject, news: NewsService,
                      search: SearchService, app_config: AppConfig) -> None:
    article_id = _resolve_id(message.chat.id, command.args)
    if article_id is None:
        await message.answer(
            "用法：<code>/summary 123</code>（新闻编号）\n"
            "也可以先 <code>/news</code>，再直接说「第二条详细说说」。",
            parse_mode="HTML",
        )
        return
    item = news.by_id(article_id)
    if item is None:
        await message.answer(f"没有找到编号 {article_id} 的新闻。用 /news 查看当前编号。")
        return
    placeholder = await message.answer(f"🧠 正在对 #{item.id} 做深度分析…")
    text = await search.deep_summary(article_id)
    if not text:
        await placeholder.edit_text("分析失败，请稍后重试。")
        return
    await placeholder.delete()
    await message.answer(text, parse_mode="HTML",
                         reply_markup=K.deep_keyboard(item.id, item.url))
    log.info("deep analysis #%s for chat_id=%s", article_id, message.chat.id)


def _resolve_id(chat_id: int, argument: str | None) -> int | None:
    """`/summary 123`、`#123`、`第三条`、`第三条详细说说` -> 那条新闻的 id.

    This used to carry its own copy of the ordinal table, holding only a bare
    一..十, so the phrasing the bot's own hint teaches (`/summary 第二条`) was
    answered with a usage message. `ordinal_to_int` is the natural-language path's
    parser and now the single source here too.
    """
    text = str(argument or "").strip()
    if not text:
        return None
    # 有序数词壳子（第/条/个）就按"列表里的第几条"读，`第2条` 是第二条而不是 2 号新闻；
    # 裸数字和 `#123` 才是新闻编号。旧写法先抓数字，`第2条` 会被当成 id=2。
    if text.startswith("第") or re.search(r"[条个篇则]", text):
        index = ordinal_to_int(text)
        return store.nth(chat_id, index) if index else None
    direct = re.search(r"#?(\d{1,9})", text)
    if direct:
        return int(direct.group(1))
    index = ordinal_to_int(text)
    return store.nth(chat_id, index) if index else None
