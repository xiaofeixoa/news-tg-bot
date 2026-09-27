"""Shared fixtures.

Tests run the real SQLite schema and the real pipeline; only the network and
the LLM are replaced, so a green suite means the code paths that matter execute.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session", autouse=True)
def _environment() -> Iterable[None]:
    """Point the app at a throwaway database and a known allowlist."""
    tmp = tempfile.mkdtemp(prefix="ai-news-radar-tests-")
    os.environ["ENV_FILE"] = str(ROOT / ".env.nonexistent")
    os.environ["CONFIG_DIR"] = str(ROOT / "config")
    os.environ["DATA_DIR"] = tmp
    os.environ["LOG_DIR"] = str(Path(tmp) / "logs")
    os.environ["DATABASE_URL"] = f"sqlite:///{Path(tmp) / 'test.db'}".replace("\\", "/")
    os.environ["ALLOWED_CHAT_IDS"] = "111111111"
    os.environ["TELEGRAM_BOT_TOKEN"] = ""
    os.environ["LLM_BASE_URL"] = ""
    os.environ["LLM_API_KEY"] = ""
    os.environ["LLM_MODEL"] = ""
    os.environ["TIMEZONE"] = "Asia/Shanghai"

    from app.config import reload_config

    config = reload_config()
    # Keep the suite hermetic: the rendering tests must not reach the network.
    # tests/test_translate.py turns this back on with stubbed HTTP clients.
    config.raw.setdefault("translate", {})["enabled"] = False
    from app.database.database import init_db

    init_db()
    yield
    reload_config()


@pytest.fixture(scope="session")
def config():
    from app.config import get_config

    return get_config()


@pytest.fixture
def session():
    from app.database.database import get_session_factory

    factory = get_session_factory()
    db_session = factory()
    try:
        yield db_session
        db_session.commit()
    finally:
        db_session.rollback()
        db_session.close()


@pytest.fixture(autouse=True)
def _restore_settings():
    """一条用例改了缓存的 settings 就会污染后面所有模块：快照 + 还原。

    真实的事故是 2026-09-27：一个白名单用例把 `allowed_chat_ids` 清空，
    全量跑时把 `test_sender.py` 里依赖白名单的用例打挂，单模块跑却全绿。
    """
    from app.config import get_config

    settings = get_config().settings
    snapshot = (settings.allowed_chat_ids, settings.telegram_bot_token)
    yield
    settings.allowed_chat_ids, settings.telegram_bot_token = snapshot


@pytest.fixture(autouse=True)
def _clean_tables():
    """Each test starts from an empty database; row counts stay meaningful."""
    from sqlalchemy import delete

    from app.database.database import get_engine
    from app.database.models import Article, Event, PushLog, Source, Tag, User, UserInterest, article_tags

    engine = get_engine()
    with engine.begin() as connection:
        for table in (PushLog.__table__, article_tags, Tag.__table__, Article.__table__,
                      Event.__table__, UserInterest.__table__, User.__table__, Source.__table__):
            connection.execute(delete(table))
    yield


@pytest.fixture
def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def make_article_dict(title: str, url: str, *, source: str = "OpenAI",
                      content: str | None = None, published: datetime | None = None,
                      quality: str = "A", meta: dict | None = None) -> dict[str, Any]:
    from app.processing.normalize import build_article

    return build_article(
        title=title,
        url=url,
        source_name=source,
        source_type="rss",
        content=content or f"{title}. " + ("Details about the release. " * 8),
        published_at=published or datetime.now(timezone.utc) - timedelta(hours=1),
        quality=quality,
        meta=meta or {},
    )


@pytest.fixture
def article_factory():
    return make_article_dict


class FakeLLM:
    """Stands in for the OpenAI-compatible client with deterministic answers."""

    enabled = True

    def __init__(self, *, category: str = "AI Models", importance: float = 88,
                 relevance: float = 92, fail: bool = False) -> None:
        self.category = category
        self.importance = importance
        self.relevance = relevance
        self.fail = fail
        self.calls: list[str] = []

    async def classify(self, article: dict, interests: list | None = None) -> dict:
        self.calls.append("classify")
        if self.fail:
            raise RuntimeError("provider exploded")
        return {
            "is_ai_related": True,
            "category": self.category,
            "subcategory": "GPT",
            "relevance_score": self.relevance,
            "importance_score": self.importance,
            "novelty_score": 90,
            "tags": ["OpenAI", "LLM"],
            "reason": "model release",
        }

    async def summarize(self, article: dict) -> dict:
        self.calls.append("summarize")
        if self.fail:
            raise RuntimeError("provider exploded")
        return {
            "summary": "该模型在多项基准上取得领先。",
            "key_points": ["上下文窗口扩大", "价格下降"],
            "why_it_matters": "直接影响 Agent 产品的成本结构。",
            "tags": ["GPT"],
            "importance_score": self.importance,
        }

    async def deep_analyze(self, article: dict, related: list | None = None) -> dict:
        self.calls.append("deep_analyze")
        return {
            "headline": article.get("title", ""),
            "what_happened": "一句话说明发生了什么。",
            "key_points": ["要点一", "要点二"],
            "why_it_matters": "影响推理成本。",
            "industry_impact": "推动同类厂商降价。",
            "open_questions": ["定价细节？"],
            "confidence_note": "",
        }

    async def digest_overview(self, items: list) -> dict:
        self.calls.append("digest_overview")
        return {"overview": "今天主要是模型发布。", "highlights": ["新模型", "降价"]}

    async def answer_question(self, question: str, results: list, context: list | None = None,
                              *, now: str = "") -> str:
        self.calls.append("answer_question")
        return f"根据 {len(results)} 条新闻：今天最重要的是模型发布。"

    async def detect_intent(self, text: str, recent: list) -> dict:
        self.calls.append("detect_intent")
        return {"intent": "search", "query": "Agent", "days": 14, "index": None}

    async def parse_interests(self, text: str) -> dict:
        self.calls.append("parse_interests")
        return {"interests": [{"type": "topic", "value": "AI Agent", "weight": 1.0}],
                "summary": "关注 Agent"}

    async def same_event(self, a: dict, b: dict) -> bool:
        self.calls.append("same_event")
        return True


@pytest.fixture
def fake_llm():
    return FakeLLM()


@pytest.fixture
def broken_llm():
    return FakeLLM(fail=True)
