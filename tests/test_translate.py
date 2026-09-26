"""中文输出：翻译服务、配额、入库与显示层优先级。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.config import get_config
from app.database import repository as repo
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
# same way the service does to stay a believable stand-in.
KEEP_TERMS = [str(t) for t in (get_config().get("translate.keep_terms", []) or [])]


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
                 google_error=None):
        self.calls: list[str] = []
        self.mymemory_body = mymemory_body
        self.mymemory_status = mymemory_status
        self.google_body = google_body
        self.google_error = google_error

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
        lines = [[body, None] for body in answered if body]
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
    """One merged line for two titles must not be pasted onto the wrong news."""
    client = ChainClient(google_body={"A": "甲"})   # only one part comes back
    service = translator_with(client)
    service.provider = "google"
    assert await service.translate_many(["A", "B title that needs work"], hint="title") == {}


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
