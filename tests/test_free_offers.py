"""/免费 - 免费资讯识别、检索与展示。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_config
from app.database import repository as repo
from app.database.models import Article
from app.processing import free_offers
from app.services import format as fmt


def detect(*parts: str) -> free_offers.FreeOffer | None:
    return free_offers.detect(*parts, config=get_config())


# ------------------------------------------------------------- detection
@pytest.mark.parametrize("text,tool", [
    ("opencode 现在可以免费使用 DeepSeek V3 模型，限时一个月", "opencode"),
    ("Cursor offers a free Pro week for all students", "Cursor"),
    ("Qoder: 限时免费开放 Claude 与 GPT-5 模型", "Qoder"),
    ("Zcode is now free to use, including the GLM-4.6 model", "Zcode"),
    ("OpenRouter: DeepSeek R1 is free for a limited time", "OpenRouter"),
    ("硅基流动送额度，注册即送免费 tokens 用于 Qwen3", "SiliconFlow"),
])
def test_real_free_offers_are_detected(text, tool):
    offer = detect(text)
    assert offer is not None, f"missed: {text}"
    assert offer.tool == tool
    assert offer.signals and offer.confidence > 0.4


@pytest.mark.parametrize("text", [
    "Meta introduces camera-free AI glasses",
    "Introducing Gemma 4 12B: a unified, encoder-free multimodal model",
    "Feel free to open a pull request",
    "An error-free build of the DeepSeek tokenizer",
    "This library is free software under the MIT licence",
    "A totally unrelated story about GPU pricing going up",
])
def test_false_friends_are_not_free_offers(text):
    assert detect(text) is None, f"false positive: {text}"


def test_free_without_a_subject_is_not_reported():
    assert detect("This API is now completely free") is None
    assert detect("100 free GPUs for research") is None


def test_kind_and_model_extraction():
    offer = detect("opencode adds free access to DeepSeek V3 for every user")
    assert offer.kind == "编程 Agent/IDE"
    assert "deepseek" in [m.lower() for m in offer.models]

    platform = detect("Groq now offers a free tier for Llama models")
    assert platform.kind == "API/平台"


def test_expiry_hint_is_captured():
    offer = detect("Cursor Pro is free until October 15 for students")
    assert offer.expiry and "ctober" in offer.expiry


def test_vocabulary_is_configurable():
    """A new tool only needs config/free_offers.yaml, not code."""
    config = get_config()
    config.free["tools"]["MyNewAgent"] = ["mynewagent"]
    config.free["kind_map"]["编程 Agent/IDE"].append("MyNewAgent")
    try:
        offer = detect("mynewagent 今天开始免费了")
        assert offer is not None and offer.tool == "MyNewAgent"
        assert offer.kind == "编程 Agent/IDE"
    finally:
        config.free["tools"].pop("MyNewAgent")
        config.free["kind_map"]["编程 Agent/IDE"].remove("MyNewAgent")


# --------------------------------------------------------- storage + query
def seed_free(session, title: str, url: str, *, source: str = "Reddit LocalLLaMA RSS") -> int:
    from app.processing.normalize import build_article

    # body deliberately has no "free" wording: only the title may carry the claim
    data = build_article(title=title, url=url, source_name=source,
                         content=title + ". " + ("More detail about this release. " * 4),
                         published_at=datetime.now(timezone.utc).replace(tzinfo=None))
    article = repo.save_article(session, data)
    assert article is not None
    article.is_processed = True
    article.category = "AI Agent"
    article.final_score = 70
    article.summary = title
    from app.processing.pipeline import annotate_free_offer

    annotate_free_offer(article, config=get_config())
    session.commit()
    return article.id


def test_annotation_and_query_round_trip(session):
    seed_free(session, "opencode is free with DeepSeek V3 this week", "https://reddit.com/r/a1")
    seed_free(session, "Cursor free Pro week for students", "https://reddit.com/r/a2")
    seed_free(session, "Meta introduces camera-free AI glasses", "https://theverge.com/a3")

    items = repo.free_offers(session, days=7)
    assert {i.free_offer_tool for i in items} == {"opencode", "Cursor"}
    tools = dict(repo.free_offer_tools(session, days=7))
    assert tools.get("opencode") == 1 and tools.get("Cursor") == 1

    only_cursor = repo.free_offers(session, days=7, tool="Cursor")
    assert len(only_cursor) == 1 and only_cursor[0].free_offer_tool == "Cursor"


def test_backfill_marks_previously_stored_news(session):
    from app.processing.pipeline import backfill_free_offers

    seed_free(session, "Unrelated GPU launch story", "https://nvidia.com/x1")
    # a row stored before the vocabulary knew about this tool
    extra = repo.save_article(session, _raw(
        "SiliconFlow 免费送 Qwen3 额度", "https://linux.do/t/x2"))
    session.commit()
    assert extra.is_free_offer is False
    marked = backfill_free_offers(session, config=get_config())
    session.commit()
    assert marked >= 1
    session.refresh(extra)
    assert extra.is_free_offer and extra.free_offer_tool == "SiliconFlow"


def _raw(title: str, url: str) -> dict:
    from app.processing.normalize import build_article

    data = build_article(title=title, url=url, source_name="Linux.do 最新话题",
                         content=title, published_at=datetime.now(timezone.utc).replace(tzinfo=None))
    return data


# --------------------------------------------------------------- rendering
def test_incidental_body_words_do_not_create_offers():
    """Live-run regression: a Reddit hardware thread that happens to say
    "no cost" must not show up under 白嫖."""
    title = "Qwengram-0.8B: I transferred n-gram memory to a 0.8B model"
    body = ("This is free software, released at no cost. "
            "I benchmarked it against Hugging Face models and chatgpt.")
    assert detect(title, body) is None

    title2 = "Show HN: PlaceCall - agentic API to call businesses"
    body2 = ("We compared codex and claude on cost. " + "unrelated discussion of latency and tooling. " * 6
             + "the free tier mention is far away.")
    assert detect(title2, body2) is None, "强信号必须与主体相邻才算免费资讯"


def test_body_only_offer_still_detected_when_signal_is_strong_and_adjacent():
    title = "Announcing our new pricing page"
    body = "As of today, Cursor is free for all students for the rest of the semester."
    offer = detect(title, body)
    assert offer is not None and offer.tool == "Cursor"


def test_models_are_deduplicated():
    offer = detect("opencode is free with DeepSeek V3, DeepSeek V3 again")
    assert offer is not None
    assert len(offer.models) == len(set(offer.models))


def test_expiry_requires_a_real_date_word():
    assert detect("Groq free tier through Portland") is not None  # offer is real
    offer = detect("Groq free tier through Portland")
    assert not (offer.expiry or "").lower().startswith("through portland")
    dated = detect("Groq free tier through October")
    assert dated.expiry and "ctober" in dated.expiry


def test_free_list_rendering_is_chinese_and_grouped(session):
    from app.services.news import get_news_service

    seed_free(session, "opencode adds free DeepSeek V3 access", "https://reddit.com/r/b1")
    news = get_news_service()
    items = news.free_offers(days=7, limit=10)
    text = fmt.free_offer_list(items, config=get_config(), days=7)
    assert "近期免费" in text and "opencode" in text
    assert "deepseek" in text.lower()
    assert "判断依据" in text


def test_empty_free_list_gives_a_useful_hint():
    text = fmt.free_offer_list([], config=get_config(), days=7)
    assert "没有发现" in text and "/free deepseek" in text


# ------------------------------------------------------------------ handler
class FakeChat:
    def __init__(self, cid: int) -> None:
        self.id = cid


class FakeUser:
    id = 7
    full_name = "Tester"


class FakeMessage:
    def __init__(self, chat_id: int = 111111111, text: str | None = None) -> None:
        self.chat = FakeChat(chat_id)
        self.from_user = FakeUser()
        self.text = text
        self.sent: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs) -> "FakeMessage":
        self.sent.append((text, kwargs))
        return FakeMessage(self.chat.id)

    @property
    def last(self) -> str:
        return self.sent[-1][0] if self.sent else ""


class Cmd:
    def __init__(self, args: str | None = None) -> None:
        self.args = args


@pytest.mark.asyncio
async def test_free_command_lists_offers(session):
    from app.bot.handlers.free import cmd_free
    from app.services.news import get_news_service

    seed_free(session, "opencode is free with DeepSeek V3", "https://reddit.com/r/c1")
    message = FakeMessage(text="/free")
    await cmd_free(message, Cmd(""), get_news_service(), get_config())
    assert "opencode" in message.last
    keyboard = message.sent[-1][1]["reply_markup"]
    data = [b.callback_data for row in keyboard.inline_keyboard for b in row if b.callback_data]
    assert any(d.startswith("f:d:7") for d in data)
    assert any(d.startswith("f:t:") for d in data)


@pytest.mark.asyncio
async def test_free_command_filters_by_tool_argument(session):
    from app.bot.handlers.free import cmd_free
    from app.services.news import get_news_service

    seed_free(session, "opencode is free with DeepSeek V3", "https://reddit.com/r/d1")
    seed_free(session, "Cursor free Pro week", "https://reddit.com/r/d2", source="Linux.do 最新话题")
    message = FakeMessage(text="/free cursor")
    await cmd_free(message, Cmd("cursor"), get_news_service(), get_config())
    assert "Cursor" in message.last


def test_arg_parsing_covers_the_documented_shapes():
    from app.bot.handlers.free import _parse_args

    config = get_config()
    assert _parse_args("", config)[0] == int(config.get("free.days_default", 30))
    days, tool, kw = _parse_args("opencode", config)
    assert tool == "opencode" and kw is None
    days, tool, kw = _parse_args("7", config)
    assert days == 7 and tool is None
    days, tool, kw = _parse_args("deepseek 90", config)
    assert days == 90 and tool == "DeepSeek" and kw is None
    days, tool, kw = _parse_args("qoder 7天", config)
    assert days == 7 and tool == "Qoder"


def test_natural_language_entry_point():
    from app.bot.handlers.free import looks_like_free_query

    config = get_config()
    assert looks_like_free_query("最近有什么可以白嫖的模型？", config)
    assert looks_like_free_query("opencode 免费吗", config)
    assert looks_like_free_query("/news", config) is False
    assert looks_like_free_query("OpenAI released a new model", config) is False


def _registered_commands(router) -> list[str]:
    """Command names from the filters aiogram stored on each handler object."""
    from aiogram.filters import Command

    names: list[str] = []
    for handler in router.message.handlers:
        for wrapper in getattr(handler, "filters", []):
            test = getattr(wrapper, "callback", wrapper)
            if isinstance(test, Command):
                names.extend(test.commands)
    return names


def test_free_handlers_are_registered_on_the_router():
    from app.bot.bot import COMMANDS
    from app.bot.handlers import free as free_module

    callbacks = {h.callback.__name__ for h in free_module.router.message.handlers}
    callback_ars = {h.callback.__name__ for h in free_module.router.callback_query.handlers}
    assert "cmd_free" in callbacks and "cb_free" in callback_ars
    assert any(c.command == "free" for c in COMMANDS)


def test_unicode_alias_is_registered_on_the_handler():
    """Telegram's command menu only accepts a-z0-9_, so /免费 cannot be listed
    there - but the handler must still match it. Assert that from the router."""
    from app.bot.handlers import free as free_module

    registered = _registered_commands(free_module.router)
    assert "free" in registered
    assert "免费" in registered, "用户直接打 /免费 也要能命中"
    assert "白嫖" in registered


# ---------------------------------------------------------------------------
# Regressions from the first real 福利-category corpus (linux.do).
# These titles are what actually arrives from a Chinese community feed: no
# spaces, product names glued to version numbers, two-character vendor names.
# ---------------------------------------------------------------------------
def test_two_character_chinese_vendor_names_are_subjects():
    """"智谱" is a complete company name; the >=3 length floor is ASCII-only."""
    from app.processing.free_offers import detect

    offer = detect("智谱登录送积分x10,使用积分100%返还", "", config=get_config())
    assert offer is not None and offer.tool == "Zhipu"
    assert "送积分" in offer.signals


def test_version_suffix_does_not_break_the_word_boundary():
    """"限免100刀DeepSeekv4.1Flash" has no space between brand and version."""
    from app.processing.free_offers import detect

    offer = detect("【RelayFor】限免100刀DeepSeekv4.1Flash", "", config=get_config())
    assert offer is not None and offer.tool == "DeepSeek"
    assert "deepseek" in offer.models
    assert offer.kind == "模型"
    # ...but a genuinely different token must not match
    assert detect("deepseekai 免费了", "", config=get_config()) is None


def test_welfare_titles_from_the_real_feed_are_detected():
    """Sampled verbatim from the first 24 rows the 福利 feed delivered."""
    from app.processing.free_offers import detect

    hits = [
        "交✌️免费Qoder CN一年",
        "Kiro白嫖首月额度的银行卡问题",
    ]
    misses = [
        # 抽兑换码/充赠 is a promo but says nothing free - the offer only shows
        # up once the body text is scanned, so title-only must stay None.
        "嘀嘀嘀 AI - 中秋国庆福利:连续 7 天抽兑换码,充 100 加赠 10%",
        "中国移动2元话费 最近1周每天早上10:00抢(本周限一次)",   # not AI
        "这个月有人薅kiro pro max羊毛了吗?",                    # a question, not an offer
        "中秋快乐,送上5000个http节点,可用于代理池/注册机(境外访问)",
    ]
    for title in hits:
        assert detect(title, "", config=get_config()) is not None, title
    for title in misses:
        assert detect(title, "", config=get_config()) is None, title


def test_headline_subject_beats_a_brand_mentioned_in_the_body():
    """"免费Qoder CN一年" plus a body that name-drops gemini/grok is a Qoder story."""
    from app.processing.free_offers import detect

    offer = detect("交✌️免费Qoder CN一年",
                   "讨论里顺便提到 gemini 和 grok 与 claude 的免费额度",
                   config=get_config())
    assert offer is not None
    assert offer.tool == "Qoder"
    assert set(offer.models) >= {"gemini", "grok"}


def test_discourse_feed_boilerplate_is_not_content():
    """A Discourse topic description is "20 posts - 17 participants / Read full topic"."""
    from app.processing.normalize import strip_html

    text = strip_html("<p>20 posts - 17 participants</p><p>Read full topic</p>"
                      "<p>Qoder 向上海交通大学全校师生开放免费额度</p>")
    assert "posts" not in text and "Read full topic" not in text
    assert text.startswith("Qoder")


def test_legacy_rows_render_without_feed_boilerplate():
    """Rows stored before the collector fix must still display cleanly."""
    from datetime import datetime, timezone

    from app.services.news import ArticleView

    def view(**kw):
        base = dict(id=1, title="Qoder 校园福利", url="https://linux.do/t/1",
                    source_name="Linux.do 福利分类", source_type="rss", category="tools",
                    subcategory=None, summary=None, published_at=datetime.now(timezone.utc))
        base.update(kw)
        return ArticleView(**base)

    legacy = view(summary="20 posts - 17 participants Read full topic Qoder 向全校师生开放")
    assert "posts" not in legacy.display_summary
    assert legacy.display_summary.startswith("Qoder")
    zh = view(summary_zh="20 posts - 17 participants 阅读全文 Qoder 向全校师生开放")
    assert "posts" not in zh.display_summary


def test_boilerplate_stripping_keeps_chinese_punctuation():
    """NFKC in here would turn "，" into "," across every Chinese summary."""
    from app.processing.normalize import strip_feed_boilerplate

    text = strip_feed_boilerplate("20 posts - 17 participants 免费开放，仅限今天，Read full topic")
    assert text == "免费开放，仅限今天，"


def test_body_only_subject_is_flagged_as_inferred():
    from app.processing.free_offers import detect

    cfg = get_config()
    direct = detect("Qoder 限时免费一周", "里面还提到了 gemini 和 claude", config=cfg)
    assert direct.subject_in_title is True

    inferred = detect("社区福利周报第 38 期",
                      "本期亮点：Kiro 现在免费开放，注册即可用，另有多家模型限免。",
                      config=cfg)
    assert inferred is not None and inferred.subject_in_title is False
    assert inferred.tool == "Kiro"


def test_inferred_offers_are_labelled_in_the_list(config):
    from datetime import datetime
    from types import SimpleNamespace

    article = SimpleNamespace(
        free_offer={"tool": "Kiro", "kind": "编程 Agent/IDE", "models": [],
                    "signals": ["免费"], "subject_in_title": False},
        title="社区福利周报", display_title="社区福利周报",
        display_summary="本期亮点：Kiro 现在免费开放", url="https://example.com/1",
        source_name="Linux.do 福利分类", published_at=datetime.utcnow(),
    )
    text = fmt.free_offer_list([article], config=config, days=7)
    assert "推断自正文" in text
    explicit = SimpleNamespace(**{**article.__dict__,
                                  "free_offer": {**article.free_offer,
                                                 "subject_in_title": True}})
    assert "推断自正文" not in fmt.free_offer_list([explicit], config=config, days=7)


def test_a_free_model_from_the_gateway_can_be_the_subject():
    """No known agent, but the headline names a model that is free right now."""
    from app.config import get_config
    from app.processing.free_offers import detect

    config = get_config()
    config.register_model_names(["space-bunny-alpha"])
    offer = detect("hermes 官方提供了免费的 stealth/space-bunny-alpha", "", config=config)
    assert offer is not None
    assert offer.tool == "space-bunny-alpha"
    assert offer.kind == "模型"
    assert offer.subject_in_title is True


def test_a_site_name_next_to_a_giveaway_signal_is_a_subject():
    from app.processing.free_offers import detect

    config = get_config()
    offer = detect("咕咕嘎嘎站发放300个50刀兑换码", "", config=config)
    assert offer is not None and offer.tool == "咕咕嘎嘎站"
    # the same site word without a free signal must stay quiet
    assert detect("站内合适的公益站推荐", "", config=config) is None


def test_register_model_names_is_bounded_and_idempotent():
    from app.config import get_config

    config = get_config()
    before = len(config.free["models"])
    assert config.register_model_names(["aaa", "ab", "newmodel-x"]) == 1
    assert config.register_model_names(["newmodel-x"]) == 0
    assert len(config.free["models"]) == before + 1


def test_reddit_feed_chrome_never_becomes_a_headline():
    """Measured in production: /新闻 item #4 was the chrome, three times over.

    A link post's whole RSS body is "submitted by /u/x [link] [comments]", which
    got summarised, translated (spending free MT quota) and shown as the title.
    """
    from app.processing import summarizer
    from app.processing.normalize import strip_feed_boilerplate

    chrome = "submitted by /u/pmv143 [link] [comments] " * 3
    assert strip_feed_boilerplate(chrome) == ""
    assert strip_feed_boilerplate("由 /u/pmv143 提交 [链接] [评论] 由 /u/pmv143 提交 [链接] [评论]") == ""
    assert strip_feed_boilerplate("crossposted from /r/singularity - real headline text"
                                 ) == "real headline text"
    # the comma the chrome left behind goes too
    assert strip_feed_boilerplate("正文一句话。, submitted by /u/x [link]") == "正文一句话。"
    assert strip_feed_boilerplate("实盘内容重复。 实盘内容重复。 实盘内容重复。") == "实盘内容重复。"

    result = summarizer.fallback_summary(
        {"title": "At this point, OpenAI should be nationalized.", "content": chrome})
    assert result["summary"] == "At this point, OpenAI should be nationalized."


def test_chrome_headline_falls_back_to_the_title_in_views():
    """Legacy rows keep their translated chrome in the DB; the view must not show it."""
    from app.services.news import ArticleView

    row = ArticleView(id=1, title="At this point, OpenAI should be nationalized.",
                      url="https://reddit.com/r/LocalLLaMA/comments/1", source_name="Reddit",
                      source_type="rss", category="Other", subcategory=None,
                      summary="submitted by /u/pmv143 [link] [comments]",
                      summary_zh="由 /u/pmv143 提交 [链接] [评论]",
                      published_at=None, final_score=51)
    assert row.display_summary in (None, "")
    assert row.display_line == row.title
    assert row.display_title
