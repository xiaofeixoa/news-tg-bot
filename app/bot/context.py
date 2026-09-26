"""Short-lived per-chat memory.

Telegram needs the previous list to answer "第二条详细说一下" (§44), and inline
keyboard callbacks must know which articles the user is looking at.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.services.news import ArticleView

TTL_SECONDS = 60 * 60 * 6


@dataclass
class ChatContext:
    article_ids: list[int] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)
    kind: str = "news"


class ContextStore:
    def __init__(self) -> None:
        self._store: dict[int, ChatContext] = {}

    def remember(self, chat_id: int, items: list[ArticleView] | list[int], *, kind: str = "news") -> ChatContext:
        ids: list[int] = []
        labels: list[str] = []
        for item in items:
            if isinstance(item, ArticleView):
                ids.append(item.id)
                labels.append(item.summary or item.title)
            else:
                ids.append(int(item))
                labels.append("")
        context = ChatContext(article_ids=ids, labels=labels, kind=kind, updated_at=time.time())
        self._store[chat_id] = context
        return context

    def get(self, chat_id: int) -> ChatContext | None:
        context = self._store.get(chat_id)
        if context is None:
            return None
        if time.time() - context.updated_at > TTL_SECONDS:
            self._store.pop(chat_id, None)
            return None
        return context

    def recent(self, chat_id: int) -> list[dict[str, Any]]:
        context = self.get(chat_id)
        if context is None:
            return []
        return [{"index": i + 1, "id": a, "title": t} for i, (a, t) in enumerate(zip(context.article_ids, context.labels))]

    def nth(self, chat_id: int, index: int) -> int | None:
        context = self.get(chat_id)
        if context is None or not 1 <= index <= len(context.article_ids):
            return None
        return context.article_ids[index - 1]

    def forget(self, chat_id: int) -> None:
        self._store.pop(chat_id, None)

    def clear(self) -> None:
        self._store.clear()


store = ContextStore()
