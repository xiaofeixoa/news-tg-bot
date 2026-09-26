"""Telegram bot layer: routers, middleware, keyboards, outbound sender.

Exports resolve lazily (PEP 562). Importing `app.bot.sender` from the scheduler
must not drag aiogram into the process - it costs ~106MB of heap, and the
collection-only mode and every CLI script pay that bill for a library they
never call.
"""

from __future__ import annotations

from typing import Any

__all__ = ["COMMANDS", "BotNotConfigured", "TelegramSender", "create_bot",
           "create_dispatcher", "register_commands"]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if name == "TelegramSender":
        from app.bot.sender import TelegramSender

        return TelegramSender
    if name == "BotNotConfigured":
        from app.bot.errors import BotNotConfigured

        return BotNotConfigured
    from app.bot import bot as _bot

    return getattr(_bot, name)


def __dir__() -> list[str]:
    return sorted(__all__)
