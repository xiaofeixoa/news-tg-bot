"""/start and /help (design doc sections 14.1, 24)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app.bot.keyboards.inline import refresh_keyboard
from app.config import AppConfig
from app.logging_setup import get_logger
from app.services import format as F_fmt
from app.services.news import NewsService

log = get_logger("telegram")
router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message, news: NewsService, app_config: AppConfig) -> None:
    user = news.user_for(
        message.chat.id,
        display_name=message.from_user.full_name if message.from_user else None,
        user_id=message.from_user.id if message.from_user else None,
        tz=_tz_name(message),
    )
    stats = news.stats()
    first = not bool(stats["sources"]) or stats["total_articles"] == 0
    lines = [
        "👋 <b>AI News Radar 已就绪</b>",
        "",
        "我是你的个人 AI 新闻 Agent：持续采集 RSS / Hacker News / GitHub / arXiv，",
        "去重、分类、评分，再用中文摘要送到这里。",
        "",
        F_fmt.status_line(stats),
        "",
        f"☀️ 早报 {user['daily_time']} · 🌙 晚报 {user['evening_time']} · "
        f"🚨 突发 {'开' if user['breaking_enabled'] else '关'}（阈值 {user['breaking_threshold']:.0f}）",
        "",
        "发 /news 看今天的新闻，或直接问我，比如「最近 AI Agent 有什么值得关注的？」。",
        "完整命令见 /help。",
    ]
    if first:
        lines += ["", "<i>第一轮采集还在进行，如果列表为空请稍后再试。</i>"]
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=refresh_keyboard())
    log.info("started chat_id=%s", message.chat.id)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(F_fmt.help_text(), parse_mode="HTML")


def _tz_name(message: Message) -> str | None:
    """Telegram does not expose a chat timezone; keep the configured default."""
    return None
