"""/settings, /pause, /resume, /setinterest (design doc sections 14.1, 17)."""

from __future__ import annotations

import re
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from app.bot.context import store
from app.bot.keyboards import inline as K
from app.config import AppConfig, as_int
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
    await message.answer(_panel(user, news.breaking_quota(message.chat.id)),
                         parse_mode="HTML", reply_markup=_keyboard(user))


@router.callback_query(F.data.startswith(f"{K.ACT}:"))
async def cb_settings(callback: CallbackQuery, news: NewsService, app_config: AppConfig) -> None:
    action = (callback.data or "").split(":", 1)[1]
    if callback.message is None:
        # 没有可回写的面板时，写库就等于把设置存进一个查不到主人的抽屉
        await callback.answer("这条设置消息已失效，请再用 /settings 打开一次")
        return
    chat_id = callback.message.chat.id
    user = news.user_for(chat_id)
    note = "已更新"
    if action == "daily":
        user = news.update_user(chat_id, daily_time=_next(user["daily_time"], DAILY_SLOTS))
        note = f"☀️ 早报时间 {user['daily_time']}"
    elif action == "evening":
        user = news.update_user(chat_id, evening_time=_next(user["evening_time"], EVENING_SLOTS))
        note = f"🌙 晚报时间 {user['evening_time']}"
    elif action == "daily_on":
        user = news.update_user(chat_id, daily_enabled=not user["daily_enabled"])
        note = "🔔 早报已开" if user["daily_enabled"] else "🔕 早报已关，只剩晚报和突发"
    elif action == "evening_on":
        user = news.update_user(chat_id, evening_enabled=not user["evening_enabled"])
        note = "🔔 晚报已开" if user["evening_enabled"] else "🔕 晚报已关，只剩早报和突发"
    elif action == "breaking":
        user = news.update_user(chat_id, breaking_enabled=not user["breaking_enabled"])
        note = "🚨 突发新闻已开" if user["breaking_enabled"] else "🚨 突发新闻已关"
    elif action == "pause":
        user = news.update_user(chat_id, paused=not user["paused"])
        note = "自动推送已暂停" if user["paused"] else "自动推送已恢复"
    elif action in ("score+", "score-"):
        ceiling = news.score_ceiling()
        step = 5 if action == "score+" else -5
        floor = min(ceiling, max(0.0, user["min_score"] + step))
        if floor == user["min_score"]:
            # 到边上的那一次也必须说话；但不能只说"到边了"而把"这一档几条都不达标"丢掉——
            # 那正是他按下之后唯一想知道的事。两句一起给。
            edge = (f"已经到上限 {ceiling:.0f}：规则模式最高就给得到这个分，再往上提就一条都进不了简报了"
                    if step > 0 else "已经是 0：抓到的每一条都会进简报")
            note = f"{edge} · {_floor_note(news, app_config, floor)}"
        else:
            user = news.update_user(chat_id, min_score=floor)
            note = _floor_note(news, app_config, floor)
    elif action == "interest":
        await _answer(callback, "用 <code>/setinterest</code> 加一句话描述，例如：\n"
                                "<code>/setinterest 我主要关注 AI Agent、开源模型、GPU 和 Claude</code>",
                      html=True)
        await callback.answer()
        return
    else:
        note = "未知操作"
    await _refresh(callback, user, note, news.breaking_quota(chat_id))


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
def _panel(user: dict[str, Any], quota: dict[str, Any] | None = None) -> str:
    lines = [
        "⚙️ <b>你的推送设置</b>",
        "",
        f"☀️ 早报：{'开' if user['daily_enabled'] else '关'} · {user['daily_time']}"
        f"（{fmt.timezone_label(user['timezone'])}）",
        f"🌙 晚报：{'开' if user['evening_enabled'] else '关'} · {user['evening_time']}",
        f"🚨 突发新闻：{'开' if user['breaking_enabled'] else '关'} · {breaking.describe()}",
    ]
    # 上限用完时"开"是不够的：他需要知道为什么今天不会再有突发（见 §11 v1.83）
    quota_text = fmt.quota_line(quota)
    if quota_text:
        lines.append(f"   └ {quota_text}")
    lines += [
        f"📊 最低评分：{user['min_score']:.0f}",
        f"⏸ 自动推送：{'已暂停' if user['paused'] else '运行中'}",
        "",
        "<b>兴趣：</b>" + (
            # 兴趣词是从他打字的那句话里解析出来的，可能带 < 或 &：不转义的话
            # Telegram 会拒掉整个面板，他连"改回默认"的按钮都点不到。
            "、".join(fmt.esc(str(i["value"])) for i in user["interests"][:12])
            if user["interests"] else "未设置（默认按全局 AI 关键词）"
        ),
        "",
        "点按钮调整，或用 <code>/setinterest</code> 直接描述你想看什么。",
    ]
    return "\n".join(lines)


def _keyboard(user: dict[str, Any]):
    return K.settings_keyboard(
        paused=user["paused"], breaking=user["breaking_enabled"],
        daily=user["daily_time"], evening=user["evening_time"],
        daily_on=user["daily_enabled"], evening_on=user["evening_enabled"],
    )


async def _refresh(callback: CallbackQuery, user: dict[str, Any], note: str,
                   quota: dict[str, Any] | None = None) -> None:
    """就地改写面板，而不是再发一份。

    按钮上写的是当前状态（⏸ 暂停推送 / 🚨 突发 开），旧面板留在聊天里就是一个
    还挂着假状态的入口：再点一下会把刚设置好的东西原样改回去。
    """
    if callback.message is not None:
        try:
            await callback.message.edit_text(_panel(user, quota), parse_mode="HTML",
                                             reply_markup=_keyboard(user))
        except TelegramBadRequest as exc:
            # "message is not modified"（未知操作那一支）以及坏掉的 markup 都只
            # 影响这一条消息：设置已经落库，提示照发。
            log.warning("settings panel not edited: %s", exc)
    await callback.answer(note)


def _floor_note(news: NewsService, config: AppConfig, floor: float) -> str:
    """门槛后面跟着"这个窗口里还剩几条"，否则 🔼 点到 90 的结果只能等到早上发现。

    简报在候选为空时是直接不发（`Digest(empty=True)`），所以这一档一旦抬过当天
    最高分，唯一的预告就是这条提示。
    """
    hours = as_int(config.get("digest.morning.window_hours", 24), 24)
    kept = news.count_eligible(hours=hours, min_score=floor)
    if not kept:
        return f"⚠️ 门槛 {floor:.0f}：近 {hours} 小时 0 条达标，这样会收不到简报"
    return f"门槛 {floor:.0f}：近 {hours} 小时 {kept} 条达标"


async def _answer(callback: CallbackQuery, text: str, *, html: bool = False) -> None:
    if callback.message is not None:
        await callback.message.answer(text, parse_mode="HTML" if html else None)


def _next(current: str, slots: list[str]) -> str:
    """按面板给的顺序往后一档。库里存的时间不在档位里时，取它之后的第一档 -
    直接跳回 slots[0] 会把一个自定义的 07:15 一把拉回 06:00。
    """
    try:
        index = slots.index(current)
    except ValueError:
        later = [slot for slot in slots if slot > current]
        return later[0] if later else slots[0]
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
