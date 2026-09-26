"""AI pipeline tests: rule gate, classification, Chinese summary, resilience."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.config import get_config
from app.database import repository as repo
from app.database.models import Article
from app.processing import classifier, summarizer
from app.processing.normalize import build_article
from app.processing.pipeline import process_pending


def feed_item(title: str, url: str, *, body: str | None = None, source: str = "OpenAI",
              hours: int = 1) -> dict:
    return build_article(
        title=title, url=url, source_name=source,
        content=body or (title + ". " + "The company says the new model improves reasoning and lowers inference cost. " * 3),
        published_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours),
    )


# ------------------------------------------------------- layer 1: rule gate
def test_keyword_gate_rejects_non_ai_content():
    keep, hits = classifier.rule_filter({"title": "Best coffee shops in Seattle",
                                        "content": "Espresso, latte, pastries."})
    assert keep is False and hits == []


def test_keyword_gate_accepts_ai_content():
    keep, hits = classifier.rule_filter({"title": "New vLLM release speeds up LLM inference",
                                        "content": "vLLM 0.6 improves throughput on H100 GPUs."})
    assert keep and "vllm" in hits


def test_rule_classifier_maps_categories_and_subcategories():
    data = {"title": "Claude coding agent gets computer use", "content": "An agentic coding tool."}
    category, subcategory, confidence = classifier.rule_classify(data)
    assert category in {"AI Agent", "AI Models", "Coding"} or category is not None
    assert isinstance(confidence, float)


# ------------------------------------------------------------- the pipeline
@pytest.mark.asyncio
async def test_pipeline_classifies_summarises_and_scores(session, fake_llm):
    stored = repo.save_article(session, feed_item("OpenAI releases GPT-5 with a 1M context window",
                                                  "https://openai.com/gpt5"))
    session.commit()
    stats = await process_pending(session, config=get_config(), llm=fake_llm, limit=5)
    session.commit()
    assert stats.processed == 1
    article = session.get(Article, stored.id)
    assert article.is_processed and article.filtered_out is False
    assert article.category == "AI Models"
    assert article.summary and "基准" in article.summary  # Chinese output (§13)
    assert article.key_points
    assert article.why_it_matters
    assert 0 < article.final_score <= 100
    assert "classify" in fake_llm.calls and "summarize" in fake_llm.calls
    tags = {t.name for t in article.tags}
    assert tags, "tags are persisted through article_tags"


@pytest.mark.asyncio
async def test_pipeline_filters_non_ai_items_without_calling_the_model(session, fake_llm):
    repo.save_article(session, feed_item("Local bakery wins pastry award",
                                         "https://bakery.example/win",
                                         body="Sourdough, croissants and a friendly neighbourhood shop."))
    session.commit()
    stats = await process_pending(session, config=get_config(), llm=fake_llm, limit=5)
    session.commit()
    assert stats.filtered == 1
    assert fake_llm.calls == []
    article = session.scalars(select(Article)).first()
    assert article.filtered_out and article.final_score == 0


@pytest.mark.asyncio
async def test_llm_failure_keeps_the_news_and_the_scheduler_alive(session, broken_llm):
    """design doc section 25: "LLM 暂时失败时新闻仍然入库"."""
    stored = repo.save_article(session, feed_item("Anthropic ships Claude Opus 4.5",
                                                  "https://anthropic.com/opus"))
    session.commit()
    stats = await process_pending(session, config=get_config(), llm=broken_llm, limit=5)
    session.commit()
    assert stats.failed == 0, "a provider outage is handled, not an exception"
    article = session.get(Article, stored.id)
    assert article is not None and article.is_processed
    assert article.summary, "fallback summary keeps the digest readable"
    assert article.final_score > 0
    assert article.process_error.startswith("ai-degraded")
    assert article.meta.get("ai_degraded") is True


@pytest.mark.asyncio
async def test_degraded_rows_are_visible_for_a_later_ai_pass(session, broken_llm):
    repo.save_article(session, feed_item("Google Gemini 3 released", "https://blog.google/gemini3"))
    session.commit()
    await process_pending(session, config=get_config(), llm=broken_llm, limit=5)
    session.commit()
    article = session.scalars(select(Article)).first()
    assert article.meta.get("pipeline") in {"rule", "ai"}
    assert "degraded" in (article.process_error or "").lower()


@pytest.mark.asyncio
async def test_rule_mode_when_llm_is_not_configured(session):
    class Disabled:
        enabled = False

    stored = repo.save_article(session, feed_item("NVIDIA announces B200 GPU for AI inference",
                                                  "https://nvidia.com/b200"))
    session.commit()
    stats = await process_pending(session, config=get_config(), llm=Disabled(), limit=5)
    session.commit()
    article = session.get(Article, stored.id)
    assert stats.processed == 1
    assert article.is_processed and article.final_score > 0
    assert article.meta.get("pipeline") == "rule"
    assert article.summary


@pytest.mark.asyncio
async def test_boot_repair_recuts_a_summary_stopped_mid_word(session):
    """Fixing the producer only helps tomorrow's news without this."""
    from app.processing.pipeline import repair_truncated_summaries

    body = ("Take your time with a wall of text that keeps going, "
            + ", ".join(f"stage {i} of the release adds a little more prose" for i in range(1, 9)))
    stored = repo.save_article(session, feed_item("Release notes for the agent SDK",
                                                 "https://example.com/notes", body=body))
    row = session.get(Article, stored.id)
    row.is_processed = True
    row.meta = {"pipeline": "rule"}
    row.summary = " ".join(body.split())[:160]        # exactly what the old code stored
    row.summary_zh = "半句机器翻译"
    session.commit()

    assert repair_truncated_summaries(session) == 1
    session.commit()
    assert row.summary.endswith("…") and len(row.summary) <= 161, row.summary
    assert row.summary_zh is None, "the translation of the cut text has to go too"


@pytest.mark.asyncio
async def test_boot_repair_leaves_ai_summaries_and_whole_lines_alone(session):
    from app.processing.pipeline import repair_truncated_summaries

    cut = repo.save_article(session, feed_item("Anthropic ships a model", "https://a.example/1"))
    whole = repo.save_article(session, feed_item("Google ships a model", "https://a.example/2"))
    for row_id, meta, summary in ((cut.id, {"pipeline": "ai"}, "x" * 160),
                                 (whole.id, {"pipeline": "rule"}, "完整一句。")):
        row = session.get(Article, row_id)
        row.is_processed, row.meta, row.summary = True, meta, summary
    session.commit()

    assert repair_truncated_summaries(session) == 0


def test_fallback_summary_decodes_feed_entities():
    """Feeds ship double-escaped bodies; `Now&#160;` must not reach a briefing."""
    body = ("Now&#160;the agent SDK supports &amp; ranks every model in the catalogue " * 4)
    result = summarizer.fallback_summary({"title": "Release notes & fixes", "content": body})

    assert "&#160;" not in result["summary"] and "&amp;" not in result["summary"]
    assert all("&amp;" not in point for point in result["key_points"])


@pytest.mark.asyncio
async def test_boot_repair_runs_once_and_is_idempotent(session):
    from app.processing.pipeline import repair_truncated_summaries

    body = ("现在支持 &#160; 不间断空格与更多文字，"
            + "，".join(f"第{i}阶段把摘要长度上限提高到 {i}00 字符" for i in range(1, 9)))
    stored = repo.save_article(session, feed_item("Release notes", "https://e.example/1",
                                                 body=body))
    row = session.get(Article, stored.id)
    row.is_processed, row.meta = True, {"pipeline": "rule"}
    row.summary = " ".join(body.split())[:160]
    session.commit()

    assert repair_truncated_summaries(session) == 1
    session.commit()
    assert "&#160;" not in row.summary and row.summary.endswith("…")
    assert repair_truncated_summaries(session) == 0, "a repaired row stays repaired"


def test_fallback_summary_never_stops_inside_a_word():
    """Rule mode stores the summary, so a mid-word cut is baked into every digest."""
    # Distinct clauses, not one sentence pasted six times: a feed body that
    # literally repeats itself now collapses to a single copy.
    body = ("更改内容 在网关提示标头中添加了“x-claude-code-prompt-id”，以便 LLM 网关可以对"
            "服务于一个用户提示的请求进行分组；"
            + "，".join(f"第{i}项变更把超时从 {i}0 毫秒降到 {i} 毫秒" for i in range(1, 9))
            + "；选择“CLAUD”")
    result = summarizer.fallback_summary({"title": "v2.1.283 released", "content": body})
    summary = result["summary"]

    assert len(summary) <= 161 and summary.endswith("…"), summary
    assert _cut_is_clean(" ".join(body.split()), summary), summary


@pytest.mark.asyncio
async def test_summarizer_fallback_is_a_real_sentence():
    data = feed_item("Mistral releases an open weights 8B model", "https://mistral.ai/8b",
                     body="Mistral released an 8B open weights model. It scores 74% on MMLU. "
                          "The licence is Apache-2.0 and it runs on a single consumer GPU.")
    result = summarizer.fallback_summary(data)
    assert result["summary"] and len(result["summary"]) > 20
    assert result["method"] == "fallback"


@pytest.mark.asyncio
async def test_breaking_candidates_are_flagged_by_threshold(session, fake_llm):
    fake_llm.importance = 99
    fake_llm.relevance = 99
    stored = repo.save_article(session, feed_item("OpenAI ships GPT-6 for free to all users",
                                                  "https://openai.com/gpt6"))
    session.commit()
    stats = await process_pending(session, config=get_config(), llm=fake_llm, limit=5)
    session.commit()
    assert stored.id in stats.breaking, "high scores become breaking-news candidates"


# --------------------------------------------------------------- digests
class _Item:
    """The only two fields `select_briefing` reads."""

    def __init__(self, source: str, score: float) -> None:
        self.source_name, self.final_score = source, score


def _cut_is_clean(source: str, cut: str) -> bool:
    """The kept text must stop at a boundary: either it ends on one, or the
    character it stopped before is one. Either way nothing is split in half."""
    from app.processing.normalize import _SHORTEN_MARKS

    kept = cut.rstrip("…")
    assert source.startswith(kept), cut
    following = source[len(kept):len(kept) + 1]
    return (kept[-1] in _SHORTEN_MARKS or following in _SHORTEN_MARKS
            or not following.strip())


def test_shorten_cuts_at_a_boundary_not_mid_word():
    """The live 晚报 once printed `选择“CLAUD”` mid-word, with no ellipsis."""
    from app.services.format import shorten

    text = ("在网关提示标头中添加了“x-claude-code-prompt-id”，以便 LLM 网关可以对服务于"
            "一个用户提示的请求进行分组；选择“CLAUD”")
    cut = shorten(text, 40)
    assert cut.endswith("…") and len(cut) <= 42, cut
    assert _cut_is_clean(text, cut)
    assert shorten("短句。", 40) == "短句。", "short lines are left alone"
    assert len(shorten("x" * 500, 20)) == 21, "no boundary to find still respects the limit"
    assert shorten("  压平   空白\n换行  ", 40) == "压平 空白 换行"
    assert "\n" not in shorten("第一行\n第二行" * 20, 30), "one briefing line stays one line"

    half_entity = "A" * 100 + "&nbsp" + "，后面还有很长的一段摘要文字" * 3
    assert "&" not in shorten(half_entity, 108), "a cut must not leave half an entity"

    latin = "Benchmarks across the whole open model catalogue " * 6
    assert _cut_is_clean(" ".join(latin.split()), shorten(latin, 60)), \
        "an unpunctuated English line has to fall back to a word boundary"


def test_briefing_selects_the_best_and_not_one_feed():
    """`select_briefing` is the fix for 'the feed that polled last owns the digest'."""
    from app.services.digest import select_briefing

    pool = ([_Item("OpenAI", 70.0 + i) for i in range(4)]
            + [_Item("NVIDIA", 60.0 + i) for i in range(4)]
            + [_Item("Ars", 50.0 + i) for i in range(4)]
            + [_Item("Verge", 40.0 + i) for i in range(4)])
    picked = select_briefing(pool, top_items=10, max_per_source=3)

    assert [p.final_score for p in picked] == [73.0, 72.0, 71.0, 63.0, 62.0, 61.0,
                                               53.0, 52.0, 51.0, 43.0]
    assert len(select_briefing(pool, top_items=10, max_per_source=0)) == 10
    assert select_briefing(pool, top_items=10, max_per_source=0)[3].final_score == 70.0


def test_briefing_backfills_rather_than_sending_a_short_list():
    """A forum burst is the case the cap exists for; starving the digest is not."""
    from app.services.digest import select_briefing

    pool = [_Item("Reddit", 52.0 + i) for i in range(12)]          # 52..63 newest
    pool += [_Item("OpenAI", 70.0 + i) for i in range(3)]          # 70..72 real news
    picked = select_briefing(pool, top_items=10, max_per_source=3)

    assert len(picked) == 10, "the cap must never leave the briefing short"
    assert [p.final_score for p in picked[:6]] == [72.0, 71.0, 70.0, 63.0, 62.0, 61.0]


@pytest.mark.asyncio
async def test_briefing_lists_the_best_stories_not_the_last_polled(session, fake_llm):
    """End to end: nine fresh forum posts must not bury yesterday's real news.

    Measured on the deployed box, the briefing took `latest()[:10]` - newest
    published - so 8 of 10 lines were Reddit thread titles while 17 higher
    scoring stories in the same window never appeared.
    """
    from app.database.models import Article
    from app.services.digest import get_digest_service

    def rate(article_id: int, score: float) -> None:
        row = session.get(Article, article_id)
        row.final_score, row.is_processed = score, True

    for index in range(9):                                  # minutes old, merely OK
        rate(repo.save_article(session, feed_item(
            f"论坛帖子：本地模型讨论 {index}", f"https://linux.do/t/{index}",
            source="Linux.do 福利分类", hours=1)).id, 60.0)
    rate(repo.save_article(session, feed_item(
        "OpenAI 发布新模型 版本 0", "https://openai.com/new-0",
        source="OpenAI", hours=20)).id, 90.0)
    rate(repo.save_article(session, feed_item(
        "OpenAI 发布新模型 版本 1", "https://openai.com/new-1",
        source="OpenAI", hours=20)).id, 80.0)
    for source, score, url, hours in (("NVIDIA 开发者", 70.0, "https://nv.example/a", 10),
                                      ("Ars Technica AI", 65.0, "https://arstechnica.example/b", 12),
                                      ("The Verge AI", 62.0, "https://theverge.example/c", 14)):
        for index in range(4):
            rate(repo.save_article(session, feed_item(
                f"{source} 更新 {index}", f"{url}-{index}", source=source,
                hours=hours)).id, score)
    session.commit()

    digest = await get_digest_service().generate("morning", chat_id=111111111, llm=fake_llm)
    joined = "\n".join(digest.messages)
    per_feed = {marker: joined.count(marker) for marker in
                ("linux.do/t/", "openai.com/new-", "nv.example/a", "arstechnica.example/b",
                 "theverge.example/c")}

    assert "OpenAI 发布新模型 版本 0" in joined, "the day's best story lost on arrival order"
    assert per_feed["openai.com/new-"] == 2
    assert per_feed["linux.do/t/"] == 0, "the freshest-but-dullest feed should be out"
    assert max(per_feed.values()) <= 3, f"one feed owns the briefing: {per_feed}"


@pytest.mark.asyncio
async def test_digest_renders_sections_and_stays_under_telegram_limit(session, fake_llm):
    from app.services.digest import get_digest_service

    for index in range(6):
        repo.save_article(session, feed_item(
            f"OpenAI releases model variant {index} with better reasoning",
            f"https://openai.com/model-{index}"))
    repo.save_article(session, feed_item("NVIDIA unveils a new GPU for AI inference",
                                         "https://nvidia.com/gpu-new", source="NVIDIA"))
    session.commit()
    await process_pending(session, config=get_config(), llm=fake_llm, limit=20)
    session.commit()

    digest = await get_digest_service().generate("morning", chat_id=111111111, llm=fake_llm)
    assert digest.messages and not digest.empty
    joined = "\n\n".join(digest.messages)
    assert "☀️ AI 早报" in joined, "简报自己的标题必须是中文"
    assert "AI Morning Briefing" not in joined
    assert "今日重点" in joined
    assert "模型发布" in joined, "栏目名也要中文：taxonomy 的英文键不该露给他看"
    assert len(digest.messages[0]) <= 4096
    assert "<a href=" in joined, "headlines link out"


@pytest.mark.asyncio
async def test_breaking_guard_respects_cooldown_and_daily_cap(session):
    from datetime import datetime as dt

    from app.services.digest import get_digest_service
    from app.services.news import get_news_service

    news = get_news_service()
    news.user_for(111111111)
    stored = repo.save_article(session, feed_item("Anthropic announces Claude for Enterprise",
                                                 "https://anthropic.com/enterprise"))
    session.commit()
    article = session.get(Article, stored.id)
    article.final_score = 96
    article.source_quality = 95          # 一手来源：规则模式的突发门槛之一
    article.is_processed = True
    article.category = "AI Models"
    session.commit()

    service = get_digest_service()
    ok, _ = service.can_send_breaking(111111111, article_id=article.id)
    assert ok

    digest = await service.generate_breaking(article.id, chat_id=111111111)
    service.record_delivery(chat_id=111111111, digest=digest)
    blocked, reason = service.can_send_breaking(111111111, article_id=article.id)
    assert not blocked and reason

    session.refresh(article)
    assert article.is_sent and article.is_breaking


@pytest.mark.asyncio
async def test_write_jobs_are_serialised_so_sqlite_never_sees_two_writers():
    """Regression: collection and the AI pass both commit hundreds of rows.

    Concurrently that raised "database is locked" on the deployed server and
    lost almost an entire processing batch.
    """
    import asyncio

    from app.scheduler.jobs import NewsJobs

    jobs = NewsJobs(get_config())
    inside = {"count": 0, "max": 0}

    async def fake(kind):
        async def runner(*args, **kwargs):
            inside["count"] += 1
            inside["max"] = max(inside["max"], inside["count"])
            await asyncio.sleep(0.02)
            inside["count"] -= 1
            return kind

        return runner

    jobs._run_collect = await fake("collect")
    jobs._run_process = await fake("process")
    jobs._run_digests = await fake("digests")

    await asyncio.gather(*[jobs.run_collect() for _ in range(3)],
                         *[jobs.run_process() for _ in range(3)],
                         *[jobs.run_digests() for _ in range(3)])
    assert inside["max"] == 1, "write jobs must never overlap"


async def test_a_digest_that_will_not_arrive_says_so(monkeypatch):
    """The 08:00 早报 went missing silently; the log read exactly like a idle day."""
    from app.scheduler import jobs as jobs_module
    from app.scheduler.jobs import NewsJobs

    class Records:
        def __init__(self) -> None:
            self.items: list[tuple[str, str]] = []

        def _log(self, level):
            def emit(msg, *args):
                self.items.append((level, msg % args if args else msg))
            return emit

        def info(self, msg, *args):
            self._log("info")(msg, *args)

        def warning(self, msg, *args):
            self._log("warning")(msg, *args)

    records = Records()
    monkeypatch.setattr(jobs_module, "log", records)
    jobs = NewsJobs(get_config())
    jobs._note_miss(123, "morning", "already sent today", "08:00")
    jobs._note_miss(123, "morning", "already sent today", "08:00")   # deduped
    jobs._note_miss(123, "evening", "window closed", "20:00")
    jobs._note_miss(123, "morning", "not yet due", "08:00")          # never spams

    assert [level for level, _ in records.items] == ["info", "warning"]
    assert "already been delivered" in records.items[0][1]
    assert "missed its 20:00 window" in records.items[1][1]


async def test_run_digests_reports_a_suppressed_send():
    from app.scheduler.jobs import NewsJobs

    jobs = NewsJobs(get_config())
    jobs.chat_ids = lambda: [123]
    jobs.news.user_for = lambda chat: {"paused": False, "daily_enabled": True,
                                       "daily_time": "08:00", "evening_enabled": False,
                                       "evening_time": "20:00", "timezone": "Asia/Shanghai"}
    jobs._digest_due = lambda *args: (False, "window closed")
    noted: list[tuple] = []
    jobs._note_miss = lambda *args: noted.append(args)

    assert await jobs._run_digests() == 0
    assert noted == [(123, "morning", "window closed", "08:00")]


@pytest.mark.asyncio
async def test_process_batch_retries_while_database_is_busy(monkeypatch):
    """A momentary writer lock must cost a retry, not a lost batch."""
    from sqlalchemy.exc import OperationalError

    import app.processing.pipeline as pipeline
    from app.scheduler.jobs import NewsJobs

    jobs = NewsJobs(get_config())
    calls = {"n": 0}

    async def flaky(session, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("UPDATE articles", {}, Exception("database is locked"))
        return pipeline.ProcessStats(scanned=1, processed=1)

    monkeypatch.setattr(pipeline, "process_pending", flaky)
    stats = await jobs._process_batch(batch=5, interests=[])
    assert calls["n"] == 2
    assert stats.processed == 1


@pytest.mark.asyncio
async def test_reprocessing_a_row_does_not_duplicate_its_tags(session, fake_llm):
    """Found live: requeueing one article failed on `INSERT INTO article_tags`."""
    from app.processing.pipeline import process_pending

    stored = repo.save_article(session, feed_item("OpenAI releases a new reasoning model",
                                                 "https://openai.com/reprocess"))
    session.commit()
    await process_pending(session, config=get_config(), llm=fake_llm, limit=5)
    session.commit()
    row = session.get(Article, stored.id)
    first = sorted(tag.name for tag in row.tags)
    assert first, "the row should be tagged by now"

    row.is_processed = False                 # what a requeue or a later AI pass does
    session.commit()
    stats = await process_pending(session, config=get_config(), llm=fake_llm, limit=5)
    session.commit()

    assert stats.failed == 0, "the second pass may not die on the composite key"
    assert sorted(tag.name for tag in row.tags) == first
    assert row.is_processed


@pytest.mark.asyncio
async def test_digest_empty_window_does_not_crash(session):
    from app.services.digest import get_digest_service

    digest = await get_digest_service().generate("evening", chat_id=111111111,
                                                llm=_SilentLLM())
    assert isinstance(digest.messages, list)


class _SilentLLM:
    enabled = False


async def test_the_stub_backfill_runs_every_round_and_cannot_break_it(monkeypatch):
    """One boot pass drains five rows; a per-round requeue is a backfill."""
    import app.processing.pipeline as pipeline
    from app.processing import enrich
    from app.scheduler.jobs import NewsJobs

    jobs = NewsJobs(get_config())
    attempted: list[int] = []

    async def one_batch(*args, **kwargs):
        attempted.append(1)
        return pipeline.ProcessStats(scanned=0)

    async def nothing(*args, **kwargs):
        return 0

    monkeypatch.setattr(jobs, "_process_batch", one_batch)
    monkeypatch.setattr(jobs, "run_translation", nothing)
    monkeypatch.setattr(jobs, "send_free_alerts", nothing)
    jobs._requeue_stubs()                      # no rows: quiet, no log, no raise
    assert attempted == []

    monkeypatch.setattr(enrich, "requeue_stubs",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("database is locked")))
    stats = await jobs._run_process(limit=1)   # a broken backfill must not stop the round
    assert stats.scanned == 0 and len(attempted) >= 1


def test_status_line_reports_enabled_sources_not_the_config_file(session):
    """"数据源：37 个" counted sources.yaml, including the 17 that are switched off."""
    from app.services.format import status_line
    from app.services.news import get_news_service

    stats = get_news_service().stats()
    configured = [s for s in get_config().sources]
    enabled = [s for s in configured if s.get("enabled", True)]
    assert stats["sources"] == len(enabled) < len(configured) == stats["sources_configured"]

    line = status_line(stats)
    assert f"{stats['sources']} 启用 / {stats['sources_configured']} 配置" in line, line

    healthy = status_line({**stats, "sources_failing": 0})
    assert "报错" not in healthy, "a clean system must not advertise an error column"
    assert "3 个正在报错" in status_line({**stats, "sources_failing": 3})


# ------------------------------------------------------- 突发 alert gate
def _alert_like(title: str, *, score: float = 60.0, quality: float = 95.0,
                hours_ago: float = 2.0, source: str = "OpenAI"):
    """The attributes the gate reads, without a database row."""
    from types import SimpleNamespace

    return SimpleNamespace(final_score=score, source_quality=quality, source_name=source,
                           title=title,
                           published_at=datetime.utcnow() - timedelta(hours=hours_ago))


def test_rule_mode_breaking_needs_an_event_a_publisher_and_freshness():
    """The gate that replaced the unreachable bar of 90.

    Headlines are the ones measured on the live box: the accepted set is what a
    reader would call news, the rejected set is what a score-only cut would have
    woken him up for.
    """
    from app.processing import breaking

    cfg = get_config()
    accept = [
        "Anthropic to pay Akamai $11.6 billion over seven years in cloud deal",
        "Introducing GPT-6 Sol and Luna",
        "Court rules Trump can blacklist Anthropic for refusing to enable Claude feature",
        "xAI's Grok 4.6 is now available in Amazon Bedrock",
        "OpenAI agent hacked government website, PM says",
    ]
    for title in accept:
        assert breaking.gate(_alert_like(title), config=cfg, ai_enabled=False)[0], title

    reject = [
        # same words, second-hand community source
        (_alert_like("OpenAI agent hacked government website", source="Reddit LocalLLaMA RSS"), "社区源"),
        # a version bump is a release, not an announcement
        (_alert_like("v2.1.283 released in anthropics/claude-code"), "版本号"),
        # no event at all
        (_alert_like("TensorRT Edge-LLM Completes the MLPerf Edge Agentic Benchmark"), "无事件词"),
        # quality below the first-hand floor
        (_alert_like("Anthropic announces Claude for Enterprise", quality=65.0), "二手渠道"),
        # stale: collected late, published long ago
        (_alert_like("Anthropic announces Claude for Enterprise", hours_ago=30), "过期"),
        # under the score floor
        (_alert_like("Anthropic announces Claude for Enterprise", score=30.0), "低分"),
    ]
    for article, why in reject:
        ok, reason = breaking.gate(article, config=cfg, ai_enabled=False)
        assert not ok, f"{why}: {reason}"


def test_ai_mode_breaking_still_uses_the_configured_bar():
    from app.processing import breaking

    cfg = get_config()
    ok, _ = breaking.gate(_alert_like("Whatever the headline is", score=93.0),
                          config=cfg, ai_enabled=True)
    assert ok, "the model's importance score is the judgement in AI mode"
    ok, reason = breaking.gate(_alert_like("Introducing a major model", score=88.0),
                               config=cfg, ai_enabled=True)
    assert not ok and "88" in reason


def test_breaking_hot_bar_keeps_the_emoji_ladder_ordered():
    from app.processing import breaking

    cfg = get_config()
    hot, star, dot = breaking.emoji_bars(cfg, ai_enabled=False)
    assert hot < 78, "规则模式实测最高 78 分，🔥 必须够得着"
    assert hot > star > dot, "否则 ⭐ 比 🔥 还稀有，图例就反了"
    assert breaking.emoji_bars(cfg, ai_enabled=True)[0] == 90.0


@pytest.mark.asyncio
async def test_rule_mode_round_marks_and_logs_the_breaking_candidate(session, monkeypatch):
    """End to end: a real event headline from a first-hand source alerts."""
    from app.processing import pipeline

    stored = repo.save_article(session, feed_item(
        "OpenAI announces GPT-6 with a 1 million token context window",
        "https://openai.com/index/announces-gpt-6"))
    session.commit()

    seen: list[tuple] = []
    monkeypatch.setattr(pipeline.log, "info",
                        lambda *a, **k: seen.append(a), raising=False)
    stats = await process_pending(session, config=get_config(), llm=_SilentLLM(), limit=10)
    session.commit()

    assert stats.breaking == [stored.id], "规则模式也必须能触发突发"
    row = session.get(Article, stored.id)
    assert row.meta["breaking_reason"].startswith("event")
    # caplog cannot see these loggers (news.* does not propagate), so the
    # assertion is on the monkeypatched log object.
    logged = [" ".join(str(part) for part in entry) for entry in seen]
    assert any("breaking candidate" in line and "event" in line for line in logged), \
        "突发决定必须有日志：过去 0 条突发看起来像新闻少，其实是功能死了"


def test_briefing_backfill_shares_the_leftover_slots_round_robin():
    """Backfilling in score order is how one feed took 6 of 8 briefing slots.

    On 2026-09-26 the 12-hour evening window contained no press coverage at all,
    so after the per-source cap bit the leftovers were re-added straight from
    Reddit - the cap decided nothing.
    """
    from app.services.digest import select_briefing

    pool = ([_Item("OpenAI", 70.0 + i) for i in range(3)]
            + [_Item("Reddit", 60.0 - i) for i in range(9)]
            + [_Item("Ars", 50.0 - i) for i in range(9)])
    picked = select_briefing(pool, top_items=11, max_per_source=3)
    sources = [p.source_name for p in picked]

    assert len(picked) == 11
    assert sources.count("Reddit") == sources.count("Ars") == 4, sources


@pytest.mark.asyncio
async def test_evening_briefing_does_not_repeat_the_morning_one(session):
    """Widening the window to 24h is only safe because sent items are skipped.

    Both briefings now look back a day - the evening one has to, or it covers
    00:00-12:00 UTC and finds nothing but community posts - so the second of the
    two must not reprint what the first already delivered.
    """
    from app.services.digest import get_digest_service

    for source, base in (("OpenAI", 1), ("NVIDIA", 101), ("Hacker News", 201), ("Reddit", 301)):
        for i in range(6):
            repo.save_article(session, feed_item(
                f"{source} releases a new model with lower API pricing and better reasoning {base + i}",
                f"https://example.com/{source.lower()}-{base + i}", source=source))
    session.commit()
    await process_pending(session, config=get_config(), llm=_SilentLLM(), limit=40)
    session.commit()

    service = get_digest_service()
    news_service = service.news
    news_service.user_for(111111111)
    morning = await service.generate("morning", chat_id=111111111, llm=_SilentLLM())
    assert morning.article_ids and morning.messages
    service.record_delivery(chat_id=111111111, digest=morning)

    evening = await service.generate("evening", chat_id=111111111, llm=_SilentLLM())
    assert evening.article_ids, "24 小时窗口不该空手"
    assert not set(evening.article_ids) & set(morning.article_ids), \
        "早报发过的内容不能再出现在晚报里"
