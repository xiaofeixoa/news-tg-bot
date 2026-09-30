"""Scheduled work: collection, AI processing, briefings, breaking alerts (§16, §22).

Intervals come from .env; the per-user briefing check runs every few minutes so
each user's own /settings times are honoured without re-registering jobs.

Every job is wrapped: one exception is logged and the scheduler keeps running
(design doc section 25).
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Coroutine, Iterable, Sequence

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.bot.sender import TelegramSender
from app.collectors import build_collectors, close_client
from app.config import AppConfig, as_int, get_config
from app.database import repository as repo
from app.database.database import session_scope
from app.database.models import Article, User
from app.logging_setup import get_logger
from app.processing.pipeline import collect, process_pending, translate_pending
from app.services.digest import DigestService, deferral_worthwhile
from app.services.llm import LLMService
from app.services.news import NewsService

log = get_logger("scheduler")

DIGEST_CHECK_MINUTES = 5
# Seconds between the process start and the first AI pipeline run: collection
# rounds take longer than they do in tests, and both sides write SQLite.
FIRST_PROCESS_DELAY = 150
# First digest check after boot: short enough to still land inside the window.
DIGEST_STARTUP_DELAY = 120
# How late a scheduled briefing may arrive and still count as on time.
DIGEST_GRACE_MINUTES = 45
# First health check after boot. Six hours of *uninterrupted* uptime is what the
# interval alone asks for, and this box restarted 135 times in 46 hours.
MAINT_STARTUP_DELAY = 20
LOCK_RETRIES = 3

_SHUTTING_DOWN = False


def begin_shutdown() -> None:
    """Tell job wrappers that cancellations from now on are expected.

    APScheduler's executor logs a full traceback for every job it cancels on
    shutdown, which would otherwise put two stack dumps in journald per restart
    and look exactly like a real failure.
    """
    global _SHUTTING_DOWN
    _SHUTTING_DOWN = True
    logging.getLogger("apscheduler.executors.default").setLevel(logging.CRITICAL)
    logging.getLogger("apscheduler").setLevel(logging.CRITICAL)


def zone(name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name or "UTC")
    except Exception:  # pragma: no cover
        return timezone.utc


class NewsJobs:
    """The scheduler never imports Telegram handlers - only the sender."""

    def __init__(self, config: AppConfig | None = None, *, sender: TelegramSender | None = None,
                 news: NewsService | None = None, digest: DigestService | None = None,
                 llm: LLMService | None = None) -> None:
        self.config = config or get_config()
        self._sender = sender
        self.news = news or NewsService(self.config)
        self.digest = digest or DigestService(self.config, self.news)
        self.llm = llm or LLMService(self.config)
        self._digest_notes: set[tuple] = set()   # digest misses already logged
        # Single writer at a time. A collection round and the AI pass both commit
        # hundreds of rows; on SQLite concurrent writers raise
        # "database is locked" and the whole batch is lost (seen in production).
        self._write_lock = asyncio.Lock()

    @property
    def sender(self) -> TelegramSender | None:
        return self._sender

    def attach_sender(self, sender: TelegramSender | None) -> None:
        """The bot layer hands the scheduler its delivery channel."""
        self._sender = sender

    def chat_ids(self) -> list[int]:
        """Registered users first, the .env allowlist always included."""
        ids: list[int] = list(self.config.settings.chat_id_whitelist)
        with session_scope() as session:
            for user in session.scalars(select(User)):
                if user.telegram_chat_id not in ids:
                    ids.append(user.telegram_chat_id)
        return [i for i in ids if self.config.settings.is_allowed_chat(i)]

    # ------------------------------------------------------------------ jobs
    async def run_collect(self, types: Sequence[str] | None = None) -> Any:
        async with self._write_lock:
            return await self._run_collect(types)

    async def _run_collect(self, types: Sequence[str] | None = None) -> Any:
        collectors = build_collectors(self.config, types=types)
        if not collectors:
            log.warning("collect job: no enabled collectors for types=%s", types)
            return None
        stats = None
        with session_scope() as session:
            stats = await collect(session, collectors, config=self.config, llm=self.llm)
            session.commit()
        log.info("collect%s done: %s", f"[{','.join(types)}]" if types else "", stats)
        if stats and stats.errors:
            log.warning("collector errors: %s", "; ".join(stats.errors[:5]))
        return stats

    async def run_process(self, *, limit: int | None = None) -> Any:
        async with self._write_lock:
            return await self._run_process(limit=limit)

    def _requeue_stubs(self) -> None:
        """Give recent stub rows another pass, a few per round.

        Full text is only fetched while an article is being processed, so the
        300+ stub rows already stored (DeepMind, Hugging Face, OpenAI) would
        keep their unreachable scores until they aged out of the digest window.
        Per round rather than per boot: one boot pass drains five, and five is
        not a backfill.
        """
        from app.processing import enrich

        try:
            with session_scope() as session:
                queued = enrich.requeue_stubs(
                    session, config=self.config,
                    within_hours=as_int(self.config.get("enrich.backfill_hours", 36), 36),
                    limit=as_int(self.config.get("enrich.max_per_round", 5), 5))
                session.commit()
            if queued:
                log.info("requeued %d stub article(s) for full text", queued)
        except Exception as exc:  # noqa: BLE001 - a backfill must never break a round
            log.warning("stub requeue skipped: %s", exc)

    async def _run_process(self, *, limit: int | None = None) -> Any:
        """Drain the processing queue in batches so a cold-start backlog does
        not take days to surface. Each batch is committed separately."""
        from app.processing.pipeline import ProcessStats

        self._requeue_stubs()
        batch = limit or int(self.config.get("llm.process_batch_size", 25))
        rounds = max(1, int(self.config.get("llm.max_batches_per_run", 6)))
        interests = self.news.interest_payload()
        total = ProcessStats()
        for _ in range(rounds):
            stats = await self._process_batch(batch, interests)
            total.scanned += stats.scanned
            total.processed += stats.processed
            total.filtered += stats.filtered
            total.failed += stats.failed
            total.breaking.extend(stats.breaking)
            if stats.scanned < batch:
                break
        await self.run_translation()
        log.info("AI processing done: %s", total)
        # Called even with nothing new: rows an earlier round turned away for cooldown
        # only ever get retried here, and this is the only cadence they have.
        await self.send_breaking(total.breaking)
        await self.send_free_alerts()
        return total

    async def send_free_alerts(self) -> int:
        """Push anything that just became free (the /免费 watchtower)."""
        from app.services.free_alerts import FreeAlertService

        service = FreeAlertService(self.config, sender=self._sender, news=self.news)
        if not service.enabled:
            return 0
        try:
            return await service.run(self.chat_ids())
        except Exception as exc:  # a promo feed must never stall the news round
            log.warning("free-offer alert failed: %s", exc)
            return 0

    async def run_translation(self) -> int:
        """Backfill Chinese titles/summaries for everything already processed."""
        from app.processing.pipeline import translate_pending
        from app.services.translate import get_translator

        done = 0
        rounds = max(1, int(self.config.get("translate.rounds_per_run", 2)))
        for _ in range(rounds):
            # `per_run_limit` is an allowance per round, and the translator is a
            # process-lifetime singleton - so without this the counter only ever
            # grew and the round in its name meant nothing: after 60 items the
            # MyMemory route stayed off for the rest of the day, and every later
            # restart looked like a fix.
            get_translator(self.config).budget.reset_run()
            with session_scope() as session:
                got = await translate_pending(session, config=self.config)
                session.commit()
            done += got
            if not got:
                break
        if done:
            log.info("Chinese translation done for %d article(s)", done)
        return done

    async def _process_batch(self, batch: int, interests: list[dict[str, Any]]) -> Any:
        """One batch, retried while the database is momentarily busy."""
        from app.processing.pipeline import process_pending

        for attempt in range(1, LOCK_RETRIES + 1):
            try:
                with session_scope() as session:
                    stats = await process_pending(session, config=self.config, llm=self.llm,
                                                  limit=batch, interests=interests)
                    session.commit()
                return stats
            except OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == LOCK_RETRIES:
                    raise
                wait = 2 * attempt
                log.warning("database busy, retrying batch in %ss (attempt %d/%d)",
                            wait, attempt, LOCK_RETRIES)
                await asyncio.sleep(wait)
        raise RuntimeError("unreachable")  # pragma: no cover

    def _retry_breakings(self) -> list[int]:
        """突发 ids that a cooldown or the daily cap turned away in an earlier round.

        Without this the rejection is final: `send_breaking` is handed only the rows
        the current round processed, and a row rejected for timing is already
        `is_processed=True`, so nobody looks at it again. Measured on the live box:
        #1083 alerted at 21:41, #1101 cleared the gate at 22:12, was answered
        `cooldown 29 min left`, and stayed undelivered while its freshness window
        still ran to 22:00 the next day.
        """
        try:
            with session_scope() as session:
                return [int(row.id) for row in repo.breaking_deferrals(session)]
        except Exception as exc:  # noqa: BLE001 - a retry pass must not kill the round
            log.warning("breaking retry queue skipped: %s", exc)
            return []

    def _settle_breaking_deferrals(self, *, reasons: dict[int, str], deferred: set[int],
                                   settled: set[int], delivered: set[int]) -> None:
        """Re-book what is still waiting, drop what never will, and say so.

        The marker sits on a shared row while the cooldown is per reader, so the
        rule is: re-book a row whenever *any* reader is still waiting for it - even
        when another reader already received it this same round. Writing that as
        `deferred - delivered` loses the second reader's alert exactly the way the
        old global `is_breaking` flag used to (v1.50), which is why the two passes
        below are keyed off disjoint sets instead of a guard inside one loop.
        """
        keep = set(deferred)
        clear = (settled | delivered) - keep
        if not keep and not clear:
            return
        from app.processing import breaking

        try:
            with session_scope() as session:
                for article_id in sorted(keep):
                    row = session.get(Article, article_id)
                    if row is None:
                        continue
                    tries = breaking.note_deferral(row, reasons.get(article_id, ""))
                    reason = reasons.get(article_id, "")
                    if breaking.HEAT_WATCH in reason:
                        # A story waiting to get hot is expected to wait for hours, so
                        # it is reported once and then about hourly. One line per
                        # 10-minute round per row would be the 204-line log again.
                        if tries == 1 or tries % 6 == 0:
                            log.info("breaking 热度观察 #%s（第 %s 轮）：%s",
                                     article_id, tries, reason)
                    elif tries >= 3:
                        log.warning("breaking #%s 已连续 %s 轮被推迟，仍未送达：%s",
                                    article_id, tries, reasons.get(article_id, ""))
                    else:
                        log.info("breaking #%s 排入稍后重试（第 %s 次）：%s",
                                 article_id, tries, reasons.get(article_id, ""))
                for article_id in sorted(clear):
                    row = session.get(Article, article_id)
                    if row is None:
                        continue
                    info = breaking.clear_deferral(row)
                    if not info:
                        continue
                    waited = f"（曾推迟 {info.get('tries')} 次，上次原因：{info.get('reason')}）"
                    if article_id in delivered:
                        log.info("breaking #%s 补发成功%s", article_id, waited)
                    else:
                        log.info("breaking #%s 不再重试%s", article_id, waited)
                session.commit()
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not kill the round
            log.warning("breaking retry bookkeeping skipped: %s", exc)

    async def send_breaking(self, article_ids: Iterable[int]) -> int:
        sent_total = 0
        # One round may find several independent events. The cooldown is measured
        # against the last *delivered* push, so the first send of the round would
        # otherwise put every later story of that same round on "cooldown 60 min
        # left" - which is how four qualifying rows in a week became one alert.
        sent_this_round: set[int] = set()
        fresh = [int(i) for i in article_ids]
        ids = fresh + [i for i in self._retry_breakings() if i not in fresh]
        reasons: dict[int, str] = {}
        deferred: set[int] = set()
        settled: set[int] = set()
        delivered: set[int] = set()
        for article_id in ids:
            for chat_id in self.chat_ids():
                ok, reason = self.digest.can_send_breaking(
                    chat_id, article_id=article_id,
                    respect_cooldown=chat_id not in sent_this_round)
                if not ok:
                    log.info("breaking #%s skipped for %s: %s", article_id, chat_id, reason)
                    if deferral_worthwhile(reason):
                        deferred.add(article_id)
                        reasons[article_id] = reason
                    else:
                        settled.add(article_id)
                    continue
                payload = await self.digest.generate_breaking(article_id, chat_id=chat_id)
                if not payload.messages:
                    settled.add(article_id)
                    continue
                if self._sender is None:
                    log.warning("no Telegram sender configured; breaking news #%s not delivered", article_id)
                    continue
                sent = await self._sender.send_digest(chat_id, payload)
                if sent:
                    self.digest.record_delivery(chat_id=chat_id, digest=payload)
                    sent_this_round.add(chat_id)
                    delivered.add(article_id)
                    sent_total += sent
        self._settle_breaking_deferrals(reasons=reasons, deferred=deferred,
                                        settled=settled, delivered=delivered)
        return sent_total

    async def run_digests(self) -> int:
        async with self._write_lock:
            return await self._run_digests()

    async def _run_digests(self) -> int:
        """Per-user morning/evening briefings, driven by each user's own clock."""
        delivered = 0
        for chat_id in self.chat_ids():
            user = self.news.user_for(chat_id)
            if user.get("paused"):
                continue
            for kind, enabled_key, time_key in (
                ("morning", "daily_enabled", "daily_time"),
                ("evening", "evening_enabled", "evening_time"),
            ):
                if not user.get(enabled_key):
                    continue
                due, note = self._digest_due(chat_id, kind, user[time_key], user["timezone"])
                if not due:
                    self._note_miss(chat_id, kind, note, str(user[time_key]))
                    continue
                payload = await self.digest.generate(kind, chat_id=chat_id, llm=self.llm)
                if payload.empty or not payload.messages:
                    # This used to log `skipped: due` - the reason string from the
                    # check that had just said *yes*. A briefing that composed to
                    # nothing is a real miss and has to be reported as one.
                    self._note_miss(chat_id, kind, "nothing to send", str(user[time_key]))
                    continue
                if self._sender is None:
                    log.warning("no sender: %s digest for %s not delivered", kind, chat_id)
                    continue
                sent = await self._sender.send_digest(chat_id, payload)
                if sent:
                    self.digest.record_delivery(chat_id=chat_id, digest=payload)
                    delivered += 1
                    log.info("%s digest delivered to %s in %d message(s)", kind, chat_id, sent)
        return delivered

    def _digest_due(self, chat_id: int, kind: str, hhmm: str, tz_name: str) -> tuple[bool, str]:
        try:
            hour, minute = (int(x) for x in str(hhmm).split(":")[:2])
        except ValueError:
            return False, f"bad time {hhmm!r}"
        local_now = datetime.now(zone(tz_name))
        scheduled = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if local_now < scheduled:
            return False, "not yet due"
        with session_scope() as session:
            user = repo.ledger_user(session, chat_id, timezone=tz_name)
            midnight_local = scheduled.replace(hour=0, minute=0, second=0, microsecond=0)
            start_utc = midnight_local.astimezone(timezone.utc).replace(tzinfo=None)
            count = repo.pushes_since(session, user=user, kind=kind, since=start_utc)
        # 账本要在"窗口已过"之前问。顺序反了的时候，一份**当天已经送到**的简报会因为
        # 下一次 5 分钟检查落在宽限期之外而被写成 `missed its 08:00 window`（WARNING，
        # 还说"今天不会再发"）—— 2026-09-27 就是这么在 14:31 误报了一次早上 08:01:39
        # 成功送达的那份早报。谎报的告警比没有告警更糟：它会让人去查一个不存在的故障。
        if count:
            return False, "already sent today"
        if (local_now - scheduled) > timedelta(minutes=DIGEST_GRACE_MINUTES):
            return False, "window closed"
        return True, "due"

    def _note_miss(self, chat_id: int, kind: str, note: str, hhmm: str) -> None:
        """A briefing that will not arrive has to say so.

        Until now both outcomes below were silent, so "the 08:00 早报 never came"
        read exactly like "the scheduler never ran". It was the latter: deploys
        cycled the service every 2-3 minutes inside the window, and each fresh
        process waited a full interval before its first check.

        `bad time` joined later, and it is the worst of the three: a malformed
        `daily_time` makes that briefing impossible forever, not just for one
        morning, while the scheduler quietly returns "not due" every 5 minutes
        with nothing to show for it. The two notes that are working as designed
        (paused, the briefing switch) stay silent on purpose - he set those.
        """
        loud = (note in ("already sent today", "window closed", "nothing to send")
                or note.startswith("bad time"))
        if not loud:
            return
        key = (chat_id, kind, str(datetime.utcnow().date()), hhmm)
        if key in self._digest_notes:
            return
        if len(self._digest_notes) > 200:          # never let this grow unbounded
            self._digest_notes.clear()
        self._digest_notes.add(key)
        if note.startswith("bad time"):
            log.warning("%s digest for %s can never be sent: %r is not a valid HH:MM - "
                        "reset it in /设置 or the users row", kind, chat_id, hhmm)
        elif note == "window closed":
            log.warning("%s digest for %s missed its %s window - no check ran within "
                        "%d minutes of it, so it will not be sent today",
                        kind, chat_id, hhmm, DIGEST_GRACE_MINUTES)
        elif note == "nothing to send":
            # Deduped like the others: the watcher retries every 5 minutes, and an
            # empty pool stays empty for the rest of the grace window.
            log.warning("%s digest for %s found nothing to send at %s: no stored "
                        "article cleared the score floor in the briefing window",
                        kind, chat_id, hhmm)
        else:
            log.info("%s digest for %s skipped: today's %s briefing has already "
                     "been delivered", kind, chat_id, hhmm)

    async def run_maintenance(self) -> None:
        # Archiving commits, so this job is a writer like the other two. Without
        # the lock it could land in the middle of a collection round - which is
        # the "database is locked" the lock exists for - and the boot-time report
        # this job now produces would be the thing that fails.
        async with self._write_lock:
            await self._run_maintenance()

    async def _run_maintenance(self) -> None:
        archived = self.news.archive_old()
        stats = self.news.stats()
        with session_scope() as session:
            # `enabled` mirrors the config now (see repo.sync_sources), so a feed
            # he switched off can no longer be reported as broken every six hours.
            failing = [s for s in repo.all_sources(session)
                       if s.enabled and (s.error_count or 0) >= 5]
            pending = repo.unprocessed_articles(session, limit=200)
        if archived:
            log.info("archived %d old article(s)", archived)
        if pending:
            # Waiting is what silently kills 突发: the gate expires 24h after
            # publishing, and `updated_at` cannot show this because later writes
            # (translation, enrichment, is_sent) keep pushing it forward.
            oldest = min(row.created_at for row in pending if row.created_at)
            hours = (datetime.utcnow() - oldest).total_seconds() / 3600.0
            line = "processing backlog: %d row(s), oldest waiting %.1fh" % (len(pending), hours)
            (log.warning if hours >= 6 else log.info)(line)
        for source in failing:
            log.warning("source %s has failed %s times in a row (last error: %s)",
                        source.name, source.error_count, (source.last_error or "")[:120])
        free_mb = stats.get("disk_free_mb")
        floor = as_int(self.config.get("alerts.min_free_mb", 1024), 1024)
        if free_mb is not None and free_mb <= floor:
            log.warning("only %sMB free on the database volume (alert below %sMB): "
                        "collection will fail silently once it fills", free_mb, floor)
        # One line per run, even when everything is fine. Every conditional above
        # can be silent, and when the whole job is silent that is indistinguishable
        # from it never having run: 135 boots in 46 hours left exactly one line to
        # grep for, and telling those two cases apart is the whole job of a log.
        log.info("health check: %s article(s) in db, %s unprocessed, %s failing "
                 "source(s), %sMB free (告警线 %sMB)",
                 stats.get("total_articles", "?"), len(pending), len(failing),
                 free_mb if free_mb is not None else "?", floor)

    async def startup_report(self) -> None:
        stats = self.news.stats()
        log.info(
            "AI News Radar online: %s/%s sources enabled, %s delivered in 24h, "
            "%s article(s) in db, llm=%s, chats=%s",
            stats["sources"], stats.get("sources_configured", stats["sources"]),
            stats.get("sources_delivering", "?"), stats["total_articles"],
            "on" if stats["llm_enabled"] else "off(rules only)",
            ",".join(str(c) for c in self.config.settings.chat_id_whitelist) or "-",
        )
        self._warn_github_budget()

    def _warn_github_budget(self) -> None:
        """Anonymous GitHub allows 60 requests/hour for the whole box.

        One releases source costs roughly one request per watched repository per
        round, so without a token the GitHub half of the corpus is quietly
        starved. Say it once at boot instead of letting `0 new of 0` confuse
        everybody later.
        """
        if self.config.settings.github_token:
            return
        repos = sum(len(s.get("repositories") or [])
                    for s in self.config.sources
                    if s.get("mode") == "releases" and s.get("enabled", True))
        if not repos:
            return
        per_hour = repos * max(1, int(60 / max(1, int(self.config.get(
            "schedule.github_minutes", 30) or 30))))
        log.warning(
            "未配置 GITHUB_TOKEN：%d 个 releases 仓库约需 %d 次/小时请求，"
            "而匿名上限是 60 次/小时 - GitHub 源会经常空转。"
            "在 /etc/ai-news-radar/env 里加 GITHUB_TOKEN=<PAT> 可放宽到 5000 次/小时",
            repos, per_hour)


def guarded(func: Callable[..., Coroutine[Any, Any, Any]], name: str):
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except asyncio.CancelledError:
            # During a restart the in-flight round is cancelled on purpose.
            # Swallowing it here keeps APScheduler from logging a full traceback
            # per job every time systemd restarts the service; at any other time
            # cancellation must still unwind normally.
            if _SHUTTING_DOWN:
                log.info("job %s cancelled during shutdown", name)
                return None
            raise
        except Exception as exc:  # never let a job kill the scheduler
            log.error("job %s failed: %s\n%s", name, exc, traceback.format_exc(limit=6))
            return None

    wrapper.__name__ = f"guarded_{name}"
    return wrapper


def create_scheduler(jobs: NewsJobs, config: AppConfig | None = None) -> AsyncIOScheduler:
    config = config or get_config()
    settings = config.settings
    tz = zone(settings.timezone or "UTC")
    scheduler = AsyncIOScheduler(timezone=tz, job_defaults={
        "coalesce": True,          # a missed run during a reboot must not fire 5x
        "max_instances": 1,        # never let two collection rounds overlap
        "misfire_grace_time": 300,
    })
    # `next_run_time` is read in the scheduler's timezone, so the clock it is
    # added to has to be that timezone's too. `datetime.now()` is the *system*
    # clock, and these two were only equal by luck: the unit file sets
    # `Environment=TZ=Asia/Shanghai` while the host itself is `Etc/UTC`, so the
    # service got sane leads and every other process (CI, a test run on the box,
    # a future box without that line) scheduled all seven jobs 8 hours in the
    # past - measured as a lead of -28780 s. Nothing catastrophic came of it
    # because an overdue interval job just fires on the next wakeup, which is
    # exactly why the bug survived: the one thing a startup delay is for (do not
    # let the AI pass start alongside the collectors) was only ever honoured by
    # accident, in one process, because of an env line nobody was testing.
    now = datetime.now(tz)

    interval_jobs: list[tuple[str, Sequence[str] | None, int]] = [
        ("rss", ("rss",), settings.rss_fetch_interval),
        ("hackernews", ("hackernews",), settings.hn_fetch_interval),
        ("github", ("github",), settings.github_fetch_interval),
        ("reddit", ("reddit",), settings.reddit_fetch_interval),
        ("arxiv", ("arxiv",), settings.arxiv_fetch_interval),
        ("youtube", ("youtube",), settings.youtube_fetch_interval),
    ]
    for label, types, seconds in interval_jobs:
        if not config.sources_of_type(types[0]):
            continue
        scheduler.add_job(
            guarded(jobs.run_collect, f"collect:{label}"),
            IntervalTrigger(seconds=max(60, int(seconds or 600))),
            kwargs={"types": types},
            id=f"collect:{label}",
            name=f"collect {label}",
            next_run_time=now + timedelta(seconds=5 + 3 * interval_jobs.index((label, types, seconds))),
        )

    scheduler.add_job(
        guarded(jobs.run_process, "process"),
        IntervalTrigger(seconds=max(60, int(settings.process_interval or 600))),
        id="ai:process", name="AI pipeline",
        # After the collectors, never alongside them: both sides write.
        next_run_time=now + timedelta(seconds=FIRST_PROCESS_DELAY),
    )
    scheduler.add_job(
        guarded(jobs.run_digests, "digests"),
        IntervalTrigger(minutes=DIGEST_CHECK_MINUTES),
        id="digest:watcher", name="digest watcher",
        # Check soon after boot instead of waiting a full interval: a restart that
        # straddles the 45-minute window drops that day's briefing outright, and
        # during today's deploy storm the service was cycled several times inside
        # the 08:00 window. Re-sending is already impossible - `_digest_due`
        # dedupes per local day - so running early costs nothing.
        next_run_time=now + timedelta(seconds=DIGEST_STARTUP_DELAY),
    )
    scheduler.add_job(
        guarded(jobs.run_maintenance, "maintenance"),
        IntervalTrigger(hours=6),
        id="maintenance", name="maintenance",
        # An interval-only job is a job that never runs here: the first pass is
        # six hours into an uninterrupted process, and 135 restarts in 46 hours
        # produced exactly one maintenance report (2026-09-26 19:39). Whatever it
        # says is therefore said about once a day at best - including the disk
        # floor and the processing backlog, the two conditions that are only ever
        # worth acting on early. The same trap caught the briefings before
        # DIGEST_STARTUP_DELAY existed.
        next_run_time=now + timedelta(seconds=MAINT_STARTUP_DELAY),
    )
    log.info("scheduler configured with %d job(s)", len(scheduler.get_jobs()))
    return scheduler


async def shutdown(jobs: NewsJobs, scheduler: AsyncIOScheduler, sender: TelegramSender | None) -> None:
    begin_shutdown()
    if scheduler.running:
        scheduler.shutdown(wait=False)
    await close_client()
    from app.collectors import close_browser_session

    await close_browser_session()
    from app.services.llm import close_llm

    await close_llm()
    if sender is not None:
        await sender.close()
    log.info("scheduler and clients stopped")
