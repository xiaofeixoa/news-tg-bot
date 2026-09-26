"""/settings, /pause, /resume, /setinterest (design doc sections 14.1, 17)."""

from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from app.bot.context import store
from app.bot.keyboards import inline as K
from app.config import AppConfig
from app.logging_setup import get_logger
from app.processing import breaking
from app.services import format as fmt
from app.services.llm import LLMService
from app.services.news import NewsService
from app.services.search import SearchService

log = get_logger("telegram")
router = Router(name="settings")

DAILY_SLOTS = ["06:00", "06:30", "07:00", "07:30", "08:00", "09:00", "10:00", "12:00"]
EVENING_SLOTS = ["17:00", "18:00", "19:00", "20:00", "21:00", "22:00", "23:00"]


@router.message(Command("settings"))
async def cmd_settings(message: Message, news: NewsService) -> None:
    user = news.user_for(message.chat.id)
    lines = [
        "⚙️ <b>你的推送设置</b>",
        "",
        f"☀️ 早报：{'开' if user['daily_enabled'] else '关'} · {user['daily_time']}（{fmt.esc(user['timezone'])}）",
        f"🌙 晚报：{'开' if user['evening_enabled'] else '关'} · {user['evening_time']}",
        f"🚨 突发新闻：{'开' if user['breaking_enabled'] else '关'} · {breaking.describe()}",
        f"📊 最低评分：{user['min_score']:.0f}",
        f"⏸ 自动推送：{'已暂停' if user['paused'] else '运行中'}",
        "",
        "<b>兴趣：</b>" + (
            "、".join(f"{i['value']}" for i in user["interests"][:12]) if user["interests"]
            else "未设置（默认按全局 AI 关键词）"
        ),
        "",
        "点按钮调整，或用 <code>/setinterest</code> 直接描述你想看什么。",
    ]
    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=K.settings_keyboard(
            paused=user["paused"], breaking=user["breaking_enabled"],
            daily=user["daily_time"], evening=user["evening_time"],
        ),
    )


@router.callback_query(F.data.startswith(f"{K.ACT}:"))
async def cb_settings(callback: CallbackQuery, news: NewsService) -> None:
    action = (callback.data or "").split(":", 1)[1]
    chat_id = callback.message.chat.id if callback.message else 0
    user = news.user_for(chat_id)
    note = "已更新"
    if action == "daily":
        user = news.update_user(chat_id, daily_time=_next(user["daily_time"], DAILY_SLOTS))
    elif action == "evening":
        user = news.update_user(chat_id, evening_time=_next(user["evening_time"], EVENING_SLOTS))
    elif action == "breaking":
        user = news.update_user(chat_id, breaking_enabled=not user["breaking_enabled"])
    elif action == "pause":
        user = news.update_user(chat_id, paused=not user["paused"])
        note = "自动推送已暂停" if user["paused"] else "自动推送已恢复"
    elif action == "score+":
        user = news.update_user(chat_id, min_score=min(90.0, user["min_score"] + 5))
    elif action == "score-":
        user = news.update_user(chat_id, min_score=max(0.0, user["min_score"] - 5))
    elif action == "interest":
        await _answer(callback, "用 <code>/setinterest</code> 加一句话描述，例如：\n"
                                "<code>/setinterest 我主要关注 AI Agent、开源模型、GPU 和 Claude</code>",
                      html=True)
        await callback.answer()
        return
    else:
        note = "未知操作"
    await cmd_settings(callback.message, news)
    await callback.answer(note)


@router.message(Command("pause"))
async def cmd_pause(message: Message, news: NewsService) -> None:
    news.update_user(message.chat.id, paused=True)
    await message.answer("⏸ 已暂停早报、晚报和突发新闻推送。/resume 恢复。")


@router.message(Command("resume"))
async def cmd_resume(message: Message, news: NewsService) -> None:
    news.update_user(message.chat.id, paused=False)
    await message.answer("▶️ 已恢复自动推送。")


@router.message(Command("setinterest"))
async def cmd_setinterest(message: Message, command: CommandObject, news: NewsService,
                          llm: LLMService, app_config: AppConfig) -> None:
    text = (command.args or "").strip()
    if not text:
        await message.answer(
            "用法：<code>/setinterest 我主要关注 AI Agent、LLM、GPT、Claude、开源模型、GPU、MCP</code>\n"
            "说“不要 …”可以排除主题。设置后评分会向这些方向加权。",
            parse_mode="HTML",
        )
        return
    interests: list[dict] | None = None
    summary = ""
    if llm.enabled:
        try:
            parsed = await llm.parse_interests(text)
            interests = parsed.get("interests") or []
            summary = parsed.get("summary") or ""
        except Exception as exc:
            log.info("interest parsing fell back to rules: %s", exc)
    if not interests:
        interests = rule_interests(text, app_config)
        summary = "（按关键词解析）"
    if not interests:
        await message.answer("没能从这句话里识别出兴趣词，换个说法试试？")
        return
    payload = news.set_interests(message.chat.id, interests, replace=True)
    lines = [f"🧠 已记录 {len(payload['interests'])} 条兴趣："]
    lines += [f"• {fmt.esc(i['type'])} · {fmt.esc(i['value'])} ({i['weight']:.1f})"
              for i in payload["interests"][:15]]
    if summary:
        lines += ["", f"<i>{fmt.esc(summary)}</i>"]
    lines += ["", "评分权重会立即生效，下一轮摘要开始按这个方向调整。"]
    await message.answer("\n".join(lines), parse_mode="HTML")


# ------------------------------------------------------------------ helpers
async def _answer(callback: CallbackQuery, text: str, *, html: bool = False) -> None:
    if callback.message is not None:
        await callback.message.answer(text, parse_mode="HTML" if html else None)


def _next(current: str, slots: list[str]) -> str:
    try:
        index = slots.index(current)
    except ValueError:
        return slots[0]
    return slots[(index + 1) % len(slots)]


def rule_interests(text: str, config: AppConfig) -> list[dict]:
    """Offline interest parsing: match taxonomy words and the global keyword list."""
    lowered = text.lower()
    exclude_mode = False
    out: dict[str, dict] = {}
    vocabulary = {
        *[c.lower() for c in config.category_names],
        *[s.lower() for s in config.all_subcategories],
        *config.filter_keywords,
    }
    for chunk in re.split(r"[,，、;；\n]| 我 | 以及 ", lowered):
        if re.search(r"(不要|不想|别|排除|exclude|no |without)", chunk):
            exclude_mode = True
        else:
            exclude_mode = False
        for word in sorted(vocabulary, key=len, reverse=True):
            if word and word in chunk:
                key = ("exclude" if exclude_mode else "topic") + ":" + word
                if key not in out:
                    out[key] = {
                        "type": "exclude" if exclude_mode else "topic",
                        "value": word,
                        "weight": 1.0 if not exclude_mode else 1.0,
                    }
    return list(out.values())[:20]
