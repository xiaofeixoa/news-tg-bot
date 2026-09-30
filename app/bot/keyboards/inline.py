"""Inline keyboards (design doc section 15).

Callback data is capped at 64 bytes by Telegram, so actions stay short:
a:<id> article card, d:<id> deep analysis, b:<tag> back, t:<category>, p:<page>.

That sentence was the only thing enforcing the cap. Two buttons carry text that
comes out of the database - the tool name `/免费` filters by and the category
`/topics` filters by - and both columns are free-form (the detector invents tool
names from promo sentences; `free_offer_tool` is VARCHAR(64), i.e. up to 192 UTF-8
bytes). One oversized value does not drop one button: Telegram rejects the whole
reply markup, so the command stops answering until that row is archived. Now the
guard lives here: a button that cannot be encoded is left out, and the message
still goes out.
"""

from __future__ import annotations

from typing import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.logging_setup import get_logger

log = get_logger("telegram")

CALLBACK_MAX_BYTES = 64          # Telegram's own limit on callback_data
Slot = InlineKeyboardButton | None        # `_cb` 放不下时返回 None，由 `_keyboard` 滤掉

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


def _cb(text: str, data: str) -> InlineKeyboardButton | None:
    """放不下就返回 None：少一个按钮，总比整条消息发不出去好。

    截断不是选项——`f:t:<半个工具名>` 会成为一个看起来能点、点了却说"没有这个工具的
    限免"的假按钮，那比少一个筛选按钮坏得多。
    """
    size = len(data.encode("utf-8"))
    if size > CALLBACK_MAX_BYTES:
        log.warning("keyboard button %r dropped: callback_data is %d bytes (limit %d)",
                    text[:24], size, CALLBACK_MAX_BYTES)
        return None
    return InlineKeyboardButton(text=text, callback_data=data)


def _keyboard(rows: Sequence[Sequence[Slot]]) -> InlineKeyboardMarkup:
    """去掉被丢弃的按钮和随之变空的行；全空时留下一个回主页的按钮。"""
    kept = [[button for button in row if button is not None] for row in rows]
    kept = [row for row in kept if row]
    if not kept:
        kept = [[_cb("🏠 最新新闻", cb(BACK, "news")) or _cb("🏠", "x")]]
    return InlineKeyboardMarkup(inline_keyboard=kept)


def news_list_keyboard(article_ids: Sequence[int], *, page: int = 1,
                       total_pages: int = 1) -> InlineKeyboardMarkup:
    """One digit button per headline, five per row (§15)."""
    rows: list[list[Slot]] = []
    row: list[Slot] = []
    for index, article_id in enumerate(article_ids[: len(CIRCLE)]):
        row.append(_cb(CIRCLE[index], cb(ARTICLE, article_id)))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav: list[Slot] = []
    if page > 1:
        nav.append(_cb("⬅️ 上一页", cb(PAGE, page - 1)))
    if total_pages > page:
        nav.append(_cb("下一页 ➡️", cb(PAGE, page)))
    if nav:
        rows.append(nav)
    return _keyboard(rows)


def article_keyboard(article_id: int, url: str, *, back_tag: str = "news",
                     deep_available: bool = True) -> InlineKeyboardMarkup:
    """`deep_available=False` 时不提供 🧠：那个按钮在这台机器上点了只会重发同一张卡片。

    承诺了能力却永远交付不了的按钮，比没有按钮更糟——用户会以为是自己没等到。
    """
    row: list[Slot] = [_url("🔗 阅读原文", url)]
    if deep_available:
        row.append(_cb("🧠 AI 深度分析", cb(DEEP, article_id)))
    rows: list[list[Slot]] = [row]
    rows.append([_cb("⬅️ 返回新闻", cb(BACK, back_tag))])
    return _keyboard(rows)


def deep_keyboard(article_id: int, url: str, *, back_tag: str = "news",
                  summary_available: bool = True) -> InlineKeyboardMarkup:
    """`summary_available=False` 时不给「📄 常规摘要」：屏幕上那段已经是它了（v1.78）。

    和 v1.77 那条 🧠 是同一个缺陷的反方向：那次是按钮给不出新内容，这次是
    `/summary` 冷启动——规则模式下回的就是卡片，再挂一个"看卡片"的按钮。
    """
    row: list[Slot] = [_url("🔗 阅读原文", url)]
    if summary_available:
        row.append(_cb("📄 常规摘要", cb(ARTICLE, article_id)))
    rows: list[list[Slot]] = [row]
    rows.append([_cb("⬅️ 返回新闻", cb(BACK, back_tag))])
    return _keyboard(rows)


def topics_keyboard(topics: Sequence[dict], *, page: int = 1) -> InlineKeyboardMarkup:
    rows: list[list[Slot]] = []
    row: list[Slot] = []
    for topic in topics[:20]:
        label = f"{topic['emoji']} {topic['label']} ({topic['count']})"
        row.append(_cb(label[:48], cb(TOPIC, topic["category"])))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([_cb("🏠 最新新闻", cb(BACK, "news"))])
    return _keyboard(rows)


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
    return _keyboard(rows)


def refresh_keyboard() -> InlineKeyboardMarkup:
    return _keyboard([[_cb("🔄 刷新", cb(BACK, "news"))]])


def free_keyboard(tools: Sequence[dict], *, days: int = 30) -> InlineKeyboardMarkup:
    """/免费 的筛选按钮：时间范围 + 出现最多的工具。"""
    rows: list[list[Slot]] = [[
        _cb(("✅ " if days == choice else "") + f"近 {days if days == choice else choice} 天",
            cb(FREE, f"d:{choice}"))
        for choice in (7, 30, 90)
    ]]
    row: list[Slot] = []
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
    return _keyboard(rows)
