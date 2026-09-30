"""Natural-language news agent (design doc sections 14.2, 31, 44)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message

from app.bot.context import store
from app.config import AppConfig
from app.logging_setup import get_logger
from app.services import format as fmt
from app.services.digest import DigestService
from app.services.news import ArticleView, NewsService
from app.services.search import SearchService

log = get_logger("telegram")
router = Router(name="chat")


@router.message(F.text & ~F.text.startswith("/"))
async def free_text(message: Message, news: NewsService, search: SearchService,
                    digest: DigestService, app_config: AppConfig) -> None:
    text = (message.text or "").strip()
    if not text:
        return
    # Small talk and out-of-scope questions should not burn strong-model tokens.
    if len(text) < 3:
        await message.answer("想看新闻就发 /news，或者问我一个具体问题，比如「最近 MCP 有什么新东西？」")
        return
    # "有什么可以白嫖的模型？" 这类问题走 /免费 的检索，而不是通用问答
    from app.bot.handlers.free import answer_free, looks_like_free_query

    if looks_like_free_query(text, app_config):
        await answer_free(message, news, app_config, question=text)
        return
    recent = _recent(news, message.chat.id)
    placeholder = await message.answer("🤔 正在检索新闻库…")
    answer = await search.answer(text, chat_id=message.chat.id, recent=recent)
    try:
        await placeholder.delete()
    except Exception:
        pass
    body = answer.text
    footer = (
        ""
        if answer.intent in {"help", "settings", "sources"}
        else f"\n\n<i>依据 {len(answer.used_ids)} 条库内新闻 · 关键词：{fmt.esc(fmt.plain(answer.query, 40)) or '-'}</i>"
    )
    # 聊天回答是唯一没经过 fmt.clip 的那条用户可见输出（简报走 split_messages，
    # /免费 与 /news 都 clip 过）。答案长度取决于模型/条数，不能赌它一定短。
    await message.answer(fmt.clip(body + footer), parse_mode="HTML")
    if answer.used_ids:
        store.remember(message.chat.id, answer.used_ids, kind="answer")
    log.info("agent intent=%s ids=%d chat_id=%s", answer.intent, len(answer.used_ids), message.chat.id)


def _recent(news: NewsService, chat_id: int) -> list[ArticleView]:
    context = store.get(chat_id)
    if context and context.article_ids:
        items = [news.by_id(i) for i in context.article_ids]
        items = [i for i in items if i is not None]
        if items:
            return items
    return news.latest(limit=10, hours=72)
