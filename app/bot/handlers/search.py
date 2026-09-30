"""/search (design doc sections 14.1, 26)."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.bot.handlers.news import PER_PAGE, show_list
from app.config import AppConfig
from app.logging_setup import get_logger
from app.services import format as fmt
from app.services.news import NewsService
from app.services.search import SearchService

log = get_logger("telegram")
router = Router(name="search")


@router.message(Command("search"))
async def cmd_search(message: Message, command: CommandObject, news: NewsService,
                     search: SearchService, app_config: AppConfig) -> None:
    query = (command.args or "").strip()
    if not query:
        await message.answer(
            "用法：<code>/search 关键词</code>\n"
            "例如 <code>/search AI Agent</code>、<code>/search Claude</code>、<code>/search MCP</code>",
            parse_mode="HTML",
        )
        return
    days = 30
    result = search.search_result(query, days=days, limit=PER_PAGE * 2)
    items = result.items
    # 命中的条数与这一页的条数是两件事，写法收在 fmt.search_title 里（可测）。
    title = fmt.search_title(query, matched=result.matched, shown=len(items), days=days,
                             pool_capped=result.pool_capped, pool=result.pool)
    if not items:
        await message.answer(
            f"{title}\n\n没有找到相关新闻。\n"
            "建议：换更短的英文关键词（<code>Agent</code> / <code>GPU</code>），"
            "或用 /news 看当前采集范围。",
            parse_mode="HTML",
        )
        return
    await show_list(message, chat_id=message.chat.id, items=items, title=title,
                    news=news, config=app_config)
    log.info("search %r -> %d results", query, len(items))
