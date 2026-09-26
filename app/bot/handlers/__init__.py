"""All Telegram routers, in registration order."""

from aiogram import Router

from app.bot.handlers import chat, digest, free, news, search, settings, start, summary


def build_router() -> Router:
    root = Router(name="ai-news-radar")
    # Command routers first; the free-text agent router is a catch-all last.
    root.include_router(start.router)
    root.include_router(news.router)
    root.include_router(search.router)
    root.include_router(summary.router)
    root.include_router(digest.router)
    root.include_router(free.router)
    root.include_router(settings.router)
    root.include_router(chat.router)
    return root


__all__ = ["build_router", "chat", "digest", "free", "news", "search", "settings", "start", "summary"]
