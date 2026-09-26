"""/news, /latest, /today, /yesterday, /topics, /sources + inline callbacks.

Interactive behaviour follows design doc section 15: a numbered headline list
with digit buttons, an article card on tap, and a deep-analysis button that
spends the strong model only when asked.
"""

from __future__ import annotations

import re
from typing import Sequence

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from app.bot.context import store
from app.bot.keyboards import inline as K
from app.config import AppConfig
from app.logging_setup import get_logger
from app.services import format as fmt
from app.services.digest import DigestService
from app.services.news import ArticleView, NewsService
from app.services.search import SearchService

log = get_logger("telegram")
router = Router(name="news")

PER_PAGE = 10


# ------------------------------------------------------------------ helpers
def _tz(user: dict) -> str:
    return user.get("timezone") or "UTC"


async def show_list(message: Message | None, *, chat_id: int, items: Sequence[ArticleView],
                    title: str, news: NewsService, config: AppConfig, page: int = 1,
                    callback: CallbackQuery | None = None, edit: bool = False) -> None:
    """Render one page of a remembered list so digit buttons stay stable."""
    all_ids = [a.id for a in items]
    store.remember(chat_id, list(items), kind="news")
    total_pages = max(1, (len(all_ids) + PER_PAGE - 1) // PER_PAGE)
    page = min(max(1, page), total_pages)
    start = (page - 1) * PER_PAGE
    window = items[start : start + PER_PAGE]
    # 先把即将显示的这几条翻成中文，用户不必等后台翻译轮
    window = await news.ensure_chinese(list(window))
    # Numbers must match the buttons, so re-start the circle per page.
    text = fmt.news_list(window, config=config, tz_name=_tz(news.user_for(chat_id)), title=title)
    keyboard = K.news_list_keyboard([a.id for a in window], page=page, total_pages=total_pages)
    if callback is not None:
        target = callback.message if edit else None
        if target is not None:
            try:
                await target.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
                await callback.answer()
                return
            except Exception as exc:  # "message is not modified" and friends
                log.debug("edit failed: %s", exc)
        await callback.answer()
        if message is not None:
            await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
        return
    if message is not None:
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


async def _answer_list(target: Message, news: NewsService, config: AppConfig, *, items: Sequence[ArticleView],
                       title: str) -> None:
    if not items:
        await target.answer(
            f"{fmt.esc(title)}\n\n暂无符合条件的新闻。先用 /sources 检查数据源，"
            f"或等下一轮采集（RSS 每 {config.settings.rss_fetch_interval // 60} 分钟一次）。",
            parse_mode="HTML",
        )
        return
    await show_list(target, chat_id=target.chat.id, items=list(items), title=title, news=news, config=config)


# ------------------------------------------------------------------ commands
@router.message(Command("news"))
async def cmd_news(message: Message, command: CommandObject, news: NewsService,
                   app_config: AppConfig) -> None:
    count = _int_arg(command.args) or int(app_config.get("bot.news_limit", 10))
    items = news.latest(limit=min(count, 30), hours=72)
    await _answer_list(message, news, app_config, items=items, title="🤖 最新 AI 新闻")


@router.message(Command("latest"))
async def cmd_latest(message: Message, news: NewsService, app_config: AppConfig) -> None:
    items = news.latest(limit=int(app_config.get("bot.news_limit", 10)), hours=24)
    await _answer_list(message, news, app_config, items=items, title="🕐 最近 24 小时")


@router.message(Command("today"))
async def cmd_today(message: Message, news: NewsService, app_config: AppConfig) -> None:
    user = news.user_for(message.chat.id)
    items, label = news.day(offset_days=0, limit=20, tz_name=user.get("timezone"))
    await _answer_list(message, news, app_config, items=items, title=f"📅 今日 AI 新闻 · {label}")


@router.message(Command("yesterday"))
async def cmd_yesterday(message: Message, news: NewsService, app_config: AppConfig) -> None:
    user = news.user_for(message.chat.id)
    items, label = news.day(offset_days=-1, limit=20, tz_name=user.get("timezone"))
    await _answer_list(message, news, app_config, items=items, title=f"📅 昨日 AI 新闻 · {label}")


@router.message(Command("topics"))
async def cmd_topics(message: Message, news: NewsService) -> None:
    topics = news.topics()
    if not topics:
        await message.answer("还没有分类数据，先等一轮采集完成。")
        return
    lines = ["📚 <b>新闻分类</b>", ""]
    lines += [f"{t['emoji']} <b>{fmt.esc(t['label'])}</b> · {t['count']} 条"
              + (f"\n   <i>{fmt.esc(t['description'])}</i>" if t.get("description") else "")
              for t in topics]
    lines += ["", "点击分类查看该方向的新闻："]
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=K.topics_keyboard(topics))


@router.message(Command("sources"))
async def cmd_sources(message: Message, news: NewsService, app_config: AppConfig) -> None:
    configured = news.configured_sources()
    live = {s["name"]: s for s in news.sources()}
    lines = ["🔌 <b>信息来源</b>", ""]
    for source in configured:
        state = live.get(source["name"], {})
        if not source["enabled"]:
            flag = "⚪️"
        elif state.get("last_error"):
            flag = "🔴"
        elif state.get("last_success_at"):
            flag = "🟢"
        else:
            flag = "🟡"
        detail = f"{fmt.esc(app_config.source_type_label(source['type']))} · {fmt.esc(app_config.quality_label(source['quality']))}"
        if state.get("last_success_at"):
            detail += f" · 上次成功 {state['last_success_at']:%m-%d %H:%M}"
        if state.get("items"):
            detail += f" · 采集 {state['items']} 条"
        lines.append(f"{flag} <b>{fmt.esc(source['name'])}</b>\n   {detail}")
        if state.get("last_error"):
            lines.append(f"   <i>错误：{fmt.esc(fmt.plain(state['last_error'], 120))}</i>")
    lines += ["", fmt.status_line(news.stats())]
    await message.answer("\n".join(lines), parse_mode="HTML")


# ----------------------------------------------------------------- callbacks
@router.callback_query(F.data.startswith(f"{K.ARTICLE}:"))
async def cb_article(callback: CallbackQuery, news: NewsService, app_config: AppConfig) -> None:
    article_id = _int_arg(callback.data.split(":", 1)[1])
    item = news.by_id(article_id or 0) if article_id else None
    if item is None:
        await callback.answer("这条新闻已经不在库里了", show_alert=True)
        return
    user = news.user_for(callback.message.chat.id if callback.message else callback.from_user.id)
    await news.ensure_chinese([item])
    text = fmt.article_card(item, config=app_config, tz_name=_tz(user))
    keyboard = K.article_keyboard(item.id, item.url)
    if callback.message is not None:
        try:
            await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)
        except Exception:
            log.exception("article card failed")
    await callback.answer()


@router.callback_query(F.data.startswith(f"{K.DEEP}:"))
async def cb_deep(callback: CallbackQuery, news: NewsService, search: SearchService,
                  app_config: AppConfig) -> None:
    article_id = _int_arg(callback.data.split(":", 1)[1])
    if not article_id:
        await callback.answer("参数有误", show_alert=True)
        return
    item = news.by_id(article_id)
    if item is None:
        await callback.answer("这条新闻已经不在库里了", show_alert=True)
        return
    await callback.answer("正在用 AI 深入分析…")
    text = await search.deep_summary(article_id)
    if callback.message is not None and text:
        await callback.message.answer(
            text, parse_mode="HTML", reply_markup=K.deep_keyboard(item.id, item.url)
        )


@router.callback_query(F.data.startswith(f"{K.TOPIC}:"))
async def cb_topic(callback: CallbackQuery, news: NewsService, app_config: AppConfig) -> None:
    category = (callback.data or "").split(":", 1)[1]
    chat_id = callback.message.chat.id if callback.message else 0
    items = news.by_category(category, limit=PER_PAGE, days=7)
    if callback.message is not None:
        if not items:
            await callback.message.answer(f"{app_config.category_label(category)} 分类最近 7 天还没有新闻。")
        else:
            await show_list(callback.message, chat_id=chat_id, items=items,
                            title=f"{fmt.esc(app_config.category_label(category))} 分类",
                            news=news, config=app_config)
    await callback.answer()


@router.callback_query(F.data.startswith(f"{K.PAGE}:"))
async def cb_page(callback: CallbackQuery, news: NewsService, app_config: AppConfig) -> None:
    page = _int_arg(callback.data.split(":", 1)[1]) or 1
    chat_id = callback.message.chat.id if callback.message else 0
    context = store.get(chat_id)
    ids = context.article_ids if context else []
    items = [news.by_id(i) for i in ids]
    items = [i for i in items if i is not None]
    if not items:
        items = news.latest(limit=30, hours=72)
    await show_list(callback.message, chat_id=chat_id, items=items, title="🤖 AI 新闻",
                    news=news, config=app_config, page=page, callback=callback, edit=True)


@router.callback_query(F.data.startswith(f"{K.BACK}:"))
async def cb_back(callback: CallbackQuery, news: NewsService, app_config: AppConfig) -> None:
    tag = (callback.data or "").split(":", 1)[1]
    chat_id = callback.message.chat.id if callback.message else 0
    if tag == "sources":
        await cmd_sources(callback.message, news)  # type: ignore[arg-type]
    elif tag == "topics":
        await cmd_topics(callback.message, news)  # type: ignore[arg-type]
    else:
        context = store.get(chat_id)
        items = [news.by_id(i) for i in (context.article_ids if context else [])]
        items = [i for i in items if i is not None]
        if not items:
            items = news.latest(limit=int(app_config.get("bot.news_limit", 10)), hours=72)
        await show_list(callback.message, chat_id=chat_id, items=items, title="🤖 最新 AI 新闻",
                        news=news, config=app_config, callback=callback)
    await callback.answer()


def _int_arg(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\d+", str(value))
    return int(match.group()) if match else None
