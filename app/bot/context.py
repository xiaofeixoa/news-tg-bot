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
    # /免费 上"近 N 天"的选择：Telegram 不会告诉我们面板现在是什么状态，
    # 而工具按钮的回调数据里放不下天数（标签最长 40 字，64 字节的上限很快就爆）。
    days: int | None = None
    # 翻页按钮（`p:2`）以前把每一页都重新写成"🤖 AI 新闻"，于是 `/today` 的第 2 页
    # 顶部写着另一个名字，而 v1.84 那句"共 102 条"也在第 2 页上消失了。
    # 列表的名字与总数必须活过翻页，所以它们和 id 一起记住。
    title: str = ""
    total: int | None = None
    more: str = ""


class ContextStore:
    def __init__(self) -> None:
        self._store: dict[int, ChatContext] = {}

    def remember(self, chat_id: int, items: list[ArticleView] | list[int], *, kind: str = "news",
                 title: str | None = None, total: int | None = None,
                 more: str | None = None) -> ChatContext:
        ids: list[int] = []
        labels: list[str] = []
        for item in items:
            if isinstance(item, ArticleView):
                ids.append(item.id)
                labels.append(item.summary or item.title)
            else:
                ids.append(int(item))
                labels.append("")
        previous = self._store.get(chat_id)
        context = ChatContext(article_ids=ids, labels=labels, kind=kind, updated_at=time.time(),
                              # 没给新值时沿用上一份：翻页时 `show_list` 只带条目，
                              # 不能让列表的名字和总数在第二页上蒸发。
                              title=title if title is not None else (previous.title if previous else ""),
                              total=total if total is not None else (previous.total if previous else None),
                              more=more if more is not None else (previous.more if previous else ""))
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

    def remember_days(self, chat_id: int, days: int) -> None:
        """记住这个 chat 当前选的时间窗，不清掉他已经看到的列表。"""
        context = self._store.get(chat_id) or ChatContext(kind="free")
        context.days = days
        context.updated_at = time.time()
        self._store[chat_id] = context

    def saved_days(self, chat_id: int) -> int | None:
        context = self.get(chat_id)
        return context.days if context is not None else None

    def forget(self, chat_id: int) -> None:
        self._store.pop(chat_id, None)

    def clear(self) -> None:
        self._store.clear()


store = ContextStore()
