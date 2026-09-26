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
from app.services.search import SearchService

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
    if not argument:
        return None
    text = argument.strip()
    match = re.search(r"#?(\d{1,9})", text)
    if match:
        return int(match.group(1))
    digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if text in digits:
        return store.nth(chat_id, digits[text])
    return None
