"""/免费 - 最近哪些 agent / 模型 / API 免费。

Telegram 的官方命令名只允许 a-z0-9_，所以菜单里放的是 /free；
但用户直接打 /免费 或说"最近有什么可以白嫖的"同样能命中。
"""

from __future__ import annotations

import re
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import inline as K
from app.config import AppConfig, as_int
from app.logging_setup import get_logger
from app.services import format as fmt
from app.services.news import ArticleView, NewsService
from app.services.search import SearchService

log = get_logger("telegram")
router = Router(name="free")

DAY_CHOICES = (7, 30, 90)


@router.message(Command("free", "免费", "mianfei", "coupon", "白嫖"))
async def cmd_free(message: Message, command: CommandObject, news: NewsService,
                   app_config: AppConfig) -> None:
    days, tool, keyword = _parse_args((command.args or "").strip(), app_config)
    items, note = _collect(news, days=days, tool=tool, keyword=keyword, config=app_config)
    live, checked = await _live(app_config, term=keyword or tool)
    tz_name = news.user_for(message.chat.id).get("timezone") or app_config.settings.timezone
    text = fmt.clip(fmt.free_offer_list(items, config=app_config, tz_name=tz_name,
                                        days=days, tool=tool, live=live, note=note,
                                        live_checked=checked, unverified=bool(note)))
    await message.answer(text, parse_mode="HTML",
                         reply_markup=K.free_keyboard(news.free_offer_tools(days=days), days=days))
    log.info("/免费 days=%s tool=%s keyword=%s -> %d item(s)", days, tool, keyword, len(items))


@router.callback_query(F.data.startswith(f"{K.FREE}:"))
async def cb_free(callback: CallbackQuery, news: NewsService, app_config: AppConfig) -> None:
    payload = (callback.data or "").split(":", 1)[1]
    kind, _, value = payload.partition(":")
    days, tool = int(app_config.get("free.days_default", 30)), None
    if kind == "d" and value.isdigit():
        days = int(value)
    elif kind == "t":
        tool = value.replace("_", " ") or None
        days = as_int(app_config.get("free.days_default"), 30)
    items, note = _collect(news, days=days, tool=tool, keyword=None, config=app_config)
    live, checked = await _live(app_config, term=tool)
    tz_name = news.user_for(callback.message.chat.id if callback.message else 0).get("timezone") \
        if callback.message else app_config.settings.timezone
    text = fmt.clip(fmt.free_offer_list(items, config=app_config, tz_name=tz_name or "UTC",
                                        days=days, tool=tool, live=live, note=note,
                                        live_checked=checked, unverified=bool(note)))
    keyboard = K.free_keyboard(news.free_offer_tools(days=days), days=days)
    if callback.message is not None:
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        except Exception as exc:  # "message is not modified"
            log.debug("free callback edit failed: %s", exc)
    await callback.answer()


# ------------------------------------------------------------------ helpers
def _haystack(view: ArticleView) -> str:
    """Lowercased text a keyword may hit — in both languages.

    Searchers type what they read, so the translated columns have to be part of
    the filter: dropping them made a keyword look absent from a row whose card
    plainly shows that keyword.
    """
    return " ".join(str(part or "") for part in (
        view.title, view.summary, view.content, view.title_zh, view.summary_zh)).lower()


async def _live(config: AppConfig, *, term: str | None = None) -> tuple[str, bool]:
    """(rendered live block, whether the pricing source answered).

    The second value matters: "no free models match qoder" and "the pricing
    endpoint is unreachable" are different answers for the user.
    """
    from app.services.free_models import get_free_model_watcher

    watcher = get_free_model_watcher(config)
    limit = as_int(config.get("free.models.max_items"), 8)
    if not watcher.enabled:
        return "", False
    try:
        models = await watcher.snapshot()
    except Exception as exc:  # snapshot() swallows transport errors; this is belt-and-braces
        log.warning("live free-model snapshot failed: %s", exc)
        return "", False
    if not models and watcher.failed:
        return "", False
    if term:
        needle = term.lower()
        matched = [m for m in models
                   if needle in f"{m.id} {m.name} {' '.join(m.vendors)}".lower()]
    else:
        matched = list(models)
    body = fmt.free_models_section(matched[:limit], config=config,
                                   total=len(models) if not term else len(matched),
                                   source=watcher.source_name, limit=limit)
    newly, ended = watcher.trend()
    if not term:
        # A filtered query already answers "is X free"; the change list is only
        # noise when it would show models the user did not ask about.
        trend = fmt.free_trend_section(newly, ended, source=watcher.source_name)
        if trend:
            body = f"{body}\n\n{trend}" if body else trend
    return body, True


def _collect(news: NewsService, *, days: int, tool: str | None, keyword: str | None,
             config: AppConfig) -> tuple[list[ArticleView], str]:
    """Free-offer rows for this scope, plus a note when we had to fall back.

    The fallback matters: a keyword search that finds no promo still returns the
    matching news, and listing those under "免费" without saying so would be a lie.
    """
    limit = as_int(config.get("free.limit"), 12)
    if tool and not keyword:
        found = news.free_offers(days=days, limit=limit, tool=tool)
        if found:
            return found, ""
        # 词表里有这个工具，但没采到它的限免：至少把相关新闻找出来，
        # 并标注它们不是限免（/free qoder 只回一句"没有"等于没答）。
        keyword = tool
    items = news.free_offers(days=days, limit=limit)
    if not keyword:
        return items, ""
    # 关键词（例如 "zcode"）可能还没被登记成免费资讯，回落到全文检索 + 现场判定
    from app.processing.free_offers import detect

    searched = SearchService(config, news).search(keyword, days=days, limit=limit * 3)
    searched = [i for i in searched if not tool or tool.lower() in _haystack(i)]
    matched = [item for item in searched
               if detect(item.title, item.summary, item.content, config=config) is not None]
    if matched:
        return matched, ""
    # 明确告诉用户"有这个关键词的新闻，但没看到免费信息"，比空列表有用
    return searched[:limit], (f"🔎 没有找到“{keyword} 免费”的明确消息，"
                              "下面只是相关新闻，别当成限免。")


FREE_INTENT_RE = re.compile(r"(免费|白嫖|限免|不要钱|free)", re.I)
FREE_ASK_RE = re.compile(r"(什么|哪些|有没有|最近|现在|可以|求|推荐|吗|？|\?)", re.I)


def looks_like_free_query(text: str, config: AppConfig | None = None) -> bool:
    """/免费 的自然语言入口："最近有什么可以白嫖的模型？"、"opencode 免费吗"。"""
    if not text or not FREE_INTENT_RE.search(text):
        return False
    return bool(FREE_ASK_RE.search(text)) or len(text) <= 24


async def answer_free(message: Message, news: NewsService, config: AppConfig, *, question: str) -> None:
    """Render the free-offer list for a natural-language question."""
    from app.config import get_config

    tools = {name.lower() for name in (get_config().free_terms.get("tools") or {})}
    lowered = question.lower()
    tool = next((name.title() for name in tools if name in lowered), None)
    days = 90 if any(k in question for k in ("三个月", "90", "近三个月")) else \
        7 if any(k in question for k in ("这周", "本周", "7 天", "最近几天")) else \
        int(config.get("free.days_default", 30))
    keyword = tool and None
    items, note = _collect(news, days=days, tool=None, keyword=None, config=config)
    if tool:
        filtered = [i for i in items if (i.free_offer or {}).get("tool", "").lower() == tool.lower()]
        if not filtered:
            filtered = [i for i in news.free_offers(days=days, limit=12) if tool.lower() in _haystack(i)]
        items = filtered or items
    tz_name = news.user_for(message.chat.id).get("timezone") or config.settings.timezone
    live, checked = await _live(config, term=tool or keyword_term(question))
    await message.answer(
        fmt.clip(fmt.free_offer_list(items, config=config, tz_name=tz_name,
                                     days=days, tool=tool, live=live, note=note,
                                     live_checked=checked, unverified=bool(note))),
        parse_mode="HTML",
        reply_markup=K.free_keyboard(news.free_offer_tools(days=days), days=days),
    )
    log.info("free answer for %r -> %d item(s)", question[:40], len(items))


def keyword_term(question: str) -> str | None:
    """Pull a model/vendor name out of a free-form question, if any."""
    from app.config import get_config

    lowered = (question or "").lower()
    for needle in get_config().free_terms.get("models") or []:
        if str(needle).lower() in lowered:
            return str(needle)
    return None


def _parse_args(argument: str, config: AppConfig) -> tuple[int, str | None, str | None]:
    """`/free qoder`、`/free deepseek 7`、`/free 90` 都要能用。"""
    from app.config import get_config

    text = (argument or "").strip()
    days = as_int(config.get("free.days_default"), 30)
    tool = None
    keyword = None
    if not text:
        return days, None, None

    tools = {name.lower(): name for name in (get_config().free_terms.get("tools") or {})}
    pieces = [p for p in re.split(r"\s+", text) if p]
    rest: list[str] = []
    for piece in pieces:
        lowered = piece.lower().strip()
        match = re.fullmatch(r"(\d{1,3})\s*[天d]", lowered)
        if match:
            days = max(1, min(365, int(match.group(1))))
            continue
        if lowered in {"7", "30", "90", "365"}:
            days = int(lowered)
            continue
        if lowered in tools:
            tool = tools[lowered]
            continue
        rest.append(piece)
    keyword = " ".join(rest).strip() or None
    if keyword and tool is None:
        for name_lower, name in tools.items():
            if name_lower in keyword.lower():
                tool = name
                keyword = keyword if keyword.lower() != name_lower else None
                break
    return days, tool, keyword
