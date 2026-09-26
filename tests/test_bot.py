"""Bot-layer tests: commands, access control, inline keyboards, natural language.

Handlers are exercised directly against the real service + SQLite layers with a
record-only Telegram object, so the assertions cover the text and keyboard a
user would actually see. Live API round-trips are covered by
scripts/telegram_smoke.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from aiogram.types import InlineKeyboardMarkup

from app.bot.keyboards import inline as K
from app.bot.middleware import REFUSAL, AccessMiddleware, chat_id_of, is_allowed
from app.config import get_config
from app.database import repository as repo
from app.processing.normalize import build_article
from app.services.news import get_news_service
from app.services.search import SearchService

ALLOWED = 111111111
STRANGER = 987654321


# ----------------------------------------------------------- fake Telegram
class FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class FakeUser:
    def __init__(self, uid: int = 42, name: str = "Tester") -> None:
        self.id = uid
        self.full_name = name


class FakeMessage:
    def __init__(self, chat_id: int = ALLOWED, text: str | None = None) -> None:
        self.chat = FakeChat(chat_id)
        self.from_user = FakeUser()
        self.text = text
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def answer(self, text: str, **kwargs: Any) -> "FakeMessage":
        self.sent.append((text, kwargs))
        return FakeMessage(self.chat.id)

    async def edit_text(self, text: str, **kwargs: Any) -> None:
        self.sent.append((text, kwargs))

    async def delete(self) -> None:
        return None

    @property
    def last(self) -> str:
        return self.sent[-1][0] if self.sent else ""

    @property
    def last_kwargs(self) -> dict[str, Any]:
        return self.sent[-1][1] if self.sent else {}

    def all_text(self) -> str:
        return "\n".join(t for t, _ in self.sent)


class FakeCallback(FakeMessage):
    def __init__(self, data: str, chat_id: int = ALLOWED) -> None:
        self.from_user = FakeUser()
        self.message = FakeMessage(chat_id)
        self.chat = FakeChat(chat_id)
        self.id = "cb-1"
        self.data = data
        self.answers: list[tuple[str, dict]] = []

    async def answer(self, text: str | None = None, **kwargs: Any) -> None:
        self.answers.append((text or "", kwargs))


class Command:
    def __init__(self, args: str | None = None) -> None:
        self.args = args


# --------------------------------------------------------------- fixtures
def seed(session, *, llm=None) -> list[int]:
    """Store + process a handful of headlines; returns article ids."""
    import asyncio

    ids: list[int] = []
    items = [
        ("OpenAI releases GPT-5 for everyone", "https://openai.com/gpt5", "OpenAI"),
        ("Anthropic ships Claude Opus 4.5", "https://anthropic.com/opus45", "Anthropic"),
        ("NVIDIA B200 GPU enters production", "https://nvidia.com/b200", "NVIDIA"),
        ("Google Gemini 3 tops the reasoning benchmark", "https://blog.google/gemini3", "Google AI"),
        ("vLLM 0.7 speeds up agent serving", "https://blog.vllm/07", "Hugging Face"),
    ]
    for title, url, source in items:
        data = build_article(
            title=title, url=url, source_name=source,
            content=f"{title}. " + ("The change improves inference speed and lowers cost for AI agents. " * 3),
            published_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        article = repo.save_article(session, data)
        if article:
            ids.append(article.id)
    session.commit()
    if llm is not None:
        asyncio.get_event_loop().run_until_complete(
            __import__("app.processing.pipeline", fromlist=["x"]).process_pending(
                session, config=get_config(), llm=llm, limit=20))
        session.commit()
    return ids


@pytest.fixture
def seeded(session, fake_llm):
    ids = seed(session, llm=fake_llm)
    assert ids, "test data did not persist"
    return ids


# ---------------------------------------------------------- access control
def test_allowlist_is_fail_closed():
    assert is_allowed(None) is False
    assert is_allowed(STRANGER) is False
    assert is_allowed(ALLOWED) is True


def test_allowlist_reads_env_variable():
    config = get_config()
    assert ALLOWED in config.settings.chat_id_whitelist
    assert config.settings.is_allowed_chat(STRANGER) is False


@pytest.mark.asyncio
async def test_middleware_rejects_unknown_chat_and_never_runs_handler():
    calls: list[str] = []

    async def handler(event, data):
        calls.append("ran")

    middleware = AccessMiddleware(get_config())
    result = await middleware(handler, FakeMessage(STRANGER), {})
    assert result is None and calls == []

    await middleware(handler, FakeMessage(ALLOWED), {})
    assert calls == ["ran"]


# ---------------------------------------------------------------- commands
@pytest.mark.asyncio
async def test_start_registers_user_and_shows_help(seeded):
    from app.bot.handlers.start import cmd_start

    message = FakeMessage(ALLOWED, "/start")
    await cmd_start(message, get_news_service(), get_config())
    assert "AI News Radar" in message.last
    assert message.last_kwargs.get("parse_mode") == "HTML"
    user = get_news_service().user_for(ALLOWED)
    assert user["chat_id"] == ALLOWED and user["timezone"]


@pytest.mark.asyncio
async def test_help_lists_every_documented_command():
    from app.bot.handlers.start import cmd_help

    message = FakeMessage(ALLOWED, "/help")
    await cmd_help(message)
    for command in ("/news", "/search", "/summary", "/digest", "/today", "/topics", "/sources",
                    "/settings", "/pause"):
        assert command in message.last, f"{command} missing from /help"


@pytest.mark.asyncio
async def test_news_command_returns_numbered_list_and_keyboard(seeded):
    from app.bot.handlers.news import cmd_news

    message = FakeMessage(ALLOWED, "/news")
    await cmd_news(message, Command(), get_news_service(), get_config())
    assert "最新 AI 新闻" in message.last
    keyboard = message.last_kwargs["reply_markup"]
    assert isinstance(keyboard, InlineKeyboardMarkup)
    data = [button.callback_data for row in keyboard.inline_keyboard for button in row if button.callback_data]
    assert data and data[0].startswith("a:")
    assert any(d.split(":")[1].isdigit() for d in data)


@pytest.mark.asyncio
async def test_callback_opens_article_card_with_source_and_link(seeded):
    from app.bot.handlers.news import cb_article

    callback = FakeCallback(f"a:{seeded[0]}")
    await cb_article(callback, get_news_service(), get_config())
    text = callback.message.last
    assert "一句话总结" in text
    assert "来源" in text
    keyboard = callback.message.last_kwargs["reply_markup"]
    buttons = [(b.text, b.url, b.callback_data) for row in keyboard.inline_keyboard for b in row]
    assert any(url and url.startswith("http") for _t, url, _d in buttons), "阅读原文 link"
    assert any(d and d.startswith("d:") for _t, _u, d in buttons), "AI 深度分析"


@pytest.mark.asyncio
async def test_deep_summary_uses_the_strong_model(seeded, fake_llm):
    from app.bot.handlers.summary import cmd_summary

    message = FakeMessage(ALLOWED, f"/summary {seeded[0]}")
    search = SearchService(get_config(), get_news_service(), fake_llm)
    await cmd_summary(message, Command(str(seeded[0])), get_news_service(), search, get_config())
    assert "行业影响" in message.all_text() or "核心内容" in message.all_text()
    assert "deep_analyze" in fake_llm.calls


@pytest.mark.asyncio
async def test_search_finds_stored_news(seeded):
    from app.bot.handlers.search import cmd_search

    message = FakeMessage(ALLOWED, "/search GPT")
    await cmd_search(message, Command("GPT"), get_news_service(),
                     SearchService(get_config(), get_news_service()), get_config())
    assert "GPT" in message.all_text()
    assert message.last_kwargs.get("reply_markup") is not None


@pytest.mark.asyncio
async def test_search_without_arguments_shows_usage(seeded):
    from app.bot.handlers.search import cmd_search

    message = FakeMessage(ALLOWED, "/search")
    await cmd_search(message, Command(""), get_news_service(),
                     SearchService(get_config(), get_news_service()), get_config())
    assert "/search 关键词" in message.last


@pytest.mark.asyncio
async def test_topics_and_sources_commands(seeded):
    from app.bot.handlers.news import cmd_sources, cmd_topics

    message = FakeMessage(ALLOWED, "/topics")
    await cmd_topics(message, get_news_service())
    assert "分类" in message.last
    keyboard = message.last_kwargs["reply_markup"]
    data = [b.callback_data for row in keyboard.inline_keyboard for b in row if b.callback_data]
    assert any(d.startswith("t:") for d in data)

    other = FakeMessage(ALLOWED, "/sources")
    await cmd_sources(other, get_news_service())
    assert "信息来源" in other.last and "OpenAI" in other.last


@pytest.mark.asyncio
async def test_settings_pause_and_resume(seeded):
    from app.bot.handlers.settings import cmd_pause, cmd_resume, cmd_settings

    message = FakeMessage(ALLOWED, "/settings")
    await cmd_settings(message, get_news_service())
    assert "推送设置" in message.last
    assert isinstance(message.last_kwargs["reply_markup"], InlineKeyboardMarkup)

    await cmd_pause(message, get_news_service())
    assert get_news_service().user_for(ALLOWED)["paused"] is True
    await cmd_resume(message, get_news_service())
    assert get_news_service().user_for(ALLOWED)["paused"] is False


@pytest.mark.asyncio
async def test_setinterest_without_llm_falls_back_to_rules(seeded):
    from app.bot.handlers.settings import cmd_setinterest

    class Disabled:
        enabled = False

    message = FakeMessage(ALLOWED, "/setinterest")
    await cmd_setinterest(message, Command("我主要关注 AI Agent、GPT 和 GPU"),
                          get_news_service(), Disabled(), get_config())
    interests = get_news_service().interests(ALLOWED)
    assert interests, "rule parser must still capture topics offline"
    assert any(i["value"].lower() in {"ai agent", "gpt", "gpu"} for i in interests)


@pytest.mark.asyncio
async def test_digest_command_sends_briefing(seeded, fake_llm):
    from app.bot.handlers.digest import cmd_digest
    from app.services.digest import DigestService
    from app.services.news import NewsService

    message = FakeMessage(ALLOWED, "/digest")
    news = NewsService(get_config())
    service = DigestService(get_config(), news, fake_llm)
    await cmd_digest(message, Command(""), service, news, get_config())
    text = message.all_text()
    assert "Briefing" in text or "简报" in text
    assert "digest_overview" in fake_llm.calls


@pytest.mark.asyncio
async def test_digest_command_rejects_unknown_kind(seeded, fake_llm):
    from app.bot.handlers.digest import cmd_digest
    from app.services.digest import DigestService
    from app.services.news import NewsService

    message = FakeMessage(ALLOWED, "/digest wat")
    news = NewsService(get_config())
    await cmd_digest(message, Command("wat"), DigestService(get_config(), news, fake_llm), news,
                     get_config())
    assert "用法" in message.last


@pytest.mark.asyncio
async def test_natural_language_question_answers_from_database(seeded, fake_llm):
    from app.bot.handlers.chat import free_text
    from app.services.digest import DigestService

    search = SearchService(get_config(), get_news_service(), fake_llm)
    message = FakeMessage(ALLOWED, "最近 AI Agent 有什么值得关注的？")
    await free_text(message, get_news_service(), search, DigestService(get_config()), get_config())
    assert "模型发布" in message.all_text() or "新闻" in message.all_text()
    assert "answer_question" in fake_llm.calls


def test_refusal_message_is_polite_and_informative():
    assert "ALLOWED_CHAT_IDS" in REFUSAL


def test_every_documented_command_has_a_handler():
    """Wiring check: aiogram only fails at the first update if a router is missing."""
    from app.bot.bot import COMMANDS, create_dispatcher

    dp = create_dispatcher(get_config())
    names: set[str] = set()

    def walk(router):
        for handler in router.message.handlers:
            names.add(handler.callback.__name__)
        for handler in router.callback_query.handlers:
            names.add(handler.callback.__name__)
        for child in router.sub_routers:
            walk(child)

    walk(dp)
    expected = {
        "cmd_start", "cmd_help", "cmd_news", "cmd_latest", "cmd_today", "cmd_yesterday",
        "cmd_topics", "cmd_sources", "cmd_search", "cmd_summary", "cmd_digest",
        "cmd_settings", "cmd_pause", "cmd_resume", "cmd_setinterest", "free_text",
        "cb_article", "cb_deep", "cb_topic", "cb_page", "cb_back", "cb_settings",
    }
    assert expected <= names, f"missing handlers: {sorted(expected - names)}"
    assert {c.command for c in COMMANDS} >= {"start", "news", "search", "summary", "digest",
                                             "topics", "sources", "settings", "pause", "resume"}


def test_scheduler_only_registers_enabled_source_types():
    from app.scheduler.jobs import NewsJobs, create_scheduler

    config = get_config()
    jobs = NewsJobs(config)
    scheduler = create_scheduler(jobs, config)
    ids = {job.id for job in scheduler.get_jobs()}
    assert "collect:rss" in ids and "collect:hackernews" in ids
    assert "collect:reddit" not in ids, "reddit is disabled in sources.yaml"
    assert "ai:process" in ids and "digest:watcher" in ids


def test_chat_id_extraction_from_nested_update():
    assert chat_id_of(FakeMessage(555)) == 555
    callback = FakeCallback("a:1", chat_id=777)
    assert chat_id_of(callback) == 777
