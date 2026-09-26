"""Tests for the live "what is free right now" half of /免费.

No network: `_fetch` is stubbed, and the state file goes to tmp_path.
"""

from __future__ import annotations

import dataclasses
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.services import format as fmt
from app.services.free_models import FreeModelWatcher, _all_zero, get_free_model_watcher

PAYLOAD = {"data": [
    {"id": "z-ai/glm-5.2:free", "name": "Z.ai: GLM 5.2 (free)", "created": 1_781_631_930,
     "context_length": 32768,
     "pricing": {"prompt": "0", "completion": "0", "request": "0"}},
    {"id": "qwen/qwen3.8-27b:free", "name": "Qwen: Qwen3.8 27B (free)",
     "created": 1_755_129_600, "context_length": 262144,
     "pricing": {"prompt": "0", "completion": "0"}},
    # Paid on purpose: a non-zero image token makes the model NOT free.
    {"id": "google/lyria-3-pro-preview", "name": "Google: Lyria 3 Pro Preview",
     "created": 1_755_000_000, "context_length": 8192,
     "pricing": {"prompt": "0", "completion": "0", "image": "0.004"}},
    {"id": "openrouter/free", "name": "Free Models Router", "created": 1_755_999_999,
     "context_length": 128000, "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "stealth/space-bunny-alpha", "name": "Space Bunny Alpha", "created": 1_758_500_000,
     "context_length": 131072, "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "deepseek/deepseek-v3:free", "name": "DeepSeek V3 (free)",
     "created": 1_750_000_000, "context_length": 65536,
     "pricing": {"prompt": "0", "completion": "0"}},
]}


@pytest.fixture
def wired(config, tmp_path, monkeypatch):
    """A watcher on real vocabulary but a tmp state file and a stubbed transport."""
    # A synthetic config: it deliberately asks for exclusions to prove the
    # mechanism works. The shipped default in config/settings.yaml excludes only
    # the meta-router, because alpha/preview models really are free right now.
    models_cfg = {
        "enabled": True,
        "name": "OpenRouter",
        "url": "https://openrouter.ai/api/v1/models",
        "max_items": 3,
        "cache_max": 80,
        "exclude": ["openrouter/free", "preview", "alpha"],
        "state_file": str(tmp_path / "free_models.json"),
    }
    free = {**config.raw.get("free", {}), "models": models_cfg}
    cfg = dataclasses.replace(config, raw={**config.raw, "free": free})
    calls = {"n": 0}

    async def fake_fetch(self):
        calls["n"] += 1
        return json.loads(json.dumps(PAYLOAD))

    monkeypatch.setattr(FreeModelWatcher, "_fetch", fake_fetch)
    return FreeModelWatcher(cfg), calls


def test_all_zero_requires_every_price_to_be_zero():
    assert _all_zero({"prompt": "0", "completion": "0"})
    assert not _all_zero({"prompt": "0", "image": "0.004"})
    assert not _all_zero({})
    assert not _all_zero({"prompt": "0.0005"})


async def test_snapshot_keeps_only_true_free_models(wired):
    watcher, _ = wired
    models = await watcher.snapshot()
    ids = [m.id for m in models]
    assert "z-ai/glm-5.2:free" in ids
    assert "deepseek/deepseek-v3:free" in ids
    assert "google/lyria-3-pro-preview" not in ids     # priced images
    assert "openrouter/free" not in ids                 # router, excluded
    assert "stealth/space-bunny-alpha" not in ids       # excluded by this fixture only


async def test_newest_listing_first_and_cache_holds_everything(wired):
    watcher, _ = wired
    models = await watcher.snapshot()
    assert models == sorted(models, key=lambda m: m.listed_at, reverse=True)
    # max_items only limits the display, never the cached pool
    assert len(models) == 3
    assert watcher.source_name == "OpenRouter"


async def test_snapshot_caches_until_ttl(wired):
    watcher, calls = wired
    await watcher.snapshot()
    await watcher.snapshot()
    assert calls["n"] == 1
    await watcher.snapshot(force=True)
    assert calls["n"] == 2
    watcher._cached_at = time.time() - 99999
    watcher._cache = []
    await watcher.snapshot()
    assert calls["n"] == 3


async def test_first_seen_date_drives_days_free(wired, tmp_path):
    watcher, _ = wired
    models = await watcher.snapshot()
    state = json.loads((tmp_path / "free_models.json").read_text(encoding="utf-8"))
    assert set(state["seen"]) >= {"z-ai/glm-5.2:free"}
    assert all(m.days_free() == 0 for m in models)   # just observed: don't invent a history

    backdated = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=9)).isoformat()
    state["seen"]["z-ai/glm-5.2:free"] = backdated
    (tmp_path / "free_models.json").write_text(json.dumps(state), encoding="utf-8")
    models = await watcher.snapshot(force=True)
    glm = [m for m in models if m.id.startswith("z-ai")][0]
    assert 8 <= glm.days_free() <= 9


async def test_unwritable_state_file_does_not_break_snapshot(wired, monkeypatch):
    watcher, _ = wired

    def boom(self):
        raise PermissionError("read-only data dir")

    monkeypatch.setattr(type(watcher.state_path), "write_text", boom, raising=True)
    assert len(await watcher.snapshot()) == 3


async def test_transport_failure_returns_empty_not_an_exception(config, tmp_path, monkeypatch):
    cfg = dataclasses.replace(
        config,
        raw={**config.raw, "free": {"models": {
            "enabled": True, "state_file": str(tmp_path / "s.json"),
            "url": "https://openrouter.ai/api/v1/models"}}},
    )

    async def boom(self):
        raise RuntimeError("")     # httpx errors often stringify empty

    monkeypatch.setattr(FreeModelWatcher, "_fetch", boom)
    watcher = FreeModelWatcher(cfg)
    assert await watcher.snapshot() == []
    assert watcher.failed is True


async def test_disabled_watcher_never_fetches(config, tmp_path, monkeypatch):
    cfg = dataclasses.replace(config, raw={**config.raw, "free": {"models": {"enabled": False}}})

    async def boom(self):  # pragma: no cover - must not run
        raise AssertionError("fetch called while disabled")

    monkeypatch.setattr(FreeModelWatcher, "_fetch", boom)
    assert await FreeModelWatcher(cfg).snapshot() == []


async def test_vendors_labels_models_from_the_free_vocabulary(wired):
    watcher, _ = wired
    models = await watcher.snapshot()
    deepseek = [m for m in models if m.id.startswith("deepseek")][0]
    assert "DeepSeek" in deepseek.vendors
    assert [m for m in models if m.id.startswith("z-ai")][0].vendors == []


# ------------------------------------------------------------------ rendering
def test_section_shows_pool_and_displayed_counts(wired):
    import asyncio

    watcher, _ = wired
    models = asyncio.run(watcher.snapshot())
    text = fmt.free_models_section(models, config=watcher.config, limit=2,
                                   total=len(models), source="OpenRouter")
    assert "共 3 个，列出最新 2 个" in text
    assert "openrouter.ai/z-ai/glm-5.2:free" in text
    assert "数据来源：OpenRouter" in text
    assert fmt.free_models_section([], config=watcher.config) == ""


def test_section_single_result_does_not_claim_a_bigger_pool(wired):
    import asyncio

    watcher, _ = wired
    models = asyncio.run(watcher.snapshot())
    glm = [m for m in models if m.id.startswith("z-ai")]
    text = fmt.free_models_section(glm, config=watcher.config, total=len(glm),
                                   source="OpenRouter")
    assert "（1 个）" in text
    assert "共" not in text


def test_get_free_model_watcher_is_a_singleton():
    assert get_free_model_watcher() is get_free_model_watcher()


# ------------------------------------------------------------- handler wiring
MODELS_ON = {
    "enabled": True, "name": "OpenRouter", "max_items": 5, "cache_max": 80,
    "exclude": ["openrouter/free", "preview", "alpha"],
    "url": "https://openrouter.ai/api/v1/models",
}


def _cfg_with(config, tmp_path, **overrides):
    models = {**MODELS_ON, "state_file": str(tmp_path / "s.json"), **overrides}
    free = {**config.raw.get("free", {}), "models": models}
    return dataclasses.replace(config, raw={**config.raw, "free": free})


async def test_free_command_includes_the_live_block(config, tmp_path, monkeypatch):
    from app.bot.handlers import free as free_handler

    async def fake_fetch(self):
        return json.loads(json.dumps(PAYLOAD))

    monkeypatch.setattr(FreeModelWatcher, "_fetch", fake_fetch)
    cfg = _cfg_with(config, tmp_path)
    monkeypatch.setattr("app.services.free_models.get_free_model_watcher",
                        lambda c=None: FreeModelWatcher(cfg))
    text, checked = await free_handler._live(cfg, term=None)
    assert checked is True
    assert "现在免费可用的模型" in text
    assert "（3 个）" in text
    filtered, _ = await free_handler._live(cfg, term="glm")
    assert "glm-5.2" in filtered
    assert "（1 个）" in filtered


async def test_live_block_is_empty_when_the_source_is_down(config, tmp_path, monkeypatch):
    from app.bot.handlers import free as free_handler

    cfg = _cfg_with(config, tmp_path)
    watcher = FreeModelWatcher(cfg)

    async def boom(self):
        raise OSError("network down")

    monkeypatch.setattr(FreeModelWatcher, "_fetch", boom)
    monkeypatch.setattr("app.services.free_models.get_free_model_watcher", lambda c=None: watcher)
    text, checked = await free_handler._live(cfg)
    assert text == "" and checked is False


def test_body_only_for_free_phrase_is_no_longer_an_offer(config):
    """/free must not fire on "I run it for free" usage talk - only on announcements."""
    from app.processing.free_offers import detect

    title = "My attempt at running a model: A short story."
    body = ("I compared chatgpt and gemini side by side and found the smaller "
            "one runs for free on my laptop hardware.")
    assert detect(title, body, config=config) is None
    assert detect("Gemini API is free for students this month", "", config=config) is not None


def test_keyword_fallback_is_labelled_as_not_an_offer(config):
    """Search hits shown under /免费 must say they are not verified promos."""
    from datetime import datetime
    from types import SimpleNamespace

    article = SimpleNamespace(
        free_offer=None, title="GLM 5.2 released with a 1M context window",
        display_title="GLM 5.2 发布", display_summary="GLM 5.2 发布，支持 1M 上下文",
        url="https://example.com/glm-release", source_name="Hacker News",
        published_at=datetime.utcnow(),
    )
    text = fmt.free_offer_list([article], config=config, days=30, tool="GLM",
                               note="🔎 没有找到“glm 免费”的明确消息，下面只是相关新闻，别当成限免。")
    assert "别当成限免" in text
    assert "GLM 5.2" in text
    # without the note the same render must not hint at anything missing
    assert "别当成限免" not in fmt.free_offer_list([article], config=config, days=30)


async def test_disabled_source_is_reported_as_not_checked(config, tmp_path):
    cfg = dataclasses.replace(config, raw={**config.raw, "free": {
        **config.raw.get("free", {}), "models": {"enabled": False}}})
    from app.bot.handlers import free as free_handler

    text, checked = await free_handler._live(cfg)
    assert text == "" and checked is False


def test_tool_scope_says_what_it_checked(config):
    """/free qoder must not read as "nothing exists" when the source was down."""
    down = fmt.free_offer_list([], config=config, days=30, tool="Qoder", live_checked=False)
    assert "没连上" in down
    empty = fmt.free_offer_list([], config=config, days=30, tool="Qoder")
    assert "没有采到“Qoder”的限免公告" in empty and "没连上" not in empty
    generic = fmt.free_offer_list([], config=config, days=30)
    assert "/free deepseek" in generic


def test_fallback_items_lose_the_gift_marker(config):
    """A keyword-fallback list must not dress its rows up as 🎁 offers."""
    from datetime import datetime
    from types import SimpleNamespace

    article = SimpleNamespace(
        free_offer={"tool": "DeepSeek", "kind": "模型", "models": [], "signals": []},
        title="DeepSeek V4 released", display_title="DeepSeek V4 发布",
        display_summary="DeepSeek V4 发布", url="https://example.com/ds",
        source_name="Hacker News", published_at=datetime.utcnow(),
    )
    real = fmt.free_offer_list([article], config=config, days=30)
    fallback = fmt.free_offer_list([article], config=config, days=30,
                                   tool="DeepSeek", note="🔎 没有找到明确消息",
                                   unverified=True)
    assert "🤖" in real                      # kind 模型 gets its own emoji
    assert "🤖" not in fallback
    assert "1. 📰" in fallback
    assert "不是限免确认" in fallback


def test_an_explanatory_note_does_not_deny_the_offers(config):
    """The proactive alert passes a note; its rows are verified and keep their emoji."""
    from datetime import datetime
    from types import SimpleNamespace

    article = SimpleNamespace(
        free_offer={"tool": "Qoder", "kind": "编程 Agent/IDE", "models": [], "signals": ["限免"]},
        title="Qoder 限免一周", display_title="Qoder 限免一周",
        display_summary="Qoder 向全校师生开放", url="https://linux.do/t/9",
        source_name="Linux.do 福利分类", published_at=datetime.utcnow(),
    )
    alert = fmt.free_offer_list([article], config=config, days=7,
                                heading="🆓 刚发现的限免", note="自动监控到新出现的免费额度")
    assert "1. 💻" in alert          # real kind emoji, not the fallback marker
    assert "📰" not in alert
    assert "不是限免确认" not in alert
    assert "刚发现的限免" in alert and "自动监控" in alert


def test_tool_scope_is_escaped_into_the_html(config):
    """/free <keyword> can carry caller text; it must not inject markup."""
    from datetime import datetime
    from types import SimpleNamespace

    article = SimpleNamespace(
        free_offer={"tool": "X", "kind": "其他", "models": [], "signals": []},
        title="t", display_title="标题", display_summary="摘要", url="https://e.com",
        source_name="HN", published_at=datetime.utcnow(),
    )
    text = fmt.free_offer_list([article], config=config, days=7, tool="<b>粗</b>")
    assert "<b>粗</b>" not in text.replace("&lt;b&gt;", "")
    assert "&lt;b&gt;" in text


# ------------------------------------------------------------------ trend
def _watcher_on(config, tmp_path, monkeypatch, payload):
    async def fake_fetch(self):
        return json.loads(json.dumps(payload))

    monkeypatch.setattr(FreeModelWatcher, "_fetch", fake_fetch)
    models_cfg = {"enabled": True, "name": "OpenRouter", "max_items": 8,
                  "url": "https://openrouter.ai/api/v1/models",
                  "state_file": str(tmp_path / "trend.json")}
    cfg = dataclasses.replace(config, raw={**config.raw, "free": {
        **config.raw.get("free", {}), "models": models_cfg}})
    return FreeModelWatcher(cfg)


def _free(mid, name):
    return {"id": mid, "name": name, "created": 1_758_000_000,
            "context_length": 8192, "pricing": {"prompt": "0", "completion": "0"}}


async def test_trend_reports_new_and_ended_free_models(config, tmp_path, monkeypatch):
    payload = {"data": [_free("a/one:free", "One")]}
    watcher = _watcher_on(config, tmp_path, monkeypatch, payload)
    await watcher.snapshot(force=True)
    # the first snapshot has nothing to compare with: claiming 1 "new" is noise
    assert watcher.trend() == ([], [])

    payload["data"].append(_free("b/two:free", "Two"))
    second = _watcher_on(config, tmp_path, monkeypatch, payload)
    await second.snapshot(force=True)
    newly, ended = second.trend()
    assert [m.id for m in newly] == ["b/two:free"]
    assert ended == []

    payload["data"] = [_free("b/two:free", "Two")]
    third = _watcher_on(config, tmp_path, monkeypatch, payload)
    await third.snapshot(force=True)
    newly, ended = third.trend()
    assert newly == []
    assert ended == ["One"], "the model that stopped being free must be named"


def test_trend_section_renders_both_directions(config):
    from app.services.free_models import FreeModel

    model = FreeModel(id="z-ai/glm-9:free", name="GLM 9", provider="z-ai",
                      url="https://openrouter.ai/z-ai/glm-9:free")
    text = fmt.free_trend_section([model], ["Old Model"], source="OpenRouter")
    assert "GLM 9 开始免费" in text and "Old Model 已结束免费" in text
    assert "与上次相比的变化" in text
    assert fmt.free_trend_section([], [], source="OpenRouter") == ""


def test_trend_section_escapes_names(config):
    text = fmt.free_trend_section([], ["<b>坏名字</b>"], source="OpenRouter")
    assert "<b>坏名字</b>" not in text and "&lt;b&gt;" in text
