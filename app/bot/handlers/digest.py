"""/digest - generate a briefing on demand (design doc sections 14.1, 16)."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.config import AppConfig
from app.logging_setup import get_logger
from app.services.digest import DigestService
from app.services.news import NewsService

log = get_logger("telegram")
router = Router(name="digest")


@router.message(Command("digest"))
async def cmd_digest(message: Message, command: CommandObject, digest: DigestService,
                     news: NewsService, app_config: AppConfig) -> None:
    argument = (command.args or "").strip().lower()
    kind = "evening" if argument in {"evening", "even", "晚报", "pm", "night"} else "morning"
    if argument in {"morning", "am", "早报", ""}:
        kind = "morning"
    elif argument not in {"evening", "even", "晚报", "pm", "night"}:
        await message.answer("用法：<code>/digest</code>（早报）或 <code>/digest evening</code>（晚报）",
                             parse_mode="HTML")
        return

    sent = await message.answer("🛰 正在生成简报，请稍候…")
    payload = await digest.generate(kind, chat_id=message.chat.id)
    if payload.empty or not payload.messages:
        await sent.edit_text(
            "📭 时间窗内没有达到推送门槛的新闻。\n"
            "可以试试 <code>/news</code> 看全部，或用 <code>/settings</code> 调低评分门槛。",
            parse_mode="HTML",
        )
        return
    await sent.delete()
    for text in payload.messages:
        await message.answer(text, parse_mode="HTML", disable_web_page_preview=False)
    digest.record_delivery(chat_id=message.chat.id, digest=payload)
    log.info("manual %s digest delivered to chat_id=%s (%d message(s))", kind, message.chat.id,
             len(payload.messages))
