"""Outbound Telegram delivery for scheduled jobs.

Retries network failures, and treats a blocked/deleted chat as a permanent
error so a dead conversation cannot stall the scheduler (design doc section 25).

aiogram is imported inside the methods, not at module scope: it costs ~106MB of
heap on import, and this module is reachable from the scheduler, `--once` and
every CLI script - none of which need Telegram.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from app.config import AppConfig, get_config
from app.logging_setup import get_logger
from app.services.digest import Digest

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aiogram import Bot
    from aiogram.types import InlineKeyboardMarkup

log = get_logger("telegram")

MAX_MESSAGE = 4096


class TelegramSender:
    def __init__(self, bot: Bot, config: AppConfig | None = None) -> None:
        self.bot = bot
        self.config = config or get_config()

    @classmethod
    def from_token(cls, token: str | None = None, config: AppConfig | None = None) -> "TelegramSender":
        """Builds the bot through app.bot.bot.create_bot so TELEGRAM_PROXY applies."""
        from app.bot.bot import create_bot

        config = config or get_config()
        if token and token != config.settings.telegram_bot_token:
            config.settings.telegram_bot_token = token
        return cls(create_bot(config), config)

    async def send(self, chat_id: int, text: str, *,
                   reply_markup: InlineKeyboardMarkup | None = None,
                   preview: bool = True, attempts: int = 3,
                   parse_mode: str | None = "HTML") -> bool:
        """Deliver one message. The formatters emit HTML, so this is the path that
        has to ask Telegram to parse it - interactive replies set it themselves."""
        # The formatters already fit messages to this limit; a raw slice here would
        # cut a briefing line mid-word without saying so, so the ellipsis stays.
        if len(text or "") > MAX_MESSAGE:
            text = text[: MAX_MESSAGE - 1] + "…"
        else:
            text = text or ""
        if not text.strip():
            return False
        from aiogram.exceptions import (TelegramAPIError, TelegramBadRequest,
                                        TelegramForbiddenError, TelegramNetworkError,
                                        TelegramRetryAfter)

        for attempt in range(1, attempts + 1):
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=reply_markup,
                    disable_web_page_preview=not preview,
                    parse_mode=parse_mode,
                )
                return True
            except TelegramBadRequest as exc:
                if parse_mode is None or "can't parse" not in str(exc).lower():
                    log.error("telegram rejected message for chat_id=%s: %s", chat_id, exc)
                    return False
                # One badly escaped title must not cost the whole morning briefing.
                log.warning("markup rejected for chat_id=%s, resending as plain text: %s",
                            chat_id, exc)
                return await self.send(chat_id, text, reply_markup=reply_markup,
                                       preview=preview, attempts=1, parse_mode=None)
            except TelegramRetryAfter as exc:
                wait = float(getattr(exc, "retry_after", 3) or 3)
                log.warning("telegram flood wait %.1fs for chat_id=%s", wait, chat_id)
                await asyncio.sleep(min(wait, 30))
            except TelegramForbiddenError as exc:
                log.warning("chat_id=%s blocked the bot, skipping: %s", chat_id, exc)
                return False
            except TelegramNetworkError as exc:
                log.warning("telegram network error (%d/%d) chat_id=%s: %s", attempt, attempts, chat_id, exc)
                await asyncio.sleep(min(2 ** attempt, 15))
            except TelegramAPIError as exc:
                log.error("telegram api error for chat_id=%s: %s", chat_id, exc)
                return False
        log.error("giving up on chat_id=%s after %d attempts", chat_id, attempts)
        return False

    async def send_digest(self, chat_id: int, digest: Digest) -> int:
        """Messages delivered, or 0 unless every part landed.

        The caller uses the result to decide whether today's briefing was sent,
        and `if sent:` is true for `1` - so counting a half-delivered digest as
        delivered would mark the day done and lose the tail of the briefing for
        good. Reporting 0 instead lets the watcher try again inside its grace
        window; the worst case there is a repeated first page, which is a much
        smaller harm than a missing one.
        """
        sent = 0
        for index, text in enumerate(digest.messages):
            if not await self.send(chat_id, text):
                log.error("digest for chat_id=%s stopped at message %d/%d; not marking it delivered",
                          chat_id, index + 1, len(digest.messages))
                return 0
            sent += 1
            if index < len(digest.messages) - 1:
                await asyncio.sleep(0.6)  # stay well clear of rate limits
        return sent

    async def close(self) -> None:
        await self.bot.session.close()
