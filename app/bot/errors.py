"""Bot errors that must be catchable without importing the Telegram SDK.

`app.main` needs `BotNotConfigured` to turn a missing token into a readable
startup failure, and the collection-only mode needs that path too - importing it
from `app.bot.bot` would drag aiogram (and ~100MB) into every run.
"""

from __future__ import annotations


class BotNotConfigured(RuntimeError):
    """The bot cannot start: no token / invalid token / empty allowlist."""
