"""Collector plug-ins. Importing this package registers every collector type."""

from app.collectors.base import (
    BaseCollector,
    BrowserTLSUnavailable,
    CollectorError,
    browser_tls_available,
    build_collectors,
    close_browser_session,
    close_client,
    get_client,
    known_types,
    register,
)
from app.collectors.rss import RSSCollector  # noqa: F401  registers "rss"
from app.collectors.hackernews import HackerNewsCollector  # noqa: F401  registers "hackernews"
from app.collectors.github import GitHubCollector  # noqa: F401  registers "github"
from app.collectors.reddit import RedditCollector  # noqa: F401  registers "reddit"
from app.collectors.arxiv import ArxivCollector  # noqa: F401  registers "arxiv"
from app.collectors.youtube import YouTubeCollector  # noqa: F401  registers "youtube"

# Not implemented in this version (phase 2/3 in the design doc):
#   telegram channel ingestion (needs a Telethon user session)
UNIMPLEMENTED_TYPES = {"telegram_channel"}

__all__ = [
    "BaseCollector",
    "BrowserTLSUnavailable",
    "CollectorError",
    "browser_tls_available",
    "build_collectors",
    "close_browser_session",
    "close_client",
    "get_client",
    "known_types",
    "register",
    "RSSCollector",
    "HackerNewsCollector",
    "GitHubCollector",
    "RedditCollector",
    "ArxivCollector",
    "YouTubeCollector",
    "UNIMPLEMENTED_TYPES",
]
