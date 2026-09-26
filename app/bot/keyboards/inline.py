"""Inline keyboards (design doc section 15).

Callback data is capped at 64 bytes by Telegram, so actions stay short:
a:<id> article card, d:<id> deep analysis, b:<tag> back, t:<category>, p:<page>.
"""

from __future__ import annotations

from typing import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# Callback action prefixes.
ARTICLE = "a"
DEEP = "d"
BACK = "b"
TOPIC = "t"
PAGE = "p"
ACT = "x"
FREE = "f"

CIRCLE = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


def cb(action: str, value: object = "") -> str:
    return f"{action}:{value}" if value != "" else action


def _url(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url)


def _cb(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def news_list_keyboard(article_ids: Sequence[int], *, page: int = 1,
                       total_pages: int = 1) -> InlineKeyboardMarkup:
    """One digit button per headline, five per row (§15)."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for index, article_id in enumerate(article_ids[: len(CIRCLE)]):
        row.append(_cb(CIRCLE[index], cb(ARTICLE, article_id)))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav: list[InlineKeyboardButton] = []
    if page > 1:
        nav.append(_cb("⬅️ 上一页", cb(PAGE, page - 1)))
    if total_pages > page:
        nav.append(_cb("下一页 ➡️", cb(PAGE, page)))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def article_keyboard(article_id: int, url: str, *, back_tag: str = "news") -> InlineKeyboardMarkup:
    rows = [[_url("🔗 阅读原文", url), _cb("🧠 AI 深度分析", cb(DEEP, article_id))]]
    rows.append([_cb("⬅️ 返回新闻", cb(BACK, back_tag))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def deep_keyboard(article_id: int, url: str, *, back_tag: str = "news") -> InlineKeyboardMarkup:
    rows = [[_url("🔗 阅读原文", url), _cb("📄 常规摘要", cb(ARTICLE, article_id))]]
    rows.append([_cb("⬅️ 返回新闻", cb(BACK, back_tag))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def topics_keyboard(topics: Sequence[dict], *, page: int = 1) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for topic in topics[:20]:
        label = f"{topic['emoji']} {topic['label']} ({topic['count']})"
        row.append(_cb(label[:48], cb(TOPIC, topic["category"])))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([_cb("🏠 最新新闻", cb(BACK, "news"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_keyboard(*, paused: bool, breaking: bool, daily: str, evening: str,
                      daily_on: bool = True, evening_on: bool = True) -> InlineKeyboardMarkup:
    """时间按钮只换时间，提醒开关按钮只管开关 - 两者放一行会互相踩。"""
    rows = [
        [
            _cb("☀️ 早报 " + daily, cb(ACT, "daily")),
            _cb(("🔔 早报提醒 开" if daily_on else "🔕 早报提醒 关"), cb(ACT, "daily_on")),
        ],
        [
            _cb("🌙 晚报 " + evening, cb(ACT, "evening")),
            _cb(("🔔 晚报提醒 开" if evening_on else "🔕 晚报提醒 关"), cb(ACT, "evening_on")),
        ],
        [
            _cb("🚨 突发 " + ("开" if breaking else "关"), cb(ACT, "breaking")),
            _cb(("▶️ 恢复推送" if paused else "⏸ 暂停推送"), cb(ACT, "pause")),
        ],
        [
            _cb("🔽 降低门槛", cb(ACT, "score-")),
            _cb("🔼 提高门槛", cb(ACT, "score+")),
            _cb("🧠 兴趣设置", cb(ACT, "interest")),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sources_keyboard(sources: Sequence[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for source in sources[:20]:
        flag = "🟢" if source.get("enabled") else "⚪️"
        if source.get("last_error"):
            flag = "🔴"
        row.append(_cb(f"{flag} {source['name']}"[:44], cb(BACK, "sources")))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def refresh_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_cb("🔄 刷新", cb(BACK, "news"))]])


def free_keyboard(tools: Sequence[dict], *, days: int = 30) -> InlineKeyboardMarkup:
    """/免费 的筛选按钮：时间范围 + 出现最多的工具。"""
    rows: list[list[InlineKeyboardButton]] = [[
        _cb(("✅ " if days == choice else "") + f"近 {days if days == choice else choice} 天",
            cb(FREE, f"d:{choice}"))
        for choice in (7, 30, 90)
    ]]
    row: list[InlineKeyboardButton] = []
    for tool in list(tools)[:8]:
        name = str(tool.get("tool") or "")
        if not name:
            continue
        label = f"{name} ({tool.get('count', 0)})"[:40]
        row.append(_cb(label, cb(FREE, f"t:{name.replace(' ', '_')}")))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)
