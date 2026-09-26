"""Telegram access control (design doc section 24).

Only chat ids listed in ALLOWED_CHAT_IDS may use the bot. The allowlist is
empty by default, so a misconfigured deployment refuses everyone rather than
handing an LLM-budget-burning agent to strangers.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Chat, Message, TelegramObject

from app.config import AppConfig, get_config
from app.logging_setup import get_logger

log = get_logger("telegram")

REFUSAL = ("这个 Bot 只对个人开放：你的 Chat ID 不在白名单里。\n"
           "你的 Chat ID 是 <code>{chat_id}</code> —— 如果你是管理员，把它加进 .env 的 "
           "ALLOWED_CHAT_IDS 后重启服务即可。")
NOTICE_COOLDOWN = 300  # seconds between "go away" replies to the same stranger

_denied_notices: dict[int, float] = {}


def chat_id_of(event: TelegramObject) -> int | None:
    chat: Chat | None = getattr(event, "chat", None)
    if chat is None:
        message = getattr(event, "message", None)
        chat = getattr(message, "chat", None)
    return chat.id if chat is not None else None


def is_allowed(chat_id: int | None, config: AppConfig | None = None) -> bool:
    return (config or get_config()).settings.is_allowed_chat(chat_id)


class AccessMiddleware(BaseMiddleware):
    """Reject unknown chats and expose `chat_id`/`config` to every handler."""

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        chat_id = chat_id_of(event)
        data["chat_id"] = chat_id
        data["app_config"] = self.config
        if is_allowed(chat_id, self.config):
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            await event.answer("未授权", show_alert=False)
        elif isinstance(event, Message):
            now = time.time()
            if now - _denied_notices.get(chat_id or 0, 0) > NOTICE_COOLDOWN:
                _denied_notices[chat_id or 0] = now
                # Telling the stranger their own chat id is what turns the
                # allowlist into a one-line .env edit instead of a support ticket.
                await event.answer(REFUSAL.format(chat_id=chat_id), parse_mode="HTML")
            log.info("rejected message from unauthorised chat_id=%s", chat_id)
        else:
            log.info("rejected update from unauthorised chat_id=%s", chat_id)
        return None


class LoggingMiddleware(BaseMiddleware):
    """One line per update, without tokens or message bodies."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        chat_id = data.get("chat_id") or chat_id_of(event)
        label = type(event).__name__
        text = getattr(event, "text", None) or getattr(event, "data", None)
        log.debug("update %s chat_id=%s %.40s", label, chat_id, str(text or ""))
        started = time.time()
        try:
            return await handler(event, data)
        except Exception:
            log.exception("handler crashed for %s chat_id=%s", label, chat_id)
            raise
        finally:
            log.debug("handled %s in %.2fs", label, time.time() - started)


def silence_notice(chat_id: int | None) -> None:
    _denied_notices.pop(chat_id or 0, None)
