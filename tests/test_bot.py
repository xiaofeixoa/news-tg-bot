"""Bot-layer tests: commands, access control, inline keyboards, natural language.

Handlers are exercised directly against the real service + SQLite layers with a
record-only Telegram object, so the assertions cover the text and keyboard a
user would actually see. Live API round-trips are covered by
scripts/telegram_smoke.py.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

import pytest

from aiogram.types import CallbackQuery, Chat as TgChat, InlineKeyboardMarkup
from aiogram.types import Message as TgMessage, User as TgUser

from app.bot.keyboards import inline as K
from app.bot.middleware import REFUSAL, AccessMiddleware, chat_id_of, is_allowed
from app.config import get_config
from app.database import repository as repo
from app.database.database import session_scope
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
async def test_callback_opens_article_card_with_source_and_link(seeded, monkeypatch):
    from app.bot.handlers.news import cb_article

    callback = FakeCallback(f"a:{seeded[0]}")
    await cb_article(callback, get_news_service(), get_config())
    text = callback.message.last
    assert "一句话总结" in text
    assert "来源" in text
    keyboard = callback.message.last_kwargs["reply_markup"]
    buttons = [(b.text, b.url, b.callback_data) for row in keyboard.inline_keyboard for b in row]
    assert any(url and url.startswith("http") for _t, url, _d in buttons), "阅读原文 link"
    # 这台机器（以及测试环境）没配 LLM：🧠 点下去只会把同一张卡片重发一遍，
    # 所以按钮不该出现。配上 key 之后它必须回来——两个方向都要钉住。
    assert not any(d and d.startswith("d:") for _t, _u, d in buttons), \
        f"没有 LLM 时不该提供深度分析按钮：{[b[2] for b in buttons]}"

    config = get_config()
    monkeypatch.setattr(config.settings, "llm_base_url", "https://example.org/v1")
    monkeypatch.setattr(config.settings, "llm_api_key", "sk-test")
    monkeypatch.setattr(config.settings, "llm_model", "some-model")
    second = FakeCallback(f"a:{seeded[0]}")
    await cb_article(second, get_news_service(), config)
    after = [(b.text, b.url, b.callback_data)
             for row in second.message.last_kwargs["reply_markup"].inline_keyboard for b in row]
    assert any(d and d.startswith("d:") for _t, _u, d in after), \
        f"配了 key 就该给出深度分析：{[b[2] for b in after]}"


@pytest.mark.asyncio
async def test_deep_summary_uses_the_strong_model(seeded, fake_llm):
    from app.bot.handlers.summary import cmd_summary

    message = FakeMessage(ALLOWED, f"/summary {seeded[0]}")
    search = SearchService(get_config(), get_news_service(), fake_llm)
    await cmd_summary(message, Command(str(seeded[0])), get_news_service(), search, get_config())
    assert "行业影响" in message.all_text() or "核心内容" in message.all_text()
    assert "deep_analyze" in fake_llm.calls
    # 真的跑了强模型，「📄 常规摘要」才是一个能给出新内容的按钮（v1.78 的反方向）
    data = [b.callback_data for r in message.sent[-1][1]["reply_markup"].inline_keyboard
            for b in r if getattr(b, "callback_data", None)]
    assert f"a:{seeded[0]}" in data, data


def _seed_today(session, rows: int) -> None:
    """`rows` 条今天的新闻，分数各不相同，所以翻页的结果是确定的。

    `published_at` 全部用"此刻"而不是往前推几小时：往前推会让用例在
    接近当地午夜运行时漂到昨天去（CI 是 UTC，开发机 +08:00）。
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as s:
        for i in range(rows):
            art = repo.save_article(s, build_article(
                title=f"OpenAI announces model release {i}",
                url=f"https://example.org/todayday{i}", source_name="OpenAI",
                source_type="rss", content="OpenAI announces a model. " * 12,
                published_at=now))
            if art:
                art.is_processed = True
                art.filtered_out = False
                art.final_score = 50.0 + i
                art.source_quality = 90.0
    s.commit()


@pytest.mark.asyncio
async def test_today_says_how_many_there_really_are_and_how_to_see_the_rest(session):
    """/today 以前给 20 条就结束：真机当天符合条件 101 条，他没有任何线索知道少了 81 条。"""
    from app.bot.handlers.news import cmd_today

    _seed_today(session, 25)
    msg = FakeMessage(ALLOWED, "/today")
    await cmd_today(msg, Command(None), get_news_service(), get_config())
    text = msg.all_text()
    assert "共 25 条" in text, text
    assert "这批 20 条的第 1/2 页" in text, text
    assert "更多请用 /today 2" in text, f"标题得给他下一页的入口：{text}"


@pytest.mark.asyncio
async def test_news_and_latest_admit_they_are_one_page_of_many(session):
    """/news 与 /latest 也属于同一族：72 小时里 729 条，标题里以前一个数字都没有。"""
    from app.bot.handlers.news import cmd_latest, cmd_news

    _seed_today(session, 13)
    msg = FakeMessage(ALLOWED, "/news")
    await cmd_news(msg, Command(None), get_news_service(), get_config())
    text = msg.all_text()
    assert "共 13 条" in text, text
    assert "这里列出最新 10 条" in text, text
    assert "/news 30" in text, text

    later = FakeMessage(ALLOWED, "/latest")
    await cmd_latest(later, get_news_service(), get_config())
    assert "共 13 条" in later.all_text(), later.all_text()


@pytest.mark.asyncio
async def test_page_two_keeps_the_lists_own_name_not_a_generic_one(session, monkeypatch):
    """➡️ 以前把每一页都重写成"🤖 AI 新闻"：`/today` 的第 2 页顶着别人的名字，
    而 v1.84 刚加上的"共 25 条"在第二页上消失了。"""
    from app.bot.handlers.news import cb_page, cmd_today

    _seed_today(session, 25)
    msg = FakeMessage(ALLOWED, "/today")
    await cmd_today(msg, Command(None), get_news_service(), get_config())
    keyboard = msg.sent[-1][1]["reply_markup"]
    pages = [b.callback_data for r in keyboard.inline_keyboard for b in r
             if getattr(b, "callback_data", None) and b.callback_data.startswith("p:")]
    assert pages, f"20 条应该给出第 2 页的按钮：{pages}"

    callback = FakeCallback("p:2")
    await cb_page(callback, get_news_service(), get_config())
    edited = callback.message.last
    assert "今日 AI 新闻" in edited, edited[:160]
    assert "共 25 条" in edited, edited[:160]
    assert "这批 20 条的第 2/2 页" in edited, edited[:200]
    assert "🤖 AI 新闻" not in edited, f"第 2 页不能换名单：{edited[:160]}"


@pytest.mark.asyncio
async def test_an_expired_panel_says_so_instead_of_passing_as_the_same_list(session, monkeypatch):
    """记忆 6 小时（重启也清空）。点旧消息的 ➡️ 时以前会悄悄换成"最近 72 小时"，
    却继续用原标题展示——他点的是某条消息第 2 页，拿到的是另一个列表第 1 页。"""
    from app.bot.context import store
    from app.bot.handlers.news import cb_page

    _seed_today(session, 30)
    store._store.clear()                      # 模拟过期/重启
    callback = FakeCallback("p:3")
    await cb_page(callback, get_news_service(), get_config())
    text = callback.message.last
    assert "已经过期" in text, text[:200]
    assert "最近 72 小时" in text, text[:200]
    assert "第 3" not in text, f"重新取的就是新的一页，不该谎称第 3 页：{text[:200]}"


@pytest.mark.asyncio
async def test_the_topic_page_states_how_many_the_category_really_has(session):
    """/topics 点进去的分类页是这一族里最后一块：它给 10 条，而分类里可能有 37 条。"""
    from app.bot.handlers.news import cb_topic
    from app.database.models import Article

    # 14 条：其中 2 条低于门槛、一对是同一事件的两条报道 → 能看到 11 条，本页只给 10 条
    _seed_today(session, 14)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as s:
        for i in range(3):
            art = repo.save_article(s, build_article(
                title=f"Control group row {i}", url=f"https://example.org/ctrl{i}",
                source_name="GitHub", source_type="rss",
                content="Control group body text. " * 12, published_at=now))
            if art:
                art.is_processed = True
                art.final_score = 60.0
        s.commit()
    with session_scope() as s:
        rows = s.query(Article).filter(Article.title.like("OpenAI announces model%")).all()
        for row in rows:
            row.category = "AI Models"
        # 另两个分类做对照：`count_category` 如果漏掉分类过滤，总数就会把别人家也算进来
        others = s.query(Article).filter(Article.title.like("Control group%")).all()
        for row in others:
            row.is_processed = True
            row.filtered_out = False
            row.final_score = 60.0
            row.source_quality = 90.0
            row.category = "Open Source"
        s.commit()
        rows = s.query(Article).filter(Article.title.like("OpenAI announces model%")).all()
        for i, score in ((0, 10.0), (1, 20.0)):          # 门槛之下的两条
            rows[i].final_score = score
        from app.processing import deduplicate
        event = repo.get_or_create_event(s, deduplicate.make_event_key(rows[2].title),
                                         rows[2].title, rows[2])
        for r in (rows[2], rows[3]):                      # 两条报道 = 一个事件（都要挂上）
            r.event_id = event.id
        s.commit()

    news = get_news_service()
    # 期望值从 fixture 自己算出来，不靠我心算：门槛之上的行，按事件去重后有几条？
    floor = news.category_min_score()
    with session_scope() as s:
        rows = s.query(Article).filter(Article.category == "AI Models").all()
        eligible = [r for r in rows if (r.final_score or 0) >= floor and not r.filtered_out]
        distinct = {r.event_id or r.id for r in eligible}
    assert len(eligible) < 14, f"低于门槛的两条不该进来：{len(eligible)}"
    assert len(distinct) < len(eligible), "fixture 里必须真的有一对重复事件，否则这条测试是空跑"

    visible = len(news.by_category("AI Models", limit=100, days=7))
    total = news.count_category("AI Models", days=7)
    assert visible == len(distinct), (visible, len(distinct))
    assert total == visible, f"标题的总数必须等于列表能给的：{total} vs {visible}"

    callback = FakeCallback("t:AI Models")
    await cb_topic(callback, news, get_config())
    text = callback.message.last
    assert f"共 {total} 条" in text, text[:220]
    assert "这里列出最新 10 条" in text, text[:220]
    assert "更多请用 /search 关键词" in text, text[:220]
    assert "近 7 天" in text, text[:120]


@pytest.mark.asyncio
async def test_the_second_page_is_a_different_slice_not_the_same_ones(session):
    _seed_today(session, 25)
    news = get_news_service()
    first = news.day(offset_days=0, limit=20, page=1)
    second = news.day(offset_days=0, limit=20, page=2)
    assert len(first.items) == 20 and len(second.items) == 5, (len(first.items), len(second.items))
    assert not ({a.id for a in first.items} & {a.id for a in second.items}), "第二页不能是同一批"
    assert first.total == second.total == 25, (first.total, second.total)
    assert first.pages == 2 and second.pages == 2, (first.pages, second.pages)
    scores = [round(a.final_score) for a in second.items]
    assert scores == sorted(scores, reverse=True), f"页内按分数从高到低：{scores}"
    assert max(round(a.final_score) for a in second.items) < min(
        round(a.final_score) for a in first.items), "第一页必须整体高分于第二页"


@pytest.mark.asyncio
async def test_the_placeholder_promises_only_what_it_can_deliver(seeded, fake_llm, monkeypatch):
    """占位那一句也是一次承诺：没配 key 时说「正在深入分析」，下一句必然是做不到。"""
    from app.bot.handlers.summary import cmd_summary

    config = get_config()
    message = FakeMessage(ALLOWED, f"/summary {seeded[0]}")
    await cmd_summary(message, Command(str(seeded[0])), get_news_service(),
                      SearchService(config, get_news_service()), config)
    placeholder = message.sent[0][0]
    assert "正在" not in placeholder, placeholder
    assert "没配 LLM" in placeholder, placeholder
    # 这条回帖本身就是卡片，所以「📄 常规摘要」会是第二个点了没有新内容的按钮（v1.78）
    data = [b.callback_data for r in message.sent[1][1]["reply_markup"].inline_keyboard
            for b in r if getattr(b, "callback_data", None)]
    assert f"a:{seeded[0]}" not in data, data
    assert any(d == "b:news" for d in data), data

    monkeypatch.setattr(config.settings, "llm_base_url", "https://example.org/v1")
    monkeypatch.setattr(config.settings, "llm_api_key", "sk-test")
    monkeypatch.setattr(config.settings, "llm_model", "some-model")
    second = FakeMessage(ALLOWED, f"/summary {seeded[0]}")
    await cmd_summary(second, Command(str(seeded[0])), get_news_service(),
                      SearchService(config, get_news_service(), fake_llm), config)
    assert "正在" in second.sent[0][0], second.sent[0][0]


@pytest.mark.asyncio
async def test_a_stale_deep_callback_is_told_what_this_machine_can_do(seeded):
    """🧠 已经不渲染了，但旧消息里的过期回调仍会走到 cb_deep——toast 不能承诺做不到的事。"""
    from app.bot.handlers.news import cb_deep

    callback = FakeCallback(f"d:{seeded[0]}")
    await cb_deep(callback, get_news_service(),
                  SearchService(get_config(), get_news_service()), get_config())
    assert callback.answers[0][0] == "这台机器没配 LLM，只能给规则摘要", callback.answers
    body = callback.message.sent[-1][0]
    assert "上一条卡片" in body, body
    data = [b.callback_data for r in callback.message.last_kwargs["reply_markup"].inline_keyboard
            for b in r if getattr(b, "callback_data", None)]
    assert f"a:{seeded[0]}" not in data, f"这条解释上面没有卡片，但也不该给一个重发卡片的按钮：{data}"
    assert any(d == "b:news" for d in data), data


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
    assert "模型发布" in message.last, "栏目名给用户看的是中文"
    assert "AI Models" not in message.last, "taxonomy 的英文键不该露出来"
    keyboard = message.last_kwargs["reply_markup"]
    labels = [b.text for row in keyboard.inline_keyboard for b in row if b.text]
    assert any("模型发布" in text for text in labels), labels
    assert not any("AI Models" in text for text in labels), labels
    data = [b.callback_data for row in keyboard.inline_keyboard for b in row if b.callback_data]
    assert any(d.startswith("t:") for d in data), "回调仍然带英文键"

    topic_page = FakeCallback("t:AI Models")
    from app.bot.handlers.news import cb_topic
    from app.config import get_config
    await cb_topic(topic_page, get_news_service(), get_config())
    assert "AI Models" not in topic_page.message.last, topic_page.message.last

    other = FakeMessage(ALLOWED, "/sources")
    await cmd_sources(other, get_news_service(), get_config())
    assert "信息来源" in other.last and "OpenAI" in other.last
    assert "RSS 新闻源" in other.last and "A 级（一手官方）" in other.last
    # 旧格式是内部取值直接拼接："rss · A 级"
    assert not re.search(r"\brss\b ·", other.last), "采集器类型的内部取值不该直接印出来"


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
async def test_a_long_chat_answer_is_delivered_clipped_not_lost(seeded, fake_llm, monkeypatch):
    """/news、/免费、简报都 clip 过；聊天回答是唯一没有的那一条。"""
    from app.bot.handlers.chat import free_text
    from app.services import format as fmt
    from app.services.digest import DigestService
    from app.services.search import AgentAnswer, SearchService

    async def long_answer(self, question, **kwargs):
        return AgentAnswer(text="这是一句很长的回答。" * 1200, used_ids=[],
                           intent="search", query=question)

    monkeypatch.setattr(SearchService, "answer", long_answer)
    message = FakeMessage(ALLOWED, "最近 AI Agent 有什么值得关注的？")
    await free_text(message, get_news_service(), SearchService(get_config()),
                    DigestService(get_config()), get_config())
    sent = message.last or ""
    assert "内容过长已截断" in sent, sent[-40:]
    assert fmt.utf16_len(sent) <= fmt.SAFE_LIMIT, fmt.utf16_len(sent)


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


@pytest.mark.asyncio
async def test_no_model_answers_with_the_chinese_it_already_has(seeded, session):
    """没有 LLM 时的问答走规则列表路径——它曾经直接打印英文标题。

    `ensure_chinese` 就调在上一行，中文已经在库里，渲染却用 `a.summary or a.title`，
    所以他每天看到的问答列表是英文的。
    """
    from sqlalchemy import select

    from app.database.models import Article
    from app.services.search import SearchService

    class NoModel:
        enabled = False

    for row in session.scalars(select(Article)):
        row.title_zh = f"中文标题{row.id}"
        row.summary_zh = "中文一句话总结"
    session.commit()

    search = SearchService(get_config(), get_news_service(), NoModel())
    answer = await search.answer("最近 AI Agent 有什么值得关注的？", chat_id=ALLOWED)
    assert "未配置 AI 模型" in answer.text
    # display_line 优先中文摘要（与简报同一套规则），所以这里出现的是中文总结行
    assert "中文一句话总结" in answer.text, answer.text[:300]
    assert "releases" not in answer.text.lower(), "库里已有中文时不该再出现英文标题"


def test_refusal_message_is_polite_and_informative():
    assert "ALLOWED_CHAT_IDS" in REFUSAL


def test_every_documented_command_has_a_handler_and_its_services_are_injected():
    """接线检查：路由缺一个 handler，aiogram 要到第一条 update 才报错。

    顺带检查服务注入：aiogram 是按参数名注入的，/sources 这次多了 `app_config`
    形参——单元测试都直接调 handler 并显式传参，拼错了也全绿，线上却是死按钮。
    两者共用一个 Dispatcher：aiogram 的 Router 只能挂一次，建两次会直接抛错。
    """
    import inspect

    from app.bot.bot import COMMANDS, create_dispatcher
    from app.config import AppConfig
    from app.services.digest import DigestService
    from app.services.llm import LLMService
    from app.services.news import NewsService
    from app.services.search import SearchService

    service_types = (AppConfig, NewsService, SearchService, DigestService, LLMService)
    # 处理器模块都是 `from __future__ import annotations`，注解在运行时是字符串，
    # 所以只能按名字比对，不能 isinstance。
    service_names = {t.__name__ for t in service_types}
    dp = create_dispatcher(get_config())
    available = set(dp.workflow_data)
    names: set[str] = set()
    injected: list[str] = []

    def annotation_name(param: inspect.Parameter) -> str:
        ann = param.annotation
        return ann.__name__ if isinstance(ann, type) else str(ann).rsplit(".", 1)[-1]

    def walk(router):
        for observer in (router.message, router.callback_query):
            for handler in observer.handlers:
                callback = getattr(handler, "callback", None)
                if callback is None:
                    continue
                names.add(getattr(callback, "__name__", str(callback)))
                for param_name, param in inspect.signature(callback).parameters.items():
                    if annotation_name(param) in service_names:
                        assert param_name in available, \
                            f"{callback.__name__} 需要 {param_name!r}，dispatcher 没注册"
                        injected.append(param_name)
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
    assert len(injected) >= 5, f"只检查到 {len(injected)} 处服务注入，检查本身可能失效"
    assert {"app_config", "news", "search", "digest", "llm"} <= available


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


# ------------------------------------------------- 回调：源码里那两个真缺陷
def _seed_many(count: int) -> list[int]:
    from datetime import timedelta

    ids: list[int] = []
    with session_scope() as s:
        for i in range(count):
            title = f"Model release {i} tops the reasoning benchmark for AI agents"
            data = build_article(
                title=title, url=f"https://example.org/many-{i}", source_name="OpenAI",
                source_type="rss", content=f"{title}. " + ("Details about the AI model release. " * 6),
                published_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=i),
                quality="A",
            )
            article = repo.save_article(s, data)
            assert article is not None
            article.is_processed = True
            article.final_score = 60.0 + i / 100
            ids.append(article.id)
        s.commit()
    return ids


@pytest.mark.asyncio
async def test_sources_page_is_an_honest_status_table_without_buttons(seeded):
    """`/sources` 是一页只读状态表：没有按钮，也不该假装有。

    以前这里有一份 `sources_keyboard`（每行都是 `b:sources`）但从没被调用过，
    而它唯一的回调分支还漏传了 `cmd_sources` 的第三个参数 —— 一旦被接上就是 TypeError。
    v1.35 把这份死代码删掉，这条测试负责证明它没有以"看起来能用"的形式回来。
    """
    from app.bot.handlers.news import cmd_sources

    message = FakeMessage(ALLOWED, "/sources")
    await cmd_sources(message, get_news_service(), get_config())
    assert "信息来源" in message.last and "RSS 新闻源" in message.last
    assert message.last_kwargs.get("reply_markup") is None, "只读页不该挂一排点了没用的按钮"


def test_the_dead_sources_keyboard_is_gone_from_the_tree():
    """源码扫描：删掉的死功能不能被重新接回去（grep 比记忆可靠）。

    只看**调用点**——news.py 的注释里写着 `sources_keyboard` 是为了解释为什么删掉它，
    那不算引用；第一版按整文件子串匹配，被自己的注释判成了"又接回去了"。
    """
    root = Path(__file__).resolve().parent.parent
    markers = ("K.sources_keyboard", "def sources_keyboard", 'cb(BACK, "sources")')
    hits = []
    for path in list((root / "app").rglob("*.py")) + list((root / "scripts").rglob("*.py")):
        code_lines = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lstrip().startswith("#"):
                continue
            code_lines.append(line.split("  # ")[0])
        if any(marker in line for line in code_lines for marker in markers):
            hits.append(path.name)
    assert hits == [], f"被删掉的来源键盘又被接回去了：{hits}"


@pytest.mark.asyncio
async def test_an_unknown_back_tag_falls_back_to_the_news_list_with_a_log(seeded):
    """没见过的回路标签不该静默，也不该炸：给他一份能看的列表，日志里留一行。"""
    from app.bot.handlers import news as news_handlers
    from app.bot.handlers.news import cb_back

    logged: list[str] = []

    class Recorder:
        def debug(self, *a):
            return None

        def info(self, msg, *args):
            logged.append(str(msg % args if args else msg))

        warning = error = exception = info

    original = news_handlers.log
    news_handlers.log = Recorder()
    try:
        callback = FakeCallback("b:sources")
        await cb_back(callback, get_news_service(), get_config())
    finally:
        news_handlers.log = original
    assert any("unknown back tag" in m and "sources" in m for m in logged), logged
    assert "最新 AI 新闻" in callback.message.last, "回退也要给他一个列表"


@pytest.mark.asyncio
async def test_a_callback_without_a_message_creates_no_phantom_subscriber(seeded):
    """`user_for(0)` / `user_for(from_user.id)` 是订阅者表里假行的来源。"""
    from app.bot.handlers.news import (cb_article, cb_back, cb_page, cb_topic)

    ids = _seed_many(2)
    news = get_news_service()
    for data in ("p:2", "b:news", f"a:{ids[0]}", "t:AI Models"):
        callback = FakeCallback(data)
        callback.message = None
        await {"p": cb_page, "b": cb_back, "a": cb_article, "t": cb_topic}[data.split(":")[0]](
            callback, news, get_config())
        assert "/news" in callback.answers[-1][0], (data, callback.answers)
        assert "失效" in callback.answers[-1][0] or "不可用" in callback.answers[-1][0]
    with session_scope() as s:
        assert repo.get_user(s, 0) is None, "chat 0 不该成为一个订阅者"
        assert repo.get_user(s, 42) is None, "from_user.id 是用户号，不是聊天号"
    assert get_news_service().user_for(ALLOWED)["chat_id"] == ALLOWED


@pytest.mark.asyncio
async def test_page_two_numbers_match_page_two_buttons(seeded):
    """列表上的编号就是按钮，翻页后必须重新从 1️⃣ 开始，且只露出这一页的条数。"""
    from app.bot.handlers.news import cb_page, show_list

    ids = _seed_many(12)
    news = get_news_service()
    items = [news.by_id(i) for i in ids]
    first = FakeMessage(ALLOWED, "/news")
    await show_list(first, chat_id=ALLOWED, items=[i for i in items if i],
                    title="🤖 最新 AI 新闻", news=news, config=get_config())
    keyboard = first.last_kwargs["reply_markup"]
    page1_ids = [b.callback_data for r in keyboard.inline_keyboard for b in r
                 if (b.callback_data or "").startswith("a:")]
    assert len(page1_ids) == 10, page1_ids

    callback = FakeCallback("p:2")
    await cb_page(callback, news, get_config())
    rendered = callback.message.last
    keyboard2 = callback.message.last_kwargs["reply_markup"]
    page2_ids = [b.callback_data for r in keyboard2.inline_keyboard for b in r
                 if (b.callback_data or "").startswith("a:")]
    assert page2_ids == [f"a:{i}" for i in ids[10:]], page2_ids
    assert "1️⃣" in rendered and "2️⃣" in rendered and "3️⃣" not in rendered, rendered


@pytest.mark.asyncio
async def test_an_empty_category_answers_in_chinese_with_the_label(seeded):
    from app.bot.handlers.news import cb_topic

    callback = FakeCallback("t:Research")     # 真存在的栏目键，最近 7 天在测试库里没有行
    await cb_topic(callback, get_news_service(), get_config())
    text = callback.message.last
    assert "论文与方法" in text and "还没有新闻" in text, text
    assert "Research" not in text, "分类名要出中文，不是数据库里的内部键"


# ------------------------------------------------ 处理器崩溃时他看见什么
class FakeBot:
    """挂在 aiogram 对象的 bot 上下文上，记录它被要求做的方法。"""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[Any] = []
        self.fail = fail

    async def __call__(self, method, **_kwargs):
        self.calls.append(method)
        if self.fail:
            raise RuntimeError("api.telegram.org 现在连不上")


def _mounted(obj, bot: FakeBot):
    payload = obj.model_dump()
    return type(obj).model_validate(payload, context={"bot": bot})


def _callback(bot: FakeBot):
    cb = CallbackQuery(
        id="cb-1",
        from_user=TgUser(id=ALLOWED, is_bot=False, first_name="Tester"),
        chat_instance="ci",
        data="a:1",
        message=None,
    )
    return _mounted(cb, bot)


def _message(bot: FakeBot, *, chat_id: int = ALLOWED, text: str = "/news"):
    msg = TgMessage(
        message_id=7,
        date=datetime.now(timezone.utc),
        chat=TgChat(id=chat_id, type="private"),
        from_user=TgUser(id=chat_id, is_bot=False, first_name="Tester"),
        text=text,
    )
    return _mounted(msg, bot)


# ------------------------------------------- 处理器崩溃：必须走真实 dispatcher
def _error_event(target, exc):
    """aiogram 3.15 是把错误包成一个 ErrorEvent 送进来的 —— 签名不对就整个失效。"""
    from aiogram.types import ErrorEvent, Update

    if isinstance(target, CallbackQuery):
        update = Update(update_id=1, callback_query=target)
    else:
        update = Update(update_id=1, message=target)
    return ErrorEvent(update=update, exception=exc)


@pytest.mark.asyncio
async def test_a_crashed_callback_still_answers_so_the_button_stops_spinning():
    """回调不 answer，Telegram 会让那个按钮转圈转满一分钟。"""
    from app.bot.bot import on_error

    bot = FakeBot()
    cb = _mounted(CallbackQuery(id="cb-1", from_user=TgUser(id=ALLOWED, is_bot=False,
                      first_name="Tester"), chat_instance="ci", data="a:1", message=None), bot)
    assert await on_error(_error_event(cb, ValueError("article 12 has no url"))) is True
    assert len(bot.calls) == 1, bot.calls
    sent = bot.calls[0]
    assert type(sent).__name__ == "AnswerCallbackQuery"
    assert "这一步失败了" in (sent.text or "") and "article 12 has no url" in (sent.text or "")


@pytest.mark.asyncio
async def test_a_crashed_command_replies_once_with_the_error_type():
    from app.bot.bot import on_error

    bot = FakeBot()
    msg = _message(bot)
    assert await on_error(_error_event(msg, KeyError("final_score"))) is True
    assert len(bot.calls) == 1, bot.calls
    sent = bot.calls[0]
    assert type(sent).__name__ == "SendMessage"
    assert "KeyError" in (sent.text or "") and "处理失败" in (sent.text or "")


@pytest.mark.asyncio
async def test_the_error_report_cannot_take_the_bot_down_too():
    """报告失败本身如果抛出，一条坏更新就变成两条 —— 那才是真的把轮询带下水。"""
    from aiogram.types import Update

    from app.bot.bot import on_error

    assert await on_error(_error_event(_callback(FakeBot(fail=True)), ValueError("boom"))) is True
    assert await on_error(_error_event(_message(FakeBot(fail=True)), ValueError("boom"))) is True
    plain = Update(update_id=9)
    from aiogram.types import ErrorEvent

    assert await on_error(ErrorEvent(update=plain, exception=ValueError("boom"))) is True


@pytest.mark.asyncio
async def test_the_real_dispatcher_wires_the_error_handler_at_all():
    """这条是修 bug 的理由：签名错了的话，上面那一整组用例照样全绿。

    只调用 `on_error(...)` 是在验证我自己的假设；把更新喂给真的 `Dispatcher`
    才会经过 aiogram 的调用约定 —— 线上那句
    `TypeError: on_error() missing 1 required positional argument: 'exception'`
    就是这么露出来的。这里不复用 build_router()（那是模块级 Router 单例，
    第二次挂到新 Dispatcher 上会 RuntimeError），只挂一个会抛的小处理器。
    """
    from aiogram import Bot, Dispatcher
    from aiogram.client.session.base import BaseSession
    from aiogram.methods import AnswerCallbackQuery
    from aiogram.types import Update

    from app.bot.bot import on_error

    class CapturingSession(BaseSession):
        def __init__(self) -> None:
            super().__init__()
            self.sent: list[Any] = []

        async def close(self) -> None:
            return None

        async def make_request(self, bot, method, timeout=None):
            self.sent.append(method)
            return SimpleNamespace(ok=True, result=None)

        async def stream_content(self, *args, **kwargs):
            yield b""

    dp = Dispatcher()
    bot = Bot(token="123456:LOCAL-TEST-TOKEN", session=CapturingSession())

    async def explodes(callback):
        raise RuntimeError("sqlite 读不到这一行")

    dp.callback_query.register(explodes)
    dp.errors.register(on_error)

    cb = CallbackQuery(
        id="wire-1", from_user=TgUser(id=ALLOWED, is_bot=False, first_name="Tester"),
        chat_instance="ci", data="a:999999",
        message=TgMessage(message_id=1, date=datetime.now(timezone.utc),
                          chat=TgChat(id=ALLOWED, type="private"), text=None),
    )
    update = Update.model_validate(Update(update_id=1, callback_query=cb).model_dump(),
                                   context={"bot": bot})
    await dp.feed_update(bot, update)          # 旧签名会在这里把 TypeError 抛出来

    kinds = [type(m).__name__ for m in bot.session.sent]
    assert "AnswerCallbackQuery" in kinds, f"错误处理器没被走通：{kinds}"
    toast = [m for m in bot.session.sent if isinstance(m, AnswerCallbackQuery)][0]
    assert "sqlite 读不到这一行" in (toast.text or ""), toast.text


# ------------------------------------- 白名单边界：陌生人看到什么、被记多少
class _NobodyAllowed:
    """替身 config：白名单为空 = fail-closed。

    不能去改 `get_config()` 那个缓存单例 —— 第一版那么写了，于是这条用例把全局
    配置洗成"谁都不许"，全量跑时打挂了 `test_sender.py` 里依赖白名单的用例，
    而单跑本模块还是绿的。
    """

    class settings:
        @staticmethod
        def is_allowed_chat(chat_id):
            return False


@pytest.mark.asyncio
async def test_a_stranger_callback_is_answered_so_the_button_does_not_spin():
    """未授权回调不 answer 的话，Telegram 会让那个按钮转圈转满一分钟。"""
    from app.bot.middleware import AccessMiddleware

    calls: list[str] = []

    async def handler(event, data):
        calls.append("ran")

    bot = FakeBot()
    result = await AccessMiddleware(_NobodyAllowed())(handler, _callback(bot), {})
    assert result is None and calls == []
    assert len(bot.calls) == 1 and type(bot.calls[0]).__name__ == "AnswerCallbackQuery"
    assert bot.calls[0].text == "未授权"


@pytest.mark.asyncio
async def test_the_refusal_is_sent_once_and_then_cools_down(monkeypatch):
    """一次告知（把 chat id 说清楚才可能自己加白名单），但不陪聊。"""
    from app.bot import middleware as mw_module
    from app.bot.middleware import NOTICE_COOLDOWN, AccessMiddleware

    mw_module._denied_notices.clear()
    clock = {"t": 1_700_000_000.0}
    monkeypatch.setattr(mw_module.time, "time", lambda: clock["t"])

    async def handler(event, data):
        return "ran"

    mw = AccessMiddleware(_NobodyAllowed())
    first = FakeBot()
    await mw(handler, _message(first, chat_id=STRANGER), {})
    assert len(first.calls) == 1 and "不在白名单里" in (first.calls[0].text or "")
    assert str(STRANGER) in (first.calls[0].text or ""), "拒绝里要带上他自己的 chat id"
    assert "<code>" in (first.calls[0].text or ""), "要能直接复制那串数字"

    second = FakeBot()
    await mw(handler, _message(second, chat_id=STRANGER), {})
    assert second.calls == [], "冷却期内不该再回第二次"

    clock["t"] += NOTICE_COOLDOWN + 1
    third = FakeBot()
    await mw(handler, _message(third, chat_id=STRANGER), {})
    assert len(third.calls) == 1, "冷却过了可以再提醒一次"
    mw_module.silence_notice(STRANGER)
    assert STRANGER not in mw_module._denied_notices


@pytest.mark.asyncio
async def test_the_notice_bookkeeping_is_bounded():
    """陌生人扫描时这个字典不能无限长大（它只是"最近提醒过谁"）。"""
    from app.bot import middleware as mw_module

    mw_module._denied_notices.clear()          # 模块级共享，别的用例会留残留
    for i in range(mw_module.NOTICE_MEMORY):
        mw_module._remember_notice(900_000 + i, 1.0)
    assert len(mw_module._denied_notices) == mw_module.NOTICE_MEMORY
    mw_module._remember_notice(999_999, 2.0)
    assert len(mw_module._denied_notices) == 1, "满了该重开，而不是继续长"
    mw_module._denied_notices.clear()


@pytest.mark.asyncio
async def test_logging_middleware_re_raises_and_only_keeps_the_first_line():
    from app.bot import middleware as mw_module
    from app.bot.middleware import LoggingMiddleware

    records: list[tuple[str, str]] = []

    class Recorder:
        def debug(self, msg, *args):
            records.append(("debug", str(msg % args if args else msg)))

        def exception(self, msg, *args):
            records.append(("exception", str(msg % args if args else msg)))

    monkeypatch_log = Recorder()
    original = mw_module.log
    mw_module.log = monkeypatch_log
    try:
        async def explodes(event, data):
            raise ValueError("boom")

        two_lines = "第一行\n第二行是长正文，不该整段进日志"
        with pytest.raises(ValueError):
            await LoggingMiddleware()(explodes, _message(FakeBot(), text=two_lines), {})
    finally:
        mw_module.log = original

    assert any(lvl == "exception" for lvl, _ in records), "错误必须原样抛出并留下记录"
    debug_lines = [m for lvl, m in records if lvl == "debug"]
    assert any("第一行" in m for m in debug_lines), debug_lines
    assert not any("第二行" in m for m in debug_lines), "文档说只留一行，测试就得盯着它"


# ------------------- 序数解析：命令那半边以前只认裸的「一..十」
def test_summary_resolves_the_ordinal_phrasings_its_own_hint_teaches(seeded):
    """/summary 的提示语写着「第二条详细说说」，可 `/summary 第二条` 以前只回用法说明。

    自然语言那一半有完整解析（第…条、二十以上），命令这半抄了一份只认裸 一..十 的表。
    序号断言刻意用 90001.. 这种合成 id：早期版本用 seeded 比对，而 seeded[1] 正好等于 2，
    「第2条 被当成新闻编号 2」这条断言就成了永远为真的空断言（变异实验抓出来的）。
    """
    from app.bot.context import store
    from app.bot.handlers.summary import _resolve_id

    ids = list(range(90001, 90011))
    store.remember(ALLOWED, ids, kind="news")
    assert _resolve_id(ALLOWED, "第二条") == ids[1]
    assert _resolve_id(ALLOWED, "第二") == ids[1]
    assert _resolve_id(ALLOWED, "第一条") == ids[0]
    assert _resolve_id(ALLOWED, "第 2 条") == ids[1]
    assert _resolve_id(ALLOWED, "第2条") == ids[1], "带数字的序数不该被当成新闻编号"
    assert _resolve_id(ALLOWED, "第9条") == ids[8]

    store.remember(ALLOWED, list(seeded), kind="news")
    assert _resolve_id(ALLOWED, str(seeded[0])) == seeded[0]
    assert _resolve_id(ALLOWED, f"#{seeded[0]}") == seeded[0]
    assert _resolve_id(ALLOWED, "随便说说") is None
    assert _resolve_id(ALLOWED, None) is None


@pytest.mark.parametrize("phrase,number", [
    ("三", 3), ("第三条", 3), ("第三", 3), ("第3条", 3), ("第十", 10), ("第十条", 10),
    ("第十一条", 11), ("第十二条", 12), ("第二十条", 20), ("第三十五条", 35),
    ("第九十", 90), ("第二十一条", 21), ("十", 10), ("一百", None),
])
def test_the_command_and_the_natural_language_path_read_the_same_number(phrase, number):
    """同一个中文序数在两个入口必须是同一个数字——这是那次分叉的根源。"""
    from app.bot.context import store
    from app.bot.handlers.summary import _resolve_id
    from app.services.search import ordinal_to_int

    ids = list(range(90001, 90141))
    store.remember(ALLOWED, ids, kind="news")
    assert ordinal_to_int(phrase) == number, phrase
    if number:
        assert _resolve_id(ALLOWED, phrase) == ids[number - 1], phrase
    else:
        assert _resolve_id(ALLOWED, phrase) is None, phrase


def test_ordinal_parsing_refuses_nonsense():
    from app.services.search import ordinal_to_int

    assert ordinal_to_int(None) is None
    assert ordinal_to_int("") is None
    assert ordinal_to_int("第十一条啊") == 11
    assert ordinal_to_int("你好") is None
    assert ordinal_to_int(True) is None, "bool 不该被当成 1"
    assert ordinal_to_int(0) is None
