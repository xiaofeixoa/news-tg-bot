"""中文输出：翻译服务、配额、入库与显示层优先级。"""

from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import get_config
from app.database import repository as repo
from app.database.database import session_scope
from app.database.models import Article
from app.services import format as fmt
from app.services.news import ArticleView, get_news_service
from app.services.translate import (
    Budget,
    Translator,
    needs_translation,
    protect_terms,
    restore_terms,
    wants_chinese,
)

# The engine-facing name list lives in settings.yaml; the stub has to guard the
# same way the service does to stay a believable stand-in. Read it off a real
# Translator so the two can never drift apart (units joined the list in v1.25).
KEEP_TERMS = Translator(get_config()).keep_terms


@pytest.fixture(autouse=True, scope="module")
def _enable_translation():
    """This module tests the translator itself; HTTP stays stubbed per test."""
    config = get_config()
    config.raw.setdefault("translate", {})["enabled"] = True
    yield
    config.raw["translate"]["enabled"] = False


def make_view(**kwargs) -> ArticleView:
    base = dict(
        id=1, title="OpenAI releases GPT-5 with cheaper reasoning",
        url="https://openai.com/gpt-5", source_name="OpenAI", source_type="rss",
        category="AI Models", subcategory="GPT", summary=None,
        published_at=datetime.now(timezone.utc).replace(tzinfo=None), final_score=80,
    )
    base.update(kwargs)
    return ArticleView(**base)


# ---------------------------------------------------------------- detection
def test_needs_translation_only_for_latin_text():
    assert needs_translation("Anthropic ships Claude Opus 4.5 for enterprises")
    assert not needs_translation("Anthropic 发布 Claude Opus 4.5 企业版")
    assert not needs_translation("")
    assert not needs_translation(None)
    assert not needs_translation("AI")          # too short to bother translating


def test_repo_style_titles_are_not_wasted_on_translation():
    """'owner/name (0 stars)' is a repository, not a sentence."""
    assert needs_translation("waytoagi-team/gallery (0 stars)") is False
    assert needs_translation("Englishtartar2028/DeepSeek-v4.1-Flash (12 stars)") is False
    assert needs_translation("DeepSeek releases a new open weights reasoning model") is True


async def test_brand_names_that_have_no_chinese_form_survive_the_engine():
    """⑴⑵ placeholders keep "Gemini"/"Hugging Face" from becoming 双子座/拥抱人脸.

    The four headings are the measured live damage, not invented examples.
    """
    cases = {
        "Introducing Gemini Omni": "介绍 Gemini Omni",
        "How Hugging Face Inference Endpoints work": "Hugging Face 推理端点如何工作",
        "Claude's Load-Bearing Seams": "Claude 的承重接缝",
        "Anthropic says its biology lab found something": "Anthropic 表示其生物实验室有发现",
    }
    client = StubClient(cases)
    service = translator_with(client)
    service.provider = "mymemory"
    got = await service.translate_many(list(cases), hint="title")
    for source, expected in cases.items():
        assert got.get(source) == expected, source
    for mangled in ("双子座", "拥抱人脸", "克劳德", "人类技术"):
        assert mangled not in "".join(got.values()), mangled


async def test_a_route_that_eats_the_brand_is_rejected_and_the_next_one_answers():
    """MyMemory returns junk or drops the name; that must not be stored as Chinese."""
    client = ChainClient(mymemory_body={"Introducing Gemini Omni": "隆重推出双子座全知"},
                         google_body={"Introducing Gemini Omni": "介绍 Gemini Omni"})
    service = translator_with(client)
    service.provider = "auto"
    got = await service.translate_many(["Introducing Gemini Omni"], hint="title")
    assert got == {"Introducing Gemini Omni": "介绍 Gemini Omni"}, got
    assert any("mymemory" in url for url in client.calls)
    assert any("translate.google.com" in url for url in client.calls)


def test_names_with_an_accepted_chinese_form_are_left_for_the_engine():
    """微软/亚马逊 are correct translations; guarding them would reject good output."""
    for plain in ("Microsoft", "Amazon", "NVIDIA", "Google"):
        assert plain not in KEEP_TERMS, plain
    guarded, mapping = protect_terms("Microsoft unveils a Copilot super app at Amazon", KEEP_TERMS)
    assert "Microsoft" in guarded and "Amazon" in guarded
    assert list(mapping.values()) == ["Copilot"], mapping


def test_adjacent_and_multiword_names_travel_as_one_placeholder():
    """Splitting "Amazon SageMaker" is what reorders a headline into nonsense."""
    cases = {
        "Deploying real-time speech with Qwen3-TTS on Amazon SageMaker AI": ["Amazon SageMaker"],
        "NarrateAI: production-ready LLM QA on Amazon Bedrock": ["Amazon Bedrock"],
        "Rendering huge pull requests in the GitHub Copilot app": ["GitHub Copilot"],
        "Microsoft and OpenAI announce Azure pricing": ["OpenAI", "Azure"],
    }
    for text, expected in cases.items():
        guarded, mapping = protect_terms(text, KEEP_TERMS)
        assert list(mapping.values()) == expected, text
        assert restore_terms(guarded, mapping) == text, "the round trip is lossless"


def test_longer_names_win_over_their_prefixes():
    """"Google DeepMind" is one name: the shorter entry must not split it."""
    guarded, mapping = protect_terms("Google DeepMind opens DeepSeek weights", KEEP_TERMS)
    assert "Google" not in guarded and "DeepMind" not in guarded and "DeepSeek" not in guarded
    assert sorted(mapping.values()) == ["DeepSeek", "Google DeepMind"], mapping


def test_card_only_shows_chinese_bullets():
    """Rule-mode bullets are English body sentences; 439 of 439 rows had none in Chinese.

    Rendering them put a raw English block under 核心内容 in every card, including
    the 29 delivered rows measured on the live box.
    """
    config = get_config()
    english = make_view(key_points=["The model beats GPT-5 on coding benchmarks.", "已有中文的一条"])
    assert english.display_key_points == ["已有中文的一条"]
    card = fmt.article_card(english, config=config)
    assert "The model beats" not in card

    stored = make_view(key_points=["English one."],
                       meta={"key_points_zh": ["第一条中文要点。", "第二条中文要点。"]})
    assert stored.display_key_points == ["第一条中文要点。", "第二条中文要点。"]
    rendered = fmt.article_card(stored, config=config)
    assert "核心内容" in rendered and "第一条中文要点" in rendered
    assert "English one" not in rendered


async def test_ensure_chinese_translates_bullets_after_the_headline(session):
    """Bullets are asked for, but only with whatever quota the headline left."""
    from app.services.translate import reset_translator
    import app.services.translate as tr

    ids = seed(session, ("OpenAI releases GPT-6 with a bigger context window",
                         "https://openai.com/gpt6"))
    row = session.get(Article, ids[0])
    row.key_points = ["GPT-6 scores higher on coding benchmarks.", "Pricing drops by a third."]
    session.commit()

    title = "OpenAI releases GPT-6 with a bigger context window"
    client = StubClient({
        title: "OpenAI 发布 GPT-6，上下文更长",
        f"{title} summary sentence in english": "上下文更长了。",
        "GPT-6 scores higher on coding benchmarks.": "GPT-6 在编程基准上得分更高。",
        "Pricing drops by a third.": "价格下降三分之一。",
    })
    translator = Translator(get_config())
    translator.provider = "mymemory"
    translator._http = lambda: _coro(client)
    reset_translator()
    tr._translator = translator
    try:
        items = await get_news_service().ensure_chinese([get_news_service().by_id(ids[0])])
    finally:
        tr._translator = None

    assert items[0].display_key_points == ["GPT-6 在编程基准上得分更高。", "价格下降三分之一。"]
    assert items[0].meta["key_points_zh"] == ["GPT-6 在编程基准上得分更高。", "价格下降三分之一。"]
    # `ensure_chinese` writes through its own session_scope; this test's session
    # still holds the pre-translation copy in its identity map.
    session.expire_all()
    assert session.get(Article, ids[0]).meta["key_points_zh"], "paid once, stored for good"


def test_output_language_switch_is_respected():
    config = get_config()
    assert wants_chinese(config) is True
    config.raw["app"]["language"] = "en"
    try:
        assert wants_chinese(config) is False
        assert Translator(config).enabled is False
    finally:
        config.raw["app"]["language"] = "zh"
    config.raw["app"]["language"] = "en"
    try:
        assert wants_chinese(config) is False
        assert Translator(config).enabled is False
    finally:
        config.raw["app"]["language"] = "zh"


# ------------------------------------------------------------------- budget
def test_budget_blocks_after_the_run_and_daily_allowances():
    budget = Budget(per_run=2, per_day=3)
    assert budget.available()
    budget.spend(2)
    assert not budget.available(), "per-run cap bites first"

    budget.used_run = 0
    budget.spend(2)                      # used_day = 4 > per_day
    assert not budget.available(), "per-day cap must bite"

    budget.used_run = 0
    budget.day_of = "1970-01-01"         # a new UTC day resets the daily counter
    assert budget.available()


# ---------------------------------------------------------------- providers
class StubResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code      # httpx always carries these two
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload


class StubClient:
    """Stands in for a free translation engine.

    Mappings are authored in plain English, but requests now arrive with brand
    names replaced by ⑴⑵ placeholders (`protect_terms`), so the stub guards each
    authored key the same way before matching and answers with the placeholders
    still in the Chinese - which is what a real engine returns and what
    `restore_terms` has to undo. `mangle=False` emulates MyMemory, which swallows
    the placeholder instead of passing it through.
    """

    def __init__(self, mapping, *, fail_with=None, mangle: bool = True):
        self.mapping = mapping
        self.calls: list[str] = []
        self.fail_with = fail_with
        self.mangle = mangle

    async def get(self, url, params=None, **kwargs):
        text = (params or {}).get("q", "")
        self.calls.append(text)
        if self.fail_with is not None:
            raise self.fail_with
        answer = text
        for key, value in self.mapping.items():
            guarded, mapping = protect_terms(key, KEEP_TERMS)
            if guarded == text or key == text:
                # mangle=True: the name comes back as a placeholder (Google does
                # this). mangle=False: the engine translated the name away and the
                # placeholder is simply missing from the answer (MyMemory does).
                answer = _reguard(value, mapping) if self.mangle else value
                break
        return StubResponse({"responseData": {"translatedText": answer},
                             "responseStatus": 200})

    def quota_exceeded(self, url, params=None, **kwargs):  # helper for tests
        return StubResponse({"responseData": {"translatedText": "MYMEMORY WARNING: quota"},
                             "responseStatus": 429}, status_code=429)


def _reguard(answer: str, mapping: dict[str, str]) -> str:
    """Put the placeholders into the canned Chinese, as a pass-through engine does."""
    out = answer
    for token, term in mapping.items():
        out = re.sub(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(term), token, out, flags=re.I)
    return out


def translator_with(client: StubClient) -> Translator:
    service = Translator(get_config())

    async def _http():
        return client
    service._http = _http
    # `Translator.__init__` now reads the persisted daily counter, and every test in
    # this module shares one DATA_DIR - so without isolating it, whichever test spent
    # two requests first decides whether the next one is "out of budget". That is
    # exactly how this suite broke when the counter became persistent.
    service.budget.path = Path(tempfile.mkdtemp(prefix="anr-budget-")) / "translate_budget.json"
    service.budget.used_run = service.budget.used_day = 0
    service.budget.day_of = ""
    return service


@pytest.mark.asyncio
async def test_mymemory_route_translates_and_skips_chinese():
    client = StubClient({
        "OpenAI releases GPT-5 for everyone": "OpenAI 面向所有人发布 GPT-5",
        "NVIDIA ships B200 GPU": "NVIDIA 出货 B200 GPU",
    })
    service = translator_with(client)
    service.provider = "mymemory"
    got = await service.translate_many(
        ["OpenAI releases GPT-5 for everyone", "NVIDIA ships B200 GPU", "英伟达已发布新加速卡"],
        hint="title")
    assert got["OpenAI releases GPT-5 for everyone"] == "OpenAI 面向所有人发布 GPT-5"
    assert got["NVIDIA ships B200 GPU"] == "NVIDIA 出货 B200 GPU"
    assert "英伟达已发布新加速卡" not in client.calls, "already-Chinese text must not be sent"


@pytest.mark.asyncio
async def test_translation_failure_keeps_the_english_and_the_news():
    client = StubClient({}, fail_with=RuntimeError("upstream down"))
    service = translator_with(client)
    service.provider = "mymemory"
    got = await service.translate_many(["Anthropic launches a new agent SDK"], hint="title")
    assert got == {}                     # nothing invented, no exception raised
    assert service.budget.used_run >= 1  # the attempt was still accounted for


@pytest.mark.asyncio
async def test_llm_route_is_preferred_when_a_model_is_configured():
    class LLM:
        enabled = True
        def __init__(self): self.calls = 0

        async def ask_json(self, prompt, *, tier="light"):
            self.calls += 1
            return {"items": [{"src": "OpenAI ships GPT-5", "zh": "OpenAI 发布 GPT-5"}]}

    llm = LLM()
    service = Translator(get_config(), llm=llm)
    service.provider = "auto"
    got = await service.translate_many(["OpenAI ships GPT-5"], hint="title")
    assert got == {"OpenAI ships GPT-5": "OpenAI 发布 GPT-5"}
    assert service.mode() == "llm" and llm.calls == 1


@pytest.mark.asyncio
async def test_llm_route_falls_back_to_free_provider(monkeypatch):
    class Broken:
        enabled = True

        async def ask_json(self, prompt, *, tier="light"):
            raise RuntimeError("429 from provider")

    service = Translator(get_config(), llm=Broken())
    service.provider = "auto"
    client = StubClient({"OpenAI ships GPT-5": "OpenAI 发布 GPT-5"})
    service._http = lambda: _coro(client)
    got = await service.translate_many(["OpenAI ships GPT-5"], hint="title")
    assert got["OpenAI ships GPT-5"] == "OpenAI 发布 GPT-5"


async def _coro(value):
    return value


# ------------------------------------------------------- pipeline + storage
def seed(session, *pairs) -> list[Article]:
    from app.processing.normalize import build_article
    ids = []
    for title, url in pairs:
        data = build_article(title=title, url=url, source_name="OpenAI",
                             content=f"{title}. Details about the AI model release and its benchmarks.",
                             published_at=datetime.now(timezone.utc).replace(tzinfo=None))
        article = repo.save_article(session, data)
        if article:
            article.is_processed = True
            article.category = "AI Models"
            article.final_score = 80
            article.summary = title + " summary sentence in english"
            ids.append(article.id)
    session.commit()
    return ids


@pytest.mark.asyncio
async def test_translate_pending_fills_chinese_columns_and_drains_queue(session):
    from app.services.translate import get_translator, reset_translator

    seed(session, ("OpenAI releases GPT-5 for everyone", "https://openai.com/gpt5"),
         ("Anthropic ships Claude Opus 4.5", "https://anthropic.com/opus"))
    client = StubClient({
        "OpenAI releases GPT-5 for everyone": "OpenAI 面向所有人发布 GPT-5",
        "Anthropic ships Claude Opus 4.5": "Anthropic 发布 Claude Opus 4.5",
        "OpenAI releases GPT-5 for everyone summary sentence in english": "OpenAI 发布 GPT-5： everyone 的摘要句",
    })
    reset_translator()
    translator = Translator(get_config())
    translator.provider = "mymemory"
    translator._http = lambda: _coro(client)

    from app.processing.pipeline import translate_pending

    done = await translate_pending(session, config=get_config(), translator=translator)
    session.commit()
    assert done == 2
    rows = session.query(Article).order_by(Article.id).all()
    assert {r.title_zh for r in rows} == {"OpenAI 面向所有人发布 GPT-5", "Anthropic 发布 Claude Opus 4.5"}
    assert all(r.translated_by for r in rows)

    # queue drains: nothing left to do on the next round
    assert await translate_pending(session, config=get_config(), translator=translator) == 0
    reset_translator()


@pytest.mark.asyncio
async def test_the_background_round_fills_key_points_with_the_leftover_budget(session):
    """settings.yaml 一直写着"剩余额度轮到要点"，但后台轮从没真做过这段。"""
    from app.processing.pipeline import translate_pending
    from app.services.translate import reset_translator

    ids = seed(session, ("Meta releases an open model", "https://meta.com/llama"))
    row = session.get(Article, ids[0])
    row.title_zh = "Meta 发布开放模型"          # 标题摘要都齐了，只剩要点
    row.summary_zh = "开放权重，可商用。"
    row.key_points = ["The release includes a 400B variant.", "Pricing drops by a third."]
    session.commit()

    client = StubClient({
        "The release includes a 400B variant.": "这次发布包含一个 400B 版本。",
        "Pricing drops by a third.": "价格下降三分之一。",
    })
    translator = Translator(get_config())
    translator.provider = "mymemory"
    translator._http = lambda: _coro(client)
    reset_translator()
    try:
        done = await translate_pending(session, config=get_config(), translator=translator)
        session.commit()
    finally:
        reset_translator()
    assert done == 1, "标题摘要已在库里，不该再问；只有要点可翻"
    session.expire_all()
    view = get_news_service().by_id(ids[0])
    assert view.meta["key_points_zh"] == ["这次发布包含一个 400B 版本。", "价格下降三分之一。"]
    assert view.key_points_in_english is False
    assert view.display_key_points == ["这次发布包含一个 400B 版本。", "价格下降三分之一。"]


@pytest.mark.asyncio
async def test_one_round_moves_headline_and_bullets_without_double_counting(session):
    """一行同时被标题段和要点段改到，返回的"处理了几篇"只能算一次。"""
    from app.processing.pipeline import translate_pending
    from app.services.translate import reset_translator

    ids = seed(session, ("Mistral ships an edge model", "https://mistral.ai/edge"))
    row = session.get(Article, ids[0])
    row.key_points = ["Runs on a phone.", "Price per million tokens falls."]
    session.commit()

    client = StubClient({
        "Mistral ships an edge model": "Mistral 发布端侧模型",
        "Mistral ships an edge model summary sentence in english": "端侧也能跑。",
        "Runs on a phone.": "可以在手机上运行。",
        "Price per million tokens falls.": "每百万 token 价格下降。",
    })
    translator = Translator(get_config())
    translator.provider = "mymemory"
    translator._http = lambda: _coro(client)
    reset_translator()
    try:
        done = await translate_pending(session, config=get_config(), translator=translator)
        session.commit()
    finally:
        reset_translator()
    assert done == 1
    session.expire_all()
    view = get_news_service().by_id(ids[0])
    assert view.title_zh == "Mistral 发布端侧模型"
    assert view.meta["key_points_zh"] == ["可以在手机上运行。", "每百万 token 价格下降。"]


@pytest.mark.asyncio
async def test_a_point_the_provider_cannot_translate_is_asked_twice_then_dropped(session):
    """问不出结果的句子不能每轮都把整条预算吃掉——正文提取用的是同一个办法。"""
    from app.processing.pipeline import translate_pending
    from app.services.translate import reset_translator

    ids = seed(session, ("Anthropic publishes a long contract", "https://anthropic.com/contract"))
    row = session.get(Article, ids[0])
    row.title_zh = "Anthropic 发布长合同"
    row.summary_zh = "条款很长。"
    row.key_points = ["Untranslatable gibberish zzzq."]
    session.commit()

    client = StubClient({})           # 什么都翻不出来
    translator = Translator(get_config())
    translator.provider = "mymemory"
    translator._http = lambda: _coro(client)
    reset_translator()
    try:
        await translate_pending(session, config=get_config(), translator=translator)
        session.commit()
        session.expire_all()
        first = session.get(Article, ids[0]).meta.get("key_points_tried")
        await translate_pending(session, config=get_config(), translator=translator)
        session.commit()
        session.expire_all()
        second = session.get(Article, ids[0]).meta.get("key_points_tried")
        asked = len(client.calls)
        await translate_pending(session, config=get_config(), translator=translator)
        session.commit()
        session.expire_all()
    finally:
        reset_translator()
    assert (first, second) == (1, 2)
    assert len(client.calls) == asked, "两次问不到就不该再问"


def test_card_labels_the_english_bullets_instead_of_dropping_the_block():
    """藏起来看起来像卡片坏了；他说过额度用尽就直接显示英文。"""
    config = get_config()
    view = make_view(key_points=["The model beats GPT-5 on coding benchmarks."])
    assert view.key_points_in_english is True
    card = fmt.article_card(view, config=config)
    assert "核心内容（以下为原文" in card
    assert "The model beats GPT-5 on coding benchmarks." in card

    translated = make_view(key_points=["English one."],
                           meta={"key_points_zh": ["中文要点。"]})
    assert translated.key_points_in_english is False
    assert "核心内容（以下为原文" not in fmt.article_card(translated, config=config)


@pytest.mark.asyncio
async def test_ensure_chinese_translates_only_what_is_about_to_be_shown(session):
    ids = seed(session, ("Microsoft unveils a Copilot super app", "https://theverge.com/copilot"))
    client = StubClient({"Microsoft unveils a Copilot super app": "微软推出 Copilot 超级应用"})
    news = get_news_service()
    items = [news.by_id(ids[0])]
    translator = Translator(get_config())
    translator.provider = "mymemory"
    translator._http = lambda: _coro(client)
    import app.services.translate as tr

    tr._translator = translator
    try:
        out = await news.ensure_chinese(items)
    finally:
        tr._translator = None
    assert out[0].display_title == "微软推出 Copilot 超级应用"
    assert session.query(Article).get(ids[0]).title_zh == "微软推出 Copilot 超级应用"


@pytest.mark.asyncio
async def test_a_row_that_dropped_its_unit_is_requeued(session):
    """库里已经存下的错译没法靠占位符救回来，但"英文有、中文没有"这个不变量能挑出来。"""
    from app.processing.pipeline import repair_lost_units

    ids = seed(session, ("I run Qwen3 27B at 50t/s on an M5 Pro", "https://reddit.com/50ts"))
    row = session.get(Article, ids[0])
    row.title_zh = "我在 M5 Pro 上以 50吨/秒 运行 Qwen3 27B"     # 吨！线上真实输出
    row.summary_zh = "速度不错。"
    row.translated_by = "mymemory"
    session.commit()

    assert repair_lost_units(session) >= 1
    session.commit()
    session.expire_all()
    again = session.get(Article, ids[0])
    assert again.title_zh is None and again.translated_by is None
    assert [a.id for a in repo.untranslated_articles(session, limit=20)] == [again.id], \
        "清空之后要能被后台队列重新捡起来"


def test_a_row_whose_chinese_kept_the_unit_is_left_alone(session):
    from app.processing.pipeline import repair_lost_units

    ids = seed(session, ("Serve the model at 1,200 tokens/s", "https://example.com/tps"))
    row = session.get(Article, ids[0])
    row.title_zh = "以 1,200 tokens/s 提供该模型"
    # `seed` 把摘要写成"标题 + summary sentence in english"，所以摘要里也带着这个单位。
    row.summary_zh = "吞吐以 1,200 tokens/s 计。"
    session.commit()
    assert repair_lost_units(session) == 0
    assert session.get(Article, ids[0]).title_zh == "以 1,200 tokens/s 提供该模型"


@pytest.mark.asyncio
async def test_a_measurement_unit_travels_through_the_placeholder():
    """占位符送过去、原样回来：单位翻错不是"译得软"，是对事实说了假话。"""
    import app.services.translate as tr
    from app.services.translate import reset_translator

    src = "I run the model at 50t/s on a laptop"
    # 引擎看到的是 "50⑴"（只有单位被换成占位符），所以真实回答里数字仍在原位。
    client = StubClient({src: "我在笔记本上以 50⑴ 运行该模型"})
    translator = Translator(get_config())
    translator.provider = "mymemory"
    translator._http = lambda: _coro(client)
    reset_translator()
    tr._translator = translator
    try:
        got = await translator.translate_many([src], hint="title")
    finally:
        tr._translator = None
    assert got == {src: "我在笔记本上以 50t/s 运行该模型"}


def test_the_guard_does_not_fire_inside_other_words():
    from app.services.translate import term_in

    assert term_in("run at 50t/s", "t/s")
    assert term_in("0.9 KB/token", "KB/token")
    assert not term_in("tokenization pipeline", "token"), "名字不能落在更长的词里面"
    assert not term_in("GPTX is not a thing", "GPT")
    # 裸词 token 故意不保护："approval token" 译成"批准令牌"是对的中文
    assert "token" not in [t.lower() for t in KEEP_TERMS]
    assert "tokens" not in [t.lower() for t in KEEP_TERMS]


@pytest.mark.asyncio
async def test_a_row_that_is_already_chinese_costs_nothing(session):
    """库里已有 title_zh/summary_zh 时，列表面一条请求都不该发（要点由卡片面负责）。"""
    ids = seed(session, ("Anthropic publishes a longer context window", "https://anthropic.com/ctx"))
    row = session.get(Article, ids[0])
    row.title_zh = "Anthropic 发布更长的上下文窗口"
    row.summary_zh = "上下文更长了。"
    row.key_points = ["Pricing drops by a third."]
    session.commit()

    import app.services.translate as tr

    client = StubClient({"Pricing drops by a third.": "价格下降三分之一。"})
    translator = Translator(get_config())
    translator.provider = "mymemory"
    translator._http = lambda: _coro(client)
    tr._translator = translator
    news = get_news_service()
    try:
        listed = await news.ensure_chinese([news.by_id(ids[0])], with_points=False)
        assert client.calls == [], f"列表面把已经翻好的标题又问了一遍：{client.calls}"
        await news.ensure_chinese([news.by_id(ids[0])], with_points=True)
    finally:
        tr._translator = None
    assert any("Pricing" in call for call in client.calls), "卡片面仍然要补要点"
    assert listed[0].display_title == "Anthropic 发布更长的上下文窗口"


# ------------------------------------------------------------- rendering
def test_rendering_prefers_chinese_everywhere():
    config = get_config()
    zh = make_view(title_zh="OpenAI 发布 GPT-5", summary_zh="推理更便宜，上下文更大")
    assert zh.display_line == "推理更便宜，上下文更大"
    list_text = fmt.news_list([zh], config=config, title="🤖 最新 AI 新闻")
    assert "推理更便宜" in list_text and "OpenAI releases GPT-5" not in list_text
    assert "AI Models" not in list_text, "每行末尾的分类也是我们自己写的文案"
    assert "模型发布" in list_text

    card = fmt.article_card(zh, config=config)
    assert "OpenAI 发布 GPT-5" in card and "一句话总结" in card

    blocks = fmt.section_blocks([zh], config=config)
    assert any("推理更便宜" in b for b in blocks)


def test_english_only_item_still_renders_without_translation():
    config = get_config()
    item = make_view()
    assert item.display_line == item.title
    assert "OpenAI releases GPT-5" in fmt.news_list([item], config=config, title="news")


def test_templated_feed_titles_are_composed_not_machine_translated():
    from app.services.translate import localize_title, needs_translation

    cases = {
        "v2.1.283 released in anthropics/claude-code": "anthropics/claude-code 发布 v2.1.283",
        "openclaw/openclaw (1,234 stars)": "openclaw/openclaw 收获 1,234 星",
        "thefgxdev/production-ai-checklist (0 stars)": "thefgxdev/production-ai-checklist 收获 0 星",
    }
    for title, expected in cases.items():
        assert localize_title(title) == expected
        assert needs_translation(title) is False      # never spends MT quota
    prose = "Google releases Gemini 2.5 with a much longer context window"
    assert localize_title(prose) is None
    assert needs_translation(prose) is True


def test_translation_queue_uses_the_template_for_releases(session, monkeypatch):
    """A release row must get the composed title, not the English original."""
    import asyncio
    from datetime import datetime, timedelta

    from app.database import repository as repo
    from app.processing.normalize import build_article
    from app.processing.pipeline import translate_pending

    data = build_article(title="v1.52.0 released in block/goose",
                         url="https://github.com/block/goose/releases/tag/v1.52.0",
                         source_name="Coding Agent Releases", source_type="github",
                         content="release notes", quality="A",
                         published_at=datetime.utcnow() - timedelta(hours=1))
    article = repo.save_article(session, data)
    article.is_processed = True
    session.commit()

    calls = []

    class StubTranslator:
        enabled = True
        # 真实的 Translator 带着额度计数器；要点那一段会先看它还剩多少。
        budget = Budget(per_run=99, per_day=99)

        async def translate_many(self, texts, hint=""):
            calls.extend(texts)
            return {}

        def mode(self):
            return "mymemory"

    asyncio.run(translate_pending(session, translator=StubTranslator()))
    session.commit()
    assert article.title_zh == "block/goose 发布 v1.52.0"
    assert article.translated_by == "template"
    assert calls == []          # nothing was sent to the free MT provider


def test_boot_repair_fixes_previously_mangled_titles(session):
    from datetime import datetime, timedelta

    from app.database import repository as repo
    from app.processing.normalize import build_article
    from app.processing.pipeline import repair_template_titles

    data = build_article(title="v2.10.0 released in google/adk-python",
                         url="https://github.com/google/adk-python/releases/tag/v2.10.0",
                         source_name="GitHub Releases", source_type="github",
                         content="notes", quality="A",
                         published_at=datetime.utcnow() - timedelta(days=1))
    article = repo.save_article(session, data)
    article.title_zh = "google/adk-python中发布的v2.10.0"   # what MT used to produce
    article.translated_by = "mymemory"
    session.commit()

    assert repair_template_titles(session) == 1
    session.commit()
    assert article.title_zh == "google/adk-python 发布 v2.10.0"
    assert article.translated_by == "template"
    assert repair_template_titles(session) == 0            # idempotent


class ChainClient:
    """One client that answers MyMemory and Google differently."""

    def __init__(self, *, mymemory_body=None, mymemory_status=200, google_body=None,
                 google_error=None, google_parts=None):
        self.calls: list[str] = []
        self.mymemory_body = mymemory_body
        self.mymemory_status = mymemory_status
        self.google_body = google_body
        self.google_error = google_error
        self.google_parts = google_parts

    async def get(self, url, params=None, **kwargs):
        self.calls.append(url)
        if "mymemory" in url:
            if self.mymemory_status == 429:
                return StubResponse({"responseData": {"translatedText": ""},
                                     "responseStatus": 429}, status_code=429)
            text = (params or {}).get("q", "")
            body = self.mymemory_body or {}
            return StubResponse({"responseData": {"translatedText": self._answer(body, text)},
                                 "responseStatus": 200})
        if self.google_error is not None:
            raise self.google_error
        text = (params or {}).get("q", "")
        # Google gets the batch newline-joined, so each line is matched separately.
        answered = [self._answer(self.google_body or {}, line) for line in text.split("\n")]
        if self.google_parts is not None:
            answered = self.google_parts
        # One segment per answer, each ending in a newline: that is the shape the
        # endpoint actually returns for a newline-joined batch, and flattening the
        # answers without it collapsed every batch into a single line.
        lines = [[body + "\n", None] for body in answered if body]
        return StubResponse([lines, None, "en"], status_code=200)

    @staticmethod
    def _answer(mapping, request: str) -> str:
        for key, value in mapping.items():
            guarded, tokens = protect_terms(key, KEEP_TERMS)
            if guarded == request or key == request:
                return _reguard(value, tokens)
        return request


@pytest.mark.asyncio
async def test_auto_falls_through_to_the_second_free_provider():
    client = ChainClient(
        mymemory_body={},                       # nothing comes back
        google_body={"OpenAI ships GPT-6": "OpenAI 发布 GPT-6"},
    )
    service = translator_with(client)
    service.provider = "auto"
    got = await service.translate_many(["OpenAI ships GPT-6"], hint="title")
    assert got == {"OpenAI ships GPT-6": "OpenAI 发布 GPT-6"}
    assert any("mymemory" in u for u in client.calls)
    assert any("translate.google.com/translate_a/t" in u for u in client.calls)


@pytest.mark.asyncio
async def test_provider_quota_failure_is_remembered_not_retried_all_day():
    client = ChainClient(mymemory_status=429,
                         google_body={"Nvidia unveils Rubin GPUs": "英伟达发布 Rubin 显卡"})
    service = translator_with(client)
    service.provider = "auto"
    assert await service.translate_many(["Nvidia unveils Rubin GPUs"], hint="title")
    first_call_count = len(client.calls)
    assert service._route_down("mymemory")

    await service.translate_many(["Nvidia unveils Rubin GPUs"], hint="title")  # cached
    service.cache.clear()
    await service.translate_many(["AMD launches MI400 series"], hint="title")
    # after backoff is recorded, MyMemory is not asked again in this process
    assert len([u for u in client.calls if "mymemory" in u]) == 1
    assert len(client.calls) > first_call_count


@pytest.mark.asyncio
async def test_a_broken_google_route_leaves_the_text_untouched_instead_of_raising():
    client = ChainClient(google_error=RuntimeError("403 from the cloud IP"))
    service = translator_with(client)
    service.provider = "google"
    assert await service.translate_many(["Anthropic raises the limit"], hint="title") == {}


@pytest.mark.asyncio
async def test_explicit_single_provider_does_not_use_the_other():
    client = ChainClient(google_body={"OpenAI ships GPT-6": "OpenAI 发布 GPT-6"})
    service = translator_with(client)
    service.provider = "mymemory"
    await service.translate_many(["OpenAI ships GPT-6"], hint="title")
    assert not any("googleapis" in u for u in client.calls)


def test_google_payload_shapes_both_flatten():
    from app.services.translate import flatten_google

    assert flatten_google(["法院裁定特朗普", None]) == "法院裁定特朗普"
    assert flatten_google([[["法院裁定", "en", None]], None, "en"]) == "法院裁定"
    assert flatten_google([]) == ""
    assert flatten_google(None) == ""


def test_transliterated_brand_names_are_restored():
    """MT writes 克劳德/迪普西克; the reader expects Claude/DeepSeek."""
    from app.services.translate import restore_proper_nouns

    assert restore_proper_nouns("克劳德与迪普西克公布新模型") == "Claude与DeepSeek公布新模型"
    assert restore_proper_nouns("普通中文句子") == "普通中文句子"


@pytest.mark.asyncio
async def test_google_route_refuses_a_misaligned_batch():
    """One merged line for two titles must not be pasted onto the wrong news.

    `google_parts` is given literally because the mapping stub answers one line per
    input line - it could not otherwise produce the short answer the endpoint
    sometimes returns when it merges two headlines into one segment.
    """
    client = ChainClient(google_parts=["两段被合成一段的回答"])
    service = translator_with(client)
    service.provider = "google"
    assert await service.translate_many(["First title that needs work",
                                         "Second title that needs work"], hint="title") == {}


@pytest.mark.asyncio
async def test_translated_by_records_the_route_that_actually_answered():
    """MyMemory was configured, Google did the work; the label must say Google."""
    client = ChainClient(mymemory_status=429,
                         google_body={"Nvidia unveils Rubin GPUs": "英伟达发布 Rubin 显卡"})
    service = translator_with(client)
    service.provider = "auto"
    assert service.mode() == "mymemory"          # configured preference
    await service.translate_many(["Nvidia unveils Rubin GPUs"], hint="title")
    assert service.last_route == "google"        # who really answered


@pytest.mark.asyncio
async def test_a_row_missing_only_its_summary_is_still_queued(session):
    """正文提取 clears summary_zh; a queue keyed on title_zh alone never came back."""
    from app.processing.pipeline import translate_pending
    from app.services.translate import Translator, reset_translator

    row = session.get(Article, seed(session, ("OpenAI ships a smaller model",
                                          "https://openai.com/small"))[0])
    row.title_zh = "OpenAI 发布了一个更小的模型"
    row.translated_by = "mymemory"
    row.summary_zh = None
    session.commit()

    assert repo.untranslated_articles(session, limit=5), "a missing summary must queue the row"

    key = "OpenAI ships a smaller model summary sentence in english"
    # A real engine that keeps the subject: brand names are guarded on the way in
    # and restored on the way out, so the answer is authored with "OpenAI" present.
    client = StubClient({key: "OpenAI 的摘要已经换成新的英文句子。"})
    reset_translator()
    translator = Translator(get_config())
    translator.provider = "mymemory"
    translator._http = lambda: _coro(client)

    assert await translate_pending(session, config=get_config(), translator=translator) == 1
    session.commit()
    assert row.summary_zh == "OpenAI 的摘要已经换成新的英文句子。"
    assert row.title_zh == "OpenAI 发布了一个更小的模型", "the good title must survive"
    assert await translate_pending(session, config=get_config(), translator=translator) == 0
    reset_translator()


@pytest.mark.asyncio
async def test_a_templated_headline_still_gets_a_translated_summary(session):
    """Every GitHub release row used to keep an English summary: the templated
    branch `continue`d before the summary was ever looked at."""
    from app.processing.pipeline import translate_pending
    from app.services.translate import Translator, reset_translator

    row = session.get(Article, seed(session, ("v1.2.0 released in sst/opencode",
                                              "https://github.com/sst/opencode/releases/tag/v1.2.0"))[0])
    session.commit()

    client = StubClient({"v1.2.0 released in sst/opencode summary sentence in english":
                         "该版本新增插件支持。"})
    reset_translator()
    translator = Translator(get_config())
    translator.provider = "mymemory"
    translator._http = lambda: _coro(client)

    await translate_pending(session, config=get_config(), translator=translator)
    session.commit()
    assert row.translated_by == "template" and row.title_zh
    assert row.summary_zh == "该版本新增插件支持。", "the template must not skip the summary"
    reset_translator()


@pytest.mark.asyncio
async def test_a_missing_headline_outranks_a_missing_summary(session):
    """The daily budget is shared; a summary backlog must not starve headlines."""
    aged = seed(session, ("OpenAI beats a fresh headline", "https://openai.com/fresh"))[0]
    backlog = seed(session, ("Anthropic backfills an old summary", "https://anthropic.com/old"))[0]
    a_row, b_row = session.get(Article, aged), session.get(Article, backlog)
    b_row.title_zh, b_row.final_score = "Anthropic 补齐一条旧摘要", 99
    a_row.final_score = 40            # lower score, but its headline is still English
    session.commit()

    queued = repo.untranslated_articles(session, limit=5)
    assert [q.id for q in queued][:2] == [a_row.id, b_row.id], \
        "score order alone would put the summary backfill first"


def test_untranslated_summary_shows_english_instead_of_disappearing():
    """2026-09-26, his call: 免费额度用完了就直接显示英文。"""
    from app.services.format import section_blocks
    from app.services.news import ArticleView

    row = ArticleView(
        id=7, title="What are the best GPU rental tools right now?",
        url="https://reddit.com/r/LocalLLaMA/comments/7", source_name="Reddit LocalLLaMA RSS",
        source_type="rss", category="AI Infrastructure", subcategory=None,
        summary="For those who can't run locally, use cloud subs or api.",
        summary_zh=None, title_zh="现在租 GPU 跑模型有什么好用的工具？",
        published_at=datetime.now(timezone.utc).replace(tzinfo=None), final_score=61)

    assert row.display_summary == "For those who can't run locally, use cloud subs or api."
    assert row.translated_summary is None
    assert row.display_line == row.title_zh, "an English chat line must not become the headline"

    block = "\n".join(section_blocks([row], config=get_config(), tz_name="UTC"))
    assert "现在租 GPU 跑模型有什么好用的工具" in block
    assert "cloud subs" in block, "the English summary should be shown, not dropped"

    # A translated summary still wins the headline slot.
    translated = ArticleView(**{**row.__dict__, "id": 8, "summary_zh": "本地跑不了就用云 API。"})
    assert translated.display_line == "本地跑不了就用云 API。"
    assert translated.display_summary == "本地跑不了就用云 API。"


@pytest.mark.asyncio
async def test_an_exhausted_route_warns_once_not_once_per_call():
    """The digest asks twice per send; a whole evening of quota made noise twice."""
    import logging

    from app.services.translate import Translator

    translator = Translator(get_config())
    logger = logging.getLogger("news.llm")   # translate.py logs under the llm subsystem
    records: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Collect()
    logger.addHandler(handler)
    try:
        translator._mark_route_down("mymemory", 3600)
        translator._mark_route_down("mymemory", 3600)
    finally:
        logger.removeHandler(handler)

    warnings = [m for m in records if "out of free quota" in m]
    assert len(warnings) == 1, warnings
    assert "stay in English" in warnings[0]


# ---------------------------------------------------------------- 义项错译
def test_model_translated_as_runway_model_is_corrected_only_when_english_says_model():
    from app.services.translate import fix_wrong_sense

    fixed = fix_wrong_sense("OpenAI暂停其“最有能力模特”的培训",
                            "OpenAI pauses training of its ‘most capable models’")
    assert fixed == "OpenAI暂停其“最有能力模型”的培训", fixed
    assert "模特" not in fixed, "错译要真的消失，不是旁边加一句说明"
    assert fix_wrong_sense("Jev风格模特排行榜？", "Jev style model leaderboard?") == "Jev风格模型排行榜？"


def test_the_boundary_rule_keeps_a_genuine_fashion_word_alone():
    from app.services.translate import fix_wrong_sense

    # "Supermodel" 里那串 model 不是一个独立词：不该触发修正
    assert fix_wrong_sense("超级模特的日程表", "A supermodel's schedule") == "超级模特的日程表"
    # 英文里根本没有 model，中文的模特就不是错译
    assert fix_wrong_sense("走秀模特穿上新面料", "Runways show off new fabric") == "走秀模特穿上新面料"


def test_the_diminutive_form_is_not_left_with_a_dangling_child_suffix():
    from app.services.translate import fix_wrong_sense

    assert fix_wrong_sense("最有能力的模特儿", "the most capable models") == "最有能力的模型"
    assert "模型儿" not in fix_wrong_sense("模特儿", "the model")


def test_agentic_ai_is_not_rendered_as_a_human_representative():
    from app.services.translate import fix_wrong_sense

    assert fix_wrong_sense("代理人工智能正在改变研究的方式",
                           "Agentic AI is changing how research is done") == "自主智能体正在改变研究的方式"


def test_token_is_measured_and_deliberately_left_alone():
    """量过之后不动手的那些：改成"对的"反而会把对的改错。"""
    from app.services.translate import fix_wrong_sense

    assert fix_wrong_sense("生成每个LLM令牌宽度相同的字体",
                           "Generate fonts where every LLM token is the same width") == \
        "生成每个LLM令牌宽度相同的字体"


def test_ai_agent_collocations_are_fixed_but_a_legal_agent_is_left_alone():
    """v1.40：把"见 agent 就改"收窄成"见 AI 搭配才改"。

    改动的根据是真库 7 行：#484 `agent swarms`→"代理人群"、#394 `AI safety ... agents`→
    "代理人可能会将监督视为障碍" 是错义；而 #323
    `Feds Target AI Critics as "Foreign Agents"`→"外国代理人" **是对的**，必须不动。
    """
    from app.services.translate import fix_wrong_sense

    assert fix_wrong_sense("几个月来， OpenAI的代理人群一直在攻击在线数据库",
                           "For months, OpenAI’s agent swarms have been attacking online databases"
                           ) == "几个月来， OpenAI的智能体群一直在攻击在线数据库"
    assert fix_wrong_sense("人工智能安全的一个核心问题是，代理人可能会将监督视为障碍",
                           "A central concern in AI safety is that agents may treat oversight as an "
                           "obstacle") == "人工智能安全的一个核心问题是，智能体可能会将监督视为障碍"
    # 法律/人事意义的"代理人"：整条规则让开
    assert fix_wrong_sense("美联储将人工智能批评者定位为“外国代理人”",
                           'Feds Target AI Critics as "Foreign Agents"') == \
        "美联储将人工智能批评者定位为“外国代理人”"
    # 英文没有 AI 搭配，就不该动手（哪怕中文里有"代理人"）
    assert fix_wrong_sense("他是我们家的代理人", "He is our family agent") == "他是我们家的代理人"


def test_the_human_agent_carveout_is_the_only_thing_between_two_senses():
    """构造用例（库里还没有这样的一行），专门盯"豁免"本身。

    上面 #323 那条真实行**测不到豁免**：它的英文里没有 AI 搭配，规则根本不会触发。
    真要把"外国代理人"保住，得是同一行里既有 AI 搭配、又有法律意义的人类代理人 ——
    这时候只有豁免表拦得住，否则会被一起换成"外国智能体"。
    """
    from app.services.translate import AGENT_HUMAN_ONLY, fix_wrong_sense

    assert fix_wrong_sense("文件说这些 AI agents 被用来识别外国代理人",
                           "The filing says AI agents were used to flag foreign agents") == \
        "文件说这些 AI agents 被用来识别外国代理人"
    # 同一句里没有人类代理的说法时，就该换
    assert fix_wrong_sense("这些代理人被用来识别风险",
                           "These AI agents were used to flag risk") == \
        "这些智能体被用来识别风险"
    assert "外国代理人" in AGENT_HUMAN_ONLY


def test_the_display_layer_applies_the_correction_without_touching_the_database():
    """库里那两行不回写：修正发生在渲染层，一次改错不影响数据。"""
    from app.services.news import ArticleView

    view = ArticleView(id=697, title="OpenAI pauses training of its ‘most capable models’",
                       title_zh="OpenAI暂停其“最有能力模特”的培训",
                       summary="It happens.", summary_zh="它的模型破坏了遏制。",
                       source_name="The Verge AI", source_type="rss", category="AI Models",
                       subcategory="GPT",
                       final_score=65.8, published_at=None, url="https://example.org/697")
    assert "模特" not in view.display_title
    assert "最有能力模型" in view.display_title
    view.meta["key_points_zh"] = ["最有能力模特被暂停训练"]
    view.key_points = ["The most capable models are paused"]
    assert "模特" not in view.display_key_points[0]


# ------------------- 09-28 早报里那两个 model 的第三个义项：车型 / 机型
def test_car_and_aircraft_type_are_the_wrong_sense_of_model():
    from app.services.translate import fix_wrong_sense

    fixed = fix_wrong_sense("哪些本地车型听起来最少“Claude”",
                            "Which Local Models are the least 'Claude' sounding")
    assert fixed == "哪些本地模型听起来最少“Claude”", fixed
    fixed2 = fix_wrong_sense("在 Apple Silicon 上运行微型机型",
                             "Run tiny models on Apple Silicon")
    assert "机型" not in fixed2 and "模型" in fixed2, fixed2


def test_the_new_sense_rules_stay_gated_on_the_english_word():
    """没有 model 就不许动手：真的在说汽车/飞机的时候，那是正确译法。"""
    from app.services.translate import fix_wrong_sense

    keep_car = fix_wrong_sense("这款新车型的风阻更低", "Aleph Alpha unveils a sleeker body")
    assert "车型" in keep_car, keep_car
    keep_plane = fix_wrong_sense("该机型将搭载新发动机", "The fleet gets a new engine")
    assert "机型" in keep_plane, keep_plane


def test_a_bare_repo_slug_gets_a_chinese_frame_instead_of_english():
    """MT 对光杆 slug 一个字都不返回（实测 {}），所以它必须被模板接住。"""
    from app.services.translate import localize_title, needs_translation

    slug = "LuffyTheFox/Swift-Qwen3.8-27B-Genesis-GGUF"
    assert localize_title(slug) == "项目：LuffyTheFox/Swift-Qwen3.8-27B-Genesis-GGUF"
    assert not needs_translation(slug), "既然模板能给出中文，就不该再花免费额度"


def test_the_slug_template_does_not_swallow_prose_or_numbers():
    from app.services.translate import localize_title

    assert localize_title("2024/01 revenue report on AI models") is None
    assert localize_title("OpenAI announces a new reasoning model") is None
    assert localize_title("12/34") is None, "纯数字不是仓库名"


# ---------------- 09-28 量到的另外三个错义：特工 / 光学 / 把站点名译成中文
def test_ai_agents_are_not_agents_of_espionage():
    from app.services.translate import fix_wrong_sense

    got = fix_wrong_sense("OpenAI特工试图“蛮力”联合国网站",
                          "OpenAI agents tried to ‘bruteforce’ a UN website")
    assert got == "OpenAI智能体试图“蛮力”联合国网站", got
    got2 = fix_wrong_sense("没有“流氓”人工智能特工", 'There are no "rogue" AI agents')
    assert "特工" not in got2 and "智能体" in got2, got2


def test_the_human_agent_carveout_covers_the_new_word_too():
    """真说"外国特工"的时候必须让开——那是人，不是智能体。"""
    from app.services.translate import fix_wrong_sense

    keep = fix_wrong_sense("他被指控为外国特工", "He was charged as a foreign agent")
    assert "外国特工" in keep, keep


def test_optics_is_appearance_here_not_a_physics_lab():
    from app.services.translate import fix_wrong_sense

    got = fix_wrong_sense("OpenAI 担心黑客新闻中可能出现的“光学”内容",
                          'OpenAI Feared "Optics" of what might appear on Hacker News')
    assert "光学" not in got and "观感" in got, got
    assert "Hacker News" in got and "黑客新闻" not in got, got
    keep = fix_wrong_sense("这组光学实验用了新透镜", "The lab built a new lens setup")
    assert "光学" in keep, keep


@pytest.mark.asyncio
async def test_a_declined_line_stops_being_re_asked_after_three_rounds(session):
    """翻不动的行不能每轮重问：它把免费额度吃光，连能翻的行也跟着变英文。"""
    from app.database import repository as repo
    from app.processing.pipeline import translate_pending
    from app.services.translate import Translator, get_translator, reset_translator

    ids = seed(session, ("OpenAI releases GPT-5 for everyone", "https://openai.com/gpt5"),
               ("Naive-N0.5-Flash - 309B-A15.5B", "https://reddit.com/r/naive"))
    client = StubClient({"OpenAI releases GPT-5 for everyone": "OpenAI 面向所有人发布 GPT-5"})
    reset_translator()
    translator = Translator(get_config())
    translator.provider = "mymemory"
    translator._http = lambda: _coro(client)
    for _ in range(5):
        await translate_pending(session, config=get_config(), translator=translator)
        session.commit()
    reset_translator()

    with session_scope() as s:
        stuck = s.get(Article, ids[1])
        assert stuck.title_zh == stuck.title, f"三轮拒绝后该退出队列：{stuck.title_zh!r}"
        assert stuck.translated_by == "source"
        assert (stuck.meta or {}).get("zh_misses", {}).get("title", 0) == 3
        queued = {a.id for a in repo.untranslated_articles(s, limit=50)}
        assert ids[1] not in queued, "它还在队列里，下一轮又会花一次额度"
    asked = [c for c in client.calls if c.strip() == "Naive-N0.5-Flash - 309B-A15.5B"]
    assert len(asked) == 3, f"问了几次：{len(asked)}，上限应是 3"
    # 已经翻好的标题不该在后面的轮里再问一遍（那是白花的额度）。
    # 摘要另说：这个桩从不回答摘要，它每轮都该被问。
    repeated = [c for c in client.calls
                if "for everyone" in c and "summary sentence" not in c]
    assert len(repeated) == 1, f"标题被反复重问 {len(repeated)} 次"


@pytest.mark.asyncio
async def test_a_quota_outage_is_not_counted_as_a_refusal(session):
    """429（线路被标退避）不等于"这行翻不动"：一次额度耗尽就把英文永久冻住是错的。"""
    from app.database.database import session_scope as _scope
    from app.processing.pipeline import translate_pending
    from app.services.translate import Translator, reset_translator

    ids = seed(session, ("Anthropic ships Claude Opus 4.6", "https://anthropic.com/opus46"))
    client = StubClient({})
    client.get = client.quota_exceeded          # 和线上那条 "out of free quota" 同一形状
    reset_translator()
    translator = Translator(get_config())
    translator.provider = "mymemory"
    translator._http = lambda: _coro(client)
    for _ in range(4):
        await translate_pending(session, config=get_config(), translator=translator)
        session.commit()
    reset_translator()

    with _scope() as s:
        row = s.get(Article, ids[0])
        print('DBG settled:', repr(row.title_zh), 'by', row.translated_by,
              'misses', (row.meta or {}).get('zh_misses'), 'down', translator._down_until)
        assert not row.title_zh, f"额度耗尽不该结案：{row.title_zh!r}"
        assert not ((row.meta or {}).get("zh_misses")), "退避/挂线不该记成拒绝"


@pytest.mark.asyncio
async def test_a_title_finished_by_template_is_never_sent_again(session):
    """模板/原文结案过的标题不在 MT 缓存里，不挡住就会每轮重新花钱。

    成功的行有 `Translator.cache` 兜着（第二问不发请求），所以"只问还缺的字段"
    这件事必须用**没有缓存**的那类行来测：标题是模板拼出来的、还欠一条摘要。
    """
    from datetime import timedelta

    from app.database import repository as repo
    from app.processing.normalize import build_article
    from app.processing.pipeline import translate_pending
    from app.services.translate import Translator, reset_translator

    data = build_article(title="someorg/somerepo (12,345 stars) — a new open model",
                         url="https://github.com/somerepo", source_name="GitHub Trending",
                         content="Release notes for the model.",
                         published_at=datetime.utcnow() - timedelta(hours=1))
    row = repo.save_article(session, data)
    row.is_processed = True
    row.final_score = 70
    row.title_zh = "someorg/somerepo 收获 12,345 星"      # 模板结案的标题
    row.summary = "This repository ships a new open model for agents."  # 还欠摘要
    session.commit()
    row_id = row.id

    client = StubClient({})                     # 什么都不答：摘要会一次一次被问
    reset_translator()
    translator = Translator(get_config())
    translator.provider = "mymemory"
    translator._http = lambda: _coro(client)
    for _ in range(2):
        await translate_pending(session, config=get_config(), translator=translator)
        session.commit()
    reset_translator()

    sent_titles = [c for c in client.calls if "somerepo (12,345" in c and "repository" not in c]
    assert not sent_titles, f"已结案的标题又被发出去 {len(sent_titles)} 次：{sent_titles[:2]}"
    with session_scope() as s:
        assert s.get(Article, row_id).title_zh == "someorg/somerepo 收获 12,345 星"


@pytest.mark.asyncio
async def test_a_spent_run_budget_is_not_counted_as_a_refusal(session):
    """请求根本没发出去（本轮额度用完了）就不是"这行被拒了"——一次计数都不能记。"""
    from app.processing.pipeline import translate_pending
    from app.services.translate import Budget, Translator, reset_translator

    ids = seed(session, ("Which local models sound the least Claude-like today",
                         "https://reddit.com/r/naive-nobody-answers"))
    client = StubClient({})
    reset_translator()
    translator = Translator(get_config())
    translator.provider = "mymemory"
    translator._http = lambda: _coro(client)
    translator.budget = Budget(per_run=0, per_day=0)      # 一次也发不出去
    for _ in range(4):
        await translate_pending(session, config=get_config(), translator=translator)
        session.commit()
    reset_translator()

    assert not client.calls, "额度为 0  yet 仍然发出了请求，那这条用例的前提就失效了"
    with session_scope() as s:
        row = s.get(Article, ids[0])
        assert not row.title_zh, f"没发出去的请求不该结案：{row.title_zh!r}"
        assert not ((row.meta or {}).get("zh_misses")), "没问出去却被记成拒绝"


# --------------------------- 简报里的 🔥/⭐/🔹 按这一页的名次，不按绝对分数线
TONIGHT = [78.0, 78.0, 78.0, 73.9, 73.5, 68.9, 65.6, 65.6]     # 09-28 20:00 实发的那 8 条


def test_a_clustered_page_still_shows_a_ladder():
    """09-28 晚报 8 条分数全在 65.6-78.0，绝对分线下 8/8 都是 🔥。"""
    from app.services.format import score_emoji

    marks = [score_emoji(s, cohort=TONIGHT) for s in TONIGHT]
    assert marks.count("🔥") < len(marks), f"整页同一个标记等于没有标记：{marks}"
    assert {"🔥", "⭐", "🔹"} <= set(marks), marks
    assert marks[0] == "🔥" and marks[-1] == "🔹", marks


def test_the_same_score_means_different_things_on_different_pages():
    """同一个分数在强页里不该还是 🔥——它本来就是相对名次。"""
    from app.services.format import score_emoji

    weak = score_emoji(70.0, cohort=[70.0, 61.0, 52.0])
    strong = score_emoji(70.0, cohort=[88.0, 85.0, 80.0, 70.0])
    assert weak == "🔥" and strong != "🔥", (weak, strong)


def test_a_row_under_the_floor_is_never_promoted_by_ranking():
    from app.services.format import score_emoji

    assert score_emoji(20.0, cohort=[20.0, 19.0, 18.0]) == "▫️"


def test_a_one_or_two_row_page_falls_back_to_the_absolute_bars():
    """凑不出名次的一两行页，只能照绝对线判——hot/star 两条线因此仍然有人读。"""
    from app.config import get_config
    from app.processing import breaking
    from app.services.format import score_emoji

    hot = breaking.emoji_bars(get_config(), ai_enabled=False)[0]
    assert score_emoji(hot + 1, cohort=[hot + 1]) == "🔥"
    assert score_emoji(50.0, cohort=[50.0]) == "🔹"          # 在 dot 线上、够不到 star 线


def test_a_rendered_briefing_shows_three_different_marks(session):
    """走真正的渲染函数，不只看 score_emoji。"""
    from app.services import format as fmt

    views = [make_view(id=10 + i, title=f"OpenAI ships model number {i} for agents",
                       title_zh=f"OpenAI 发布第 {i} 号模型",
                       summary_zh="推理更便宜，上下文更大。",
                       final_score=s) for i, s in enumerate(TONIGHT)]
    blocks = fmt.section_blocks(views, config=get_config())
    joined = "\n".join(blocks)
    for mark in ("🔥", "⭐", "🔹"):
        assert mark in joined, f"渲染出来的简报里没有 {mark}：{joined[-260:]}"


def test_a_rendered_news_list_uses_the_same_ladder():
    """列表页（/最新 带分数）是另一个调用点，也得真的拿到 cohort。"""
    from app.services import format as fmt

    views = [make_view(id=20 + i, title=f"Anthropic model {i} announced for enterprises",
                       title_zh=f"Anthropic 发布第 {i} 号模型",
                       final_score=s) for i, s in enumerate(TONIGHT[:5])]
    text = fmt.news_list(views, config=get_config(), show_scores=True)
    marks = [m for m in ("🔥", "⭐", "🔹") if m in text]
    assert len(marks) >= 2, f"列表页只出现 {marks}：{text[:200]}"


# ------------------- 核心内容回补：窗口曾被"已做完的高分行"永久堵死
def _points_row(session, title: str, url: str, points, *, score: float,
                meta: dict | None = None) -> int:
    """A row whose headline is settled, so only the 要点 layer has work left."""
    from app.processing.normalize import build_article

    data = build_article(title=title, url=url, source_name="OpenAI",
                         content=f"{title}. Details about the AI model release and benchmarks.",
                         published_at=datetime.now(timezone.utc).replace(tzinfo=None))
    row = repo.save_article(session, data)
    row.is_processed = True
    row.filtered_out = False
    row.category = "AI Models"
    row.final_score = score
    row.key_points = list(points)
    row.title_zh = title
    row.summary_zh = "推理更便宜，上下文更大。"
    if meta:
        row.meta = dict(meta)
    session.commit()
    return int(row.id)


def _points_translator(mapping, **kwargs) -> "Translator":
    """A fresh translator for the 要点 pass alone.

    Deliberately not driven through `translate_pending`: the headline pass shares
    one Translator, and in this module's database other tests' English titles come
    back refused, which puts the route in backoff before the bullets are ever
    reached. That would test the wiring, not the queue.
    """
    client = StubClient(mapping, **kwargs)
    translator = Translator(get_config())
    translator.provider = "mymemory"
    translator._http = lambda: _coro(client)
    return translator


async def _run_points_pass(session, translator, cfg) -> set[int]:
    from app.processing.pipeline import _translate_key_points

    pointed = await _translate_key_points(session, translator, cfg)
    session.commit()
    return pointed


def _points_config(**overrides):
    """Temporarily retune translate.* - `raw` is what AppConfig.get reads."""
    cfg = get_config()
    node = cfg.raw.setdefault("translate", {})
    saved = {key: node.get(key) for key in overrides}
    node.update(overrides)
    return cfg, saved


def _restore_points_config(saved: dict) -> None:
    node = get_config().raw.setdefault("translate", {})
    for key, value in saved.items():
        if value is None:
            node.pop(key, None)
        else:
            node[key] = value


@pytest.mark.asyncio
async def test_finished_rows_cannot_starve_the_key_points_window(session):
    """窗口 LIMIT 必须排除"已经翻好"的行，否则高分那批永远占着名额。

    线上 2026-09-29 00:16 实测：要点回补的扫描窗口 120 行里 95 行已完成、**0 行还能问**，
    下面还压着 687 行没翻。旧写法是"取分数最高的 N 行有要点的行"再在 Python 里剔除
    已完成的——分数 59~61 那批永远进不到窗口里。修好后两轮就把 163 → 191 行翻了出来。
    """
    cfg, saved = _points_config(points_scan_rows=3, points_per_run=8)
    try:
        for i in range(3):
            _points_row(session, f"OpenAI releases tuned model {i}", f"https://openai.com/starv-{i}",
                        ["Tuned benchmark sentence one."], score=90 - i,
                        meta={"key_points_zh": ["已翻好的中文要点。"]})
        late = _points_row(session, "Anthropic ships Claude 5 for everyone",
                           "https://anthropic.com/starv-late",
                           ["Claude 5 scores higher on coding benchmarks."], score=87)
        translator = _points_translator({"Claude 5 scores higher on coding benchmarks.":
                                         "Claude 5 在编程基准上得分更高。"})
        await _run_points_pass(session, translator, cfg)
        session.expire_all()
        row = session.get(Article, late)
        assert (row.meta or {}).get("key_points_zh") == ["Claude 5 在编程基准上得分更高。"], \
            f"高分那几行做完就把窗口占满，第 4 名永远轮不到：{(row.meta or {}).get('key_points_tried')}"
    finally:
        _restore_points_config(saved)


@pytest.mark.asyncio
async def test_a_route_outage_is_not_counted_as_a_refusal_for_bullets(session):
    """线路挂了（一行都没答）不该消耗那行的两次机会——和 v1.51 对标题的处理一致。"""
    cfg, saved = _points_config(points_scan_rows=20, points_per_run=8)
    try:
        row_id = _points_row(session, "Google announces a Gemini refresh today",
                             "https://blog.google/outage-bullets",
                             ["Gemini adds a longer context window."], score=99)
        translator = _points_translator({}, fail_with=RuntimeError("upstream down"))
        await _run_points_pass(session, translator, cfg)
        session.expire_all()
        meta = session.get(Article, row_id).meta or {}
        assert not meta.get("key_points_tried"), f"没问出去却被记成拒绝：{meta}"
        assert not meta.get("key_points_zh")
    finally:
        _restore_points_config(saved)


@pytest.mark.asyncio
async def test_bullets_that_never_entered_the_batch_are_not_charged(session):
    """`points_per_run` 只送前若干条字符串；没被送出去的行不该被记一次失败。"""
    cfg, saved = _points_config(points_scan_rows=20, points_per_run=1)
    try:
        first = _points_row(session, "Microsoft unveils an Azure AI tool today",
                            "https://microsoft.com/batch-a",
                            ["Azure adds a new inference endpoint."], score=99)
        second = _points_row(session, "Amazon reports an AWS cost cut today",
                             "https://amazon.com/batch-b",
                             ["AWS cutting storage prices by a third."], score=98)
        translator = _points_translator({"Azure adds a new inference endpoint.": "Azure 新增推理端点。"})
        await _run_points_pass(session, translator, cfg)
        session.expire_all()
        assert (session.get(Article, first).meta or {}).get("key_points_zh") == ["Azure 新增推理端点。"]
        meta = session.get(Article, second).meta or {}
        assert not meta.get("key_points_tried"), f"这条根本没被送翻，却记了一次失败：{meta}"
    finally:
        _restore_points_config(saved)


@pytest.mark.asyncio
async def test_bullets_that_need_no_translation_leave_the_window(session):
    """本来就是中文的要点要结案退出队列，不然它每轮都占一个窗口名额。"""
    from app.database import repository as repo_mod

    cfg, saved = _points_config(points_scan_rows=20, points_per_run=8)
    try:
        row_id = _points_row(session, "DeepMind publishes a new paper today",
                             "https://deepmind.com/native-bullets",
                             ["这篇论文说明新的推理方法。", "已经在中文里了。"], score=99)
        translator = _points_translator({})
        await _run_points_pass(session, translator, cfg)
        session.expire_all()
        meta = session.get(Article, row_id).meta or {}
        assert meta.get("key_points_zh") == [], f"无翻可做的行该结案：{meta}"
        ids = [int(r.id) for r in repo_mod.rows_needing_key_points(session, limit=40)]
        assert row_id not in ids, "结案的行还在队列里"
    finally:
        _restore_points_config(saved)


# ------------------- 免费额度记账：走流量最多的那条路由以前根本不计数
GOOGLE_URL = "translate.google.com/translate_a/t"


@pytest.mark.asyncio
async def test_a_google_batch_is_charged_as_the_one_request_it_is():
    """一批字符串一次请求，那就记一次——`daily_budget` 说的就是请求数。

    线上 2026-09-29 夜：标题翻译 23/26 轮走的是 Google，要点回补 1044 条字符串也走它，
    而 `_via_google` 从来不碰 `self.budget`——额度保护看着有，其实只盯着几乎没在干活的
    那条 MyMemory 路由。
    """
    client = ChainClient(google_body={
        "OpenAI ships GPT-7 with a bigger window": "OpenAI 发布 GPT-7，上下文更长",
        "Anthropic raises the Claude limit": "Anthropic 提高 Claude 上限",
    })
    service = translator_with(client)
    service.provider = "google"
    before_run, before_day = service.budget.used_run, service.budget.used_day
    got = await service.translate_many(
        ["OpenAI ships GPT-7 with a bigger window", "Anthropic raises the Claude limit"],
        hint="title")
    assert len(got) == 2, got
    assert service.budget.used_run - before_run == 1, \
        f"两条字符串一次请求，只该记 1 次：{service.budget}"
    assert service.budget.used_day - before_day == 1


@pytest.mark.asyncio
async def test_a_spent_budget_keeps_the_google_request_from_leaving():
    """额度用完就该闭嘴：一条请求都不该发出去，否则配额是被我们打爆的。"""
    client = ChainClient(google_body={"OpenAI ships GPT-7": "OpenAI 发布 GPT-7"})
    service = translator_with(client)
    service.provider = "google"
    service.budget.per_day = 1
    assert service.budget.available(), "先让日期落到今天"
    service.budget.used_day = 1
    assert await service.translate_many(["OpenAI ships GPT-7"], hint="title") == {}
    assert not any(GOOGLE_URL in url for url in client.calls), \
        f"每天额度已经用完还是发了请求：{client.calls}"


# ------------------- 每天额度要跨进程：重启不是新的一天
def test_the_daily_budget_survives_a_restart(tmp_path):
    """上一个进程问掉的那几百次请求，重启之后还得算数。

    线上 2026-09-29 数过：这台机器在同一个 UTC 日里启动了 3 次（00:30、00:39、06:39），
    而 `used_day` 只在内存里——每次重启都白送 400 次匿名额度，v1.55 刚把上限做成真的，
    又被重启乘了一遍。`github_rate.json` 早就是同样的问题、同样的解法。
    """
    from app.services.translate import Budget

    path = tmp_path / "translate_budget.json"
    first = Budget(per_run=60, per_day=400, path=path)
    assert first.available()
    first.spend(120)
    assert first.save() is True

    second = Budget(per_run=60, per_day=400, path=path)
    assert second.load() == 120, "重启后没把今天的用量读回来"
    assert second.available() and second.used_day == 120


def test_a_restart_does_not_hand_back_a_spent_daily_cap(tmp_path):
    from app.services.translate import Budget

    path = tmp_path / "translate_budget.json"
    first = Budget(per_run=60, per_day=5, path=path)
    first.available()
    first.spend(5)
    first.save()

    second = Budget(per_run=60, per_day=5, path=path)
    second.load()
    assert not second.available(), "重启把用完的每天额度又充满了，等于没有上限"


def test_yesterdays_counter_does_not_leak_into_today(tmp_path):
    from datetime import datetime, timezone

    from app.services.translate import Budget

    path = tmp_path / "translate_budget.json"
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    path.write_text(json.dumps({"day": yesterday, "used_day": 400}), encoding="utf-8")

    budget = Budget(per_run=60, per_day=400, path=path)
    assert budget.load() == 0, "昨天用完的额度不该压住今天"
    assert budget.available()


def test_a_budget_file_that_cannot_be_written_never_breaks_translation(tmp_path):
    """目录、只读、坏 JSON 都不能让一轮翻译炸掉——额度记录是附属信息。"""
    from app.services.translate import Budget

    directory = tmp_path / "not-a-file"
    directory.mkdir()
    budget = Budget(per_run=60, per_day=400, path=directory)
    budget.available()
    budget.spend(3)
    assert budget.save() is False, "写不进去要安静返回，不是抛异常"
    assert budget.used_day == 3

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    fresh = Budget(per_run=60, per_day=400, path=broken)
    assert fresh.load() == 0, "状态文件坏了就当作没记过，继续翻译"
    assert fresh.available()


@pytest.mark.asyncio
async def test_the_translator_reads_the_persisted_counter_at_startup(tmp_path):
    """接线也要测：Translator 建立时真的去读那个文件。"""
    from datetime import datetime, timezone

    from app.services.translate import Budget

    cfg = get_config()
    path = cfg.settings.data_path / "translate_budget.json"
    seed = Budget(per_run=60, per_day=400, path=path)
    seed.day_of = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    seed.used_day = 400
    assert seed.save()
    try:
        service = Translator(cfg)
        assert service.budget.used_day == 400, "新进程没把今天的用量读回来"
        assert not service.budget.available()
        client = ChainClient(google_body={"OpenAI ships GPT-9": "OpenAI 发布 GPT-9"})
        service.provider = "google"
        service._http = lambda: _coro(client)
        assert await service.translate_many(["OpenAI ships GPT-9"], hint="title") == {}
        assert not client.calls, "读回来的额度没能挡住请求"
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_what_a_round_spent_is_written_back():
    """花掉的额度要落到文件里，否则下一个进程读到的永远是 0。"""
    cfg = get_config()
    path = cfg.settings.data_path / "translate_budget.json"
    path.unlink(missing_ok=True)
    try:
        service = Translator(cfg)
        client = ChainClient(google_body={"OpenAI ships GPT-8": "OpenAI 发布 GPT-8"})
        service.provider = "google"
        service._http = lambda: _coro(client)
        got = await service.translate_many(["OpenAI ships GPT-8"], hint="title")
        assert got, "这一轮什么都没翻出来，测不到记账"
        assert path.exists(), "请求发了，但额度文件根本没写出来"
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert int(stored["used_day"]) >= 1, f"额度文件里还是空的：{stored}"
    finally:
        path.unlink(missing_ok=True)
