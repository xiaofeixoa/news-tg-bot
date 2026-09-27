"""AI pipeline tests: rule gate, classification, Chinese summary, resilience."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.config import get_config
from app.database import repository as repo
from app.database.database import session_scope
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
    # 设定时间被写坏（"8am"、空串）时这一份永远不会发，而每 5 分钟的判定只说"还没到点"
    jobs._note_miss(123, "morning", "bad time '8am'", "8am")
    jobs._note_miss(123, "morning", "bad time '8am'", "8am")

    assert [level for level, _ in records.items] == ["info", "warning", "warning"]
    assert "already been delivered" in records.items[0][1]
    assert "missed its 20:00 window" in records.items[1][1]
    assert "can never be sent" in records.items[2][1] and "8am" in records.items[2][1], \
        "要说清是哪一份、哪个值坏了，否则这条日志没法行动"


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


async def test_a_corrupt_briefing_time_reaches_the_log_through_the_real_path(monkeypatch):
    """接线也要测：`_note_miss` 会说话不够，`_digest_due` 得真的把坏值递给它。

    这条用最真实的形状（不设假 `_digest_due`、sender 为空所以绝不联网）问一个问题：
    库里 `daily_time` 被写成 "8am" 之后，运维能不能从日志里看出来。
    """
    from app.scheduler import jobs as jobs_module
    from app.scheduler.jobs import NewsJobs

    class Records:
        def __init__(self) -> None:
            self.items: list[tuple[str, str]] = []

        def _emit(self, level):
            def go(msg, *args):
                self.items.append((level, msg % args if args else msg))
            return go

        def info(self, msg, *args):
            self._emit("info")(msg, *args)

        def warning(self, msg, *args):
            self._emit("warning")(msg, *args)

        def error(self, msg, *args):
            self._emit("error")(msg, *args)

    records = Records()
    monkeypatch.setattr(jobs_module, "log", records)
    jobs = NewsJobs(get_config())
    jobs._sender = None
    jobs.chat_ids = lambda: [123]
    jobs.news.user_for = lambda chat: {"paused": False, "daily_enabled": True,
                                       "daily_time": "8am", "evening_enabled": False,
                                       "evening_time": "20:00", "timezone": "Asia/Shanghai"}

    assert await jobs._run_digests() == 0
    warnings = [text for level, text in records.items if level == "warning"]
    assert any("can never be sent" in t and "8am" in t for t in warnings), records.items


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


@pytest.mark.asyncio
async def test_rule_mode_writes_a_why_it_matters_line_from_real_signals(session):
    """没 key 的机器上这句话从来没有过：线上实测近 14 天 423 行可见行里 0 行有值。"""
    saved = repo.save_article(session, feed_item(
        "OpenAI announces a cheaper reasoning model for agents",
        "https://openai.com/index/cheaper-reasoning"))
    session.commit()
    await process_pending(session, config=get_config(), llm=_SilentLLM(), limit=10)
    session.commit()
    row = session.get(Article, saved.id)
    assert row.why_it_matters and "OpenAI" in row.why_it_matters
    assert "事件性消息" in row.why_it_matters, "一手来源 + 标题里有事件，才写这一句"


@pytest.mark.asyncio
async def test_multi_source_coverage_is_named_in_the_why_line(session):
    first = repo.save_article(session, feed_item(
        "Anthropic signs a $10 billion compute deal", "https://anthropic.com/news/deal"))
    second = repo.save_article(session, feed_item(
        "Anthropic signs a $10 billion compute deal", "https://techcrunch.com/deal",
        source="TechCrunch AI"))
    session.commit()
    await process_pending(session, config=get_config(), llm=_SilentLLM(), limit=10)
    session.commit()
    row = session.get(Article, first.id)
    assert row.why_it_matters and "另有" in row.why_it_matters, \
        f"同一事件多家报道是最值钱的信号，得说出来源名：{row.why_it_matters}"
    assert "TechCrunch AI" in row.why_it_matters
    assert second is not None


@pytest.mark.asyncio
async def test_a_quiet_row_gets_no_why_line_instead_of_padding(session):
    """真的处理过、但没有硬信号：社区来源 + 单家报道 + 没有事件词 + heat 0。"""
    saved = repo.save_article(session, feed_item(
        "Why transformer evaluation needs better benchmarks",
        "https://reddit.com/r/LocalLLaMA/comments/evaluation",
        source="Reddit LocalLLaMA RSS",
        body="Transformers and LLM evaluation benchmarks are discussed here."))
    session.commit()
    await process_pending(session, config=get_config(), llm=_SilentLLM(), limit=10)
    session.commit()
    row = session.get(Article, saved.id)
    assert row.is_processed and not row.filtered_out, "这一行要真的走过处理，断言才有意义"
    assert not row.why_it_matters, "没有真信号就留空：诚实的空比凑出来的中文好"


def test_community_heat_alone_is_not_a_reason():
    """热度只能加强一句有锚点的话，不能自己充当"为什么值得关注"。"""
    from types import SimpleNamespace

    from app.processing import summarizer

    row = SimpleNamespace(title="Show HN: a whiteboard tool for design reviews",
                          source_name="Hacker News Free", source_quality=50, community_heat=407)
    assert summarizer.compose_why_it_matters(row, None, config=get_config()) == ""


def test_an_event_plus_heat_writes_the_reason_in_chinese():
    from types import SimpleNamespace

    from app.processing import summarizer

    row = SimpleNamespace(title="OpenAI announces an agent that broke into a government site",
                          source_name="Hacker News", source_quality=65, community_heat=481)
    line = summarizer.compose_why_it_matters(row, None, config=get_config())
    assert "社区热度 481" in line, line
    assert "自己发布" not in line, "质量 65 的二手转载不能自称一手来源"


@pytest.mark.asyncio
async def test_processed_at_records_when_the_row_was_decided(session):
    """`updated_at` 会被翻译/正文提取/is_sent 反复推后，它不是处理时钟。"""
    saved = repo.save_article(session, feed_item(
        "Google DeepMind introduces a weather model", "https://deepmind.google/weather"))
    session.commit()
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    await process_pending(session, config=get_config(), llm=_SilentLLM(), limit=10)
    session.commit()
    row = session.get(Article, saved.id)
    assert row.processed_at is not None and row.processed_at >= before
    assert (row.processed_at - row.created_at) < timedelta(hours=1)


@pytest.mark.asyncio
async def test_one_poison_row_does_not_abort_the_whole_round(session, monkeypatch):
    """线上真实炸法（scheduler.log 里 5 次）：一行 flush 失败之后，except 分支没有先
    rollback 就去碰 ORM 属性 → PendingRollbackError 冒出整个 job，后面每行都被跳过，
    注释里写的"三次之后放弃"根本没机会生效。"""
    from sqlalchemy import insert
    from app.database.models import Tag, article_tags

    poison_row = repo.save_article(session, feed_item("OpenAI ships a reasoning router",
                                                      "https://openai.com/router"))
    next_row = repo.save_article(session, feed_item("Meta releases an open weight model",
                                                     "https://meta.com/open"))
    tag = Tag(name="router")
    session.add(tag)
    session.commit()
    real = repo.attach_tags

    def explode(_session, article, names):
        if article.id == poison_row.id:
            # 和生产事故同一形状：往中间表插两次同一条链接，失败发生在 flush 里，
            # 于是会话被标记为需要回滚。
            _session.execute(insert(article_tags).values(article_id=article.id, tag_id=tag.id))
            _session.execute(insert(article_tags).values(article_id=article.id, tag_id=tag.id))
            _session.flush()
            return
        real(_session, article, names)

    monkeypatch.setattr(repo, "attach_tags", explode)
    stats = await process_pending(session, config=get_config(), llm=_SilentLLM(), limit=5)
    session.commit()
    assert stats.failed == 1 and stats.processed == 1, \
        "中毒的那一行要记一次失败，但它后面那行必须照常被处理"
    first = session.get(Article, poison_row.id)
    assert first.process_attempts == 1 and "IntegrityError" in (first.process_error or "")
    assert not first.is_processed, "还该再试两次，不是一举标完成"


@pytest.mark.asyncio
async def test_attach_tags_ignores_a_link_another_session_already_wrote(session):
    """并发采集下关系集合会过期：以库里的链接表为准，才不会插重第二条。"""
    from sqlalchemy import insert
    from app.database.models import Tag, article_tags

    stored = repo.save_article(session, feed_item("Anthropic opens an agent protocol",
                                                  "https://anthropic.com/protocol"))
    tag = Tag(name="agents")
    session.add(tag)
    session.commit()
    assert stored.tags == []                      # 集合在这里被加载：此刻还没有链接

    from app.database.database import get_session_factory

    other = get_session_factory()()               # 另一个会话（另一轮采集）插好了链接
    try:
        other.execute(insert(article_tags).values(article_id=stored.id, tag_id=tag.id))
        other.commit()
    finally:
        other.close()

    repo.attach_tags(session, stored, ["Agents"])  # 同一个标签，另一种大小写
    session.commit()
    links = session.execute(select(article_tags).where(article_tags.c.article_id == stored.id))
    assert len(links.all()) == 1


# ------------------------------------------------ 简报窗口：08:00 到底会不会发
class _FrozenDatetime:
    """`_digest_due` 读 `datetime.now(zone(tz))`；把钟交给我们。"""

    def __init__(self, moment):
        self._moment = moment

    def now(self, tz=None):
        return self._moment.astimezone(tz) if tz else self._moment

    def __getattr__(self, name):          # datetime.min / datetime(...) 照常可用
        return getattr(datetime, name)


def shanghai(hour: int, minute: int = 0, *, days_ago: int = 0):
    """The frozen clock: today (or N days back) at HH:MM in his own timezone."""
    from zoneinfo import ZoneInfo
    local = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
    local = local.replace(hour=hour, minute=minute, second=0, microsecond=0) - timedelta(days=days_ago)
    return local.astimezone(timezone.utc)


def naive_stamp(hour: int, minute: int = 0, *, days_ago: int = 0) -> datetime:
    """库里 PushLog.created_at 是 naive UTC；这就是"本地某天 HH:MM"的那个值。"""
    return shanghai(hour, minute, days_ago=days_ago).replace(tzinfo=None)


def digest_jobs(monkeypatch, hour: int, minute: int = 0):
    from app.scheduler import jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "datetime", _FrozenDatetime(shanghai(hour, minute)))
    return jobs_mod.NewsJobs(get_config())


@pytest.mark.parametrize("hour,minute,expected", [
    (7, 59, "not yet due"),
    (8, 0, "due"),
    (8, 20, "due"),
    (8, 45, "due"),                      # 宽限期是"不超过 45 分钟"
    (8, 46, "window closed"),
])
def test_the_morning_window_is_a_real_window(monkeypatch, hour, minute, expected):
    jobs = digest_jobs(monkeypatch, hour, minute)
    ok, note = jobs._digest_due(111, "morning", "08:00", "Asia/Shanghai")
    assert note == expected, f"{hour:02d}:{minute:02d} 判定成 {note!r}，期望 {expected!r}"
    assert ok is (expected == "due")


def test_a_bad_clock_string_is_reported_not_crashed(monkeypatch):
    jobs = digest_jobs(monkeypatch, 8, 0)
    ok, note = jobs._digest_due(111, "morning", "morning", "Asia/Shanghai")
    assert not ok and "bad time" in note


def test_a_delivered_briefing_is_never_reported_as_missed(monkeypatch):
    """2026-09-27 真实事故：早报 08:01:39 已送到，14:31 的检查却因为"窗口已过"
    写出 `missed its 08:00 window … it will not be sent today`。谎报的告警比没有
    告警更糟 —— 判定顺序必须先看账本。"""
    from app.database import repository as repo
    from app.database.models import PushLog

    jobs = digest_jobs(monkeypatch, 14, 31)
    with session_scope() as s:
        user = repo.get_or_create_user(s, 111, timezone="Asia/Shanghai")
        s.add(PushLog(user_id=user.id, kind="morning", created_at=naive_stamp(8, 1)))
        s.commit()

    ok, note = jobs._digest_due(111, "morning", "08:00", "Asia/Shanghai")
    assert not ok and note == "already sent today", note

    # 而"今天确实没发"的那种，还是要报窗口已过
    with session_scope() as s:
        s.query(PushLog).delete()
        s.commit()
    ok, note = jobs._digest_due(111, "morning", "08:00", "Asia/Shanghai")
    assert not ok and note == "window closed", note


def test_yesterdays_send_does_not_silence_today(monkeypatch, session):
    from app.database import repository as repo
    from app.database.models import PushLog

    jobs = digest_jobs(monkeypatch, 8, 5)
    with session_scope() as s:
        user = repo.get_or_create_user(s, 111)
        s.add(PushLog(user_id=user.id, kind="morning",
                      created_at=naive_stamp(8, 5, days_ago=1)))
        s.commit()
    ok, note = jobs._digest_due(111, "morning", "08:00", "Asia/Shanghai")
    assert ok and note == "due", f"昨天发过不该影响今天：{note}"


def test_another_readers_send_does_not_silence_this_one(monkeypatch):
    """账本曾经是全局的：第二个订阅者会因为别人收到过而永远收不到。"""
    from app.database import repository as repo
    from app.database.models import PushLog

    jobs = digest_jobs(monkeypatch, 8, 5)
    with session_scope() as s:
        other = repo.get_or_create_user(s, 222, timezone="Asia/Shanghai")
        s.add(PushLog(user_id=other.id, kind="morning", created_at=naive_stamp(8, 0)))
        s.commit()
    ok, note = jobs._digest_due(111, "morning", "08:00", "Asia/Shanghai")
    assert ok and note == "due", f"别人的推送记录不该挡住他：{note}"

    with session_scope() as s:
        mine = repo.get_or_create_user(s, 111, timezone="Asia/Shanghai")
        s.add(PushLog(user_id=mine.id, kind="morning", created_at=naive_stamp(8, 0)))
        s.commit()
    ok, note = jobs._digest_due(111, "morning", "08:00", "Asia/Shanghai")
    assert not ok and note == "already sent today"


def test_evening_is_not_blocked_by_the_morning_send(monkeypatch):
    from app.database import repository as repo
    from app.database.models import PushLog

    jobs = digest_jobs(monkeypatch, 20, 5)
    with session_scope() as s:
        user = repo.get_or_create_user(s, 111, timezone="Asia/Shanghai")
        s.add(PushLog(user_id=user.id, kind="morning", created_at=naive_stamp(8, 0)))
        s.commit()
    ok, note = jobs._digest_due(111, "evening", "20:00", "Asia/Shanghai")
    assert ok and note == "due", f"早报发过不该挡住晚报：{note}"



# ------------------------------ 维护任务：135 次启动里只跑过一次的那个作业
def _job(job_id: str):
    from app.scheduler.jobs import NewsJobs, create_scheduler

    config = get_config()
    return create_scheduler(NewsJobs(config), config).get_job(job_id)


def test_maintenance_runs_at_boot_not_six_hours_after_it():
    """线上实测：46 小时里调度器启动 135 次，维护任务只产出过 1 条日志。

    纯 IntervalTrigger 的作业，首次执行要等满 6 小时**不间断**运行；每次部署
    都会把这个计时器清零，所以"哪个源挂了""磁盘还剩多少"这两句他最该看到的
    话，实际上一年也出不来几次。
    """
    from app.scheduler.jobs import MAINT_STARTUP_DELAY

    job = _job("maintenance")
    assert job is not None, "维护作业根本没注册"
    lead = _lead(job)
    assert 0 < lead < MAINT_STARTUP_DELAY * 3, f"维护任务 {lead:.0f} 秒后才跑；启动即体检才有效"


def test_every_other_job_still_keeps_its_own_startup_delay():
    """别把"启动就跑"推广到写库的作业上：那正是 database is locked 的来源。"""
    from app.scheduler.jobs import DIGEST_STARTUP_DELAY, FIRST_PROCESS_DELAY

    rss = _job("collect:rss")
    process = _job("ai:process")
    watcher = _job("digest:watcher")
    maint = _job("maintenance")
    rss_lead = _lead(rss)
    assert 0 < rss_lead < 60, f"采集作业启动延迟 {rss_lead:.0f}s，本应几秒内就开始"
    lead_p = _lead(process)
    lead_d = _lead(watcher)
    assert lead_p >= FIRST_PROCESS_DELAY - 5, f"AI 处理只等 {lead_p:.0f}s，会和采集撞锁"
    assert 0 < lead_d <= DIGEST_STARTUP_DELAY + 60
    # 起跑顺序本身就是设计：采集先跑，体检其次，写库的 AI 处理最后。
    # 这条断言就是 CI 上把那个 -28780 秒抓出来的东西：系统时区（UTC）不等于
    # 配置时区（上海）时，所有 next_run_time 都排到 8 小时前的过去去了。
    assert rss_lead < _lead(maint) < lead_d < lead_p, \
        f"启动次序错了：rss={rss_lead:.0f} maint={_lead(maint):.0f} 观察者={lead_d:.0f} AI={lead_p:.0f}"


def _lead(job):
    """Seconds from now until this job next fires (they all carry a local tz)."""
    from datetime import datetime as real_datetime

    return (job.next_run_time - real_datetime.now(job.next_run_time.tzinfo)).total_seconds()


def test_maintenance_waits_for_the_writer_lock():
    """归档会 commit：它必须和其他写者排队，而不是插进一轮采集中间。"""
    import asyncio

    from app.scheduler.jobs import NewsJobs

    async def scenario():
        jobs = NewsJobs(get_config())
        await jobs._write_lock.acquire()
        try:
            task = asyncio.create_task(jobs.run_maintenance())
            await asyncio.sleep(0.05)
            assert not task.done(), "维护任务没等写锁，会和采集同时 commit"
        finally:
            jobs._write_lock.release()
        await asyncio.wait_for(task, timeout=30)

    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_a_quiet_maintenance_still_says_it_ran(monkeypatch):
    """所有告警条件都不成立时，日志里必须还剩一行"我跑过了"。"""
    from app.scheduler import jobs as jobs_mod
    from app.scheduler.jobs import NewsJobs
    from app.services.news import NewsService

    info: list[str] = []
    monkeypatch.setattr(jobs_mod.log, "info", lambda *a, **k: info.append(str(a[0]) % a[1:] if a else ""))
    monkeypatch.setattr(NewsService, "archive_old", lambda self: 0)
    monkeypatch.setattr(NewsService, "stats", lambda self: {"total_articles": 7, "disk_free_mb": 999_999})
    jobs = NewsJobs(get_config())
    monkeypatch.setattr(jobs.news, "user_for", lambda chat_id, **k: {})
    await jobs.run_maintenance()
    line = [i for i in info if i.startswith("health check:")]
    assert line, f"维护跑完一个字都没留：{info}"
    assert "7 article(s) in db" in line[0] and "999999MB free" in line[0]


# ------------------------------------ 空简报：日志不能拿"该发了"当理由
class _EmptyDigest:
    def __init__(self):
        self.calls = 0

    async def generate(self, kind, *, chat_id=None, llm=None):
        from app.services.digest import Digest

        self.calls += 1
        return Digest(kind=kind, messages=[], date_label="2026-09-27", empty=True)


def _warn_lines(monkeypatch):
    from app.scheduler import jobs as jobs_mod

    warned: list[str] = []
    monkeypatch.setattr(jobs_mod.log, "warning",
                        lambda *a, **k: warned.append(str(a[0]) % a[1:] if a else ""))
    return warned


@pytest.mark.asyncio
async def test_an_empty_briefing_is_not_logged_as_being_due(monkeypatch):
    """旧日志：`evening digest for X skipped: due` —— 把判定结果当失败原因打印。"""
    from app.scheduler.jobs import NewsJobs

    warned = _warn_lines(monkeypatch)
    jobs = NewsJobs(get_config())
    monkeypatch.setattr(jobs, "chat_ids", lambda: [111])
    user = {"paused": False, "daily_enabled": True, "evening_enabled": True,
            "daily_time": "08:00", "evening_time": "20:00", "timezone": "Asia/Shanghai"}
    monkeypatch.setattr(jobs.news, "user_for", lambda chat_id, **k: user)
    monkeypatch.setattr(jobs, "_digest_due", lambda *a, **k: (True, "due"))
    jobs.digest = _EmptyDigest()
    assert await jobs._run_digests() == 0
    assert any("found nothing to send" in w for w in warned), f"空简报必须说清原因：{warned}"
    assert not any("skipped: due" in w for w in warned), warned


@pytest.mark.asyncio
async def test_the_empty_briefing_warning_is_not_repeated_every_five_minutes(monkeypatch):
    from app.scheduler.jobs import NewsJobs

    warned = _warn_lines(monkeypatch)
    jobs = NewsJobs(get_config())
    monkeypatch.setattr(jobs, "chat_ids", lambda: [111])
    user = {"paused": False, "daily_enabled": False, "evening_enabled": True,
            "daily_time": "08:00", "evening_time": "20:00", "timezone": "Asia/Shanghai"}
    monkeypatch.setattr(jobs.news, "user_for", lambda chat_id, **k: user)
    monkeypatch.setattr(jobs, "_digest_due", lambda *a, **k: (True, "due"))
    jobs.digest = _EmptyDigest()
    for _ in range(3):                   # 观察者每 5 分钟一轮
        await jobs._run_digests()
    assert jobs.digest.calls == 3
    empties = [w for w in warned if "found nothing to send" in w]
    assert len(empties) == 1, f"同一天的同一份简报只该警告一次：{empties}"


# ------------------------ 晚报 8/8 全是同一个源的星数行：候选窗口被并列分占满
def _seed_pool(monopoly: int, others: dict[str, float]) -> None:
    """`monopoly` 条并列满分的 A 源，加上几条分数各异的别家稿。"""
    from datetime import timedelta

    from app.processing.normalize import build_article

    with session_scope() as s:
        for i in range(monopoly):
            title = f"gpt-oss/{i} released an open model with 1,422 stars"
            data = build_article(
                title=title, url=f"https://github.com/tied/{i}", source_name="GitHub Trending",
                source_type="github", content=f"{title}. " + ("An open AI model release. " * 6),
                published_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1),
                quality="B",
            )
            article = repo.save_article(s, data)
            assert article is not None
            article.is_processed = True
            article.final_score = 78.0
        for name, score in others.items():
            title = f"{name} ships a reasoning model update for AI agents"
            data = build_article(
                title=title, url=f"https://example.org/{name}", source_name=name,
                source_type="rss", content=f"{title}. " + ("The company describes the AI model change. " * 6),
                published_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2),
                quality="A",
            )
            article = repo.save_article(s, data)
            assert article is not None
            article.is_processed = True
            article.final_score = score
        s.commit()


@pytest.mark.asyncio
async def test_one_tied_source_cannot_own_the_whole_briefing(session):
    """`max_per_source: 3` 写在配置里，交付的那份晚报却是 8/8 同一个源。

    线上实测（2026-09-27 20:03）：池子里 GitHub Trending 有 41 条并列 78.0，
    全池第二高只有 76.0，而简报只捞 top_items*4 = 32 条候选——32 条全是那一个源，
    上限砍到 3 条之后没得可换，"薄了就放宽"的兜底把溢出的 5 条又请了回来。
    """
    from app.services.digest import DigestService
    from app.services.news import NewsService

    _seed_pool(45, {"The Verge AI": 65.8, "Hacker News": 63.9, "Reddit LocalLLaMA RSS": 70.6,
                    "TechCrunch AI": 52.8, "OpenAI": 76.0})
    cfg = get_config()
    news = NewsService(cfg)
    digest = DigestService(cfg, news, llm=None)
    result = await digest._briefing("evening", chat_id=None, llm=None)

    from app.database.database import session_scope as _scope
    from app.database.models import Article as _A
    with _scope() as s:
        sources = [s.get(_A, i).source_name for i in result.article_ids]
    top_items = int(cfg.get("digest.evening.top_items", 8))
    cap = int(cfg.get("digest.max_per_source", 3))
    assert len(sources) == top_items, f"晚报只凑到 {len(sources)} 条"
    assert sources.count("GitHub Trending") <= cap, \
        f"{sources.count('GitHub Trending')} 条来自并列满分的同一个源，上限是 {cap}：{sources}"
    assert len(set(sources)) >= 3, f"一份简报不该只有一个声音：{sources}"
