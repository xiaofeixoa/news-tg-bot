"""Digest composition (design doc sections 16, 27).

Turns stored articles into Telegram-ready messages: morning / evening
briefings, breaking-news alerts, and the cooldown + daily-cap guards that keep
the bot from becoming a message cannon.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import func, select

from app.config import AppConfig, QUIET_TOKEN, as_int, get_config, quiet_window
from app.database import repository as repo
from app.database.database import session_scope
from app.database.models import Article, PushLog
from app.logging_setup import get_logger
from app.processing import breaking
from app.services import format as F
from app.services.llm import LLMService, get_llm
from app.services.news import ArticleView, NewsService, get_news_service

log = get_logger("app")

# The briefing reads 4x its length so the per-source cap has material left over.
BRIEFING_DEPTH = 4

# The guards that close on their own. They are constants because
# `deferral_worthwhile` below has to match them exactly, and a reason string that
# drifts away from its prefix silently turns "retry later" into "drop forever".
_COOLDOWN = "cooldown"
_DAILY_CAP = "daily cap"
# The third one is the gate's own heat wording; read from the gate module, never
# retyped, for the same drift reason - `tests/test_pipeline.py` asserts the two agree.
_HEAT = breaking.HEAT_WATCH
# The fourth is the quiet window, whose rule and token both live in config so the
# 突发 path, the 限免 path and `/设置` cannot disagree about what is configured.
_QUIET = QUIET_TOKEN
QUIET_CONFIG = "breaking.quiet_hours"


def deferral_worthwhile(reason: str) -> bool:
    """Is this rejection about *timing*, so the same row may pass a later round?

    Cooldown, the daily cap and the quiet window open again on a clock. So does the
    突发 heat bar: a row's `community_heat` is refreshed upward every time a collector
    meets the same URL again, while the gate is asked about that row once, minutes
    after publication. Measured on the live box over 2026-09-25..29, three stories
    crossed 250 only after processing (#581 26 -> 741 upvotes, #509 90 -> 495,
    #1182 47 -> 593) and nothing re-asked, so the bar could never rescue the very
    stories it was added for.
    Everything else - the gate no longer passing for a non-heat reason, the story ageing
    out, already sent, breaking switched off - will read the same way next round, so
    keeping it in the retry queue would be a query with no possible outcome.

    The heat test is a substring, not a prefix: that rejection arrives here through
    `can_send_breaking`, which labels every gate answer "not breaking: …" first.
    """
    return (reason.startswith(_COOLDOWN) or reason.startswith(_DAILY_CAP)
            or _HEAT in reason or _QUIET in reason)


def select_briefing(items: Sequence[ArticleView], *, top_items: int,
                    max_per_source: int) -> list[ArticleView]:
    """Best-first, and never owned by one feed.

    The briefing used to be `latest()[:10]` - the ten most *recent* rows. On a
    measured evening that handed 8 of 10 slots to Reddit thread titles
    ("杰夫的炒作让我很生气") while 17 higher-scoring stories in the same window
    were dropped, because selection followed whichever poller finished last.
    Ordering by score fixes the ranking; without the cap a single 25-item
    GitHub burst would simply own the briefing in score order instead of in
    arrival order. A thin day backfills past the cap - ten real items beat
    three items plus a rule.
    """
    ranked = sorted(items, key=lambda item: item.final_score or 0, reverse=True)
    chosen: list[ArticleView] = []
    overflow: list[ArticleView] = []
    per_source: dict[str, int] = {}
    for item in ranked:
        source = item.source_name or "?"
        if max_per_source > 0 and per_source.get(source, 0) >= max_per_source:
            overflow.append(item)
            continue
        per_source[source] = per_source.get(source, 0) + 1
        chosen.append(item)
        if len(chosen) >= top_items:
            return chosen
    if len(chosen) < top_items and overflow:
        # Thin day: fill the slots anyway, but cycle through the capped sources
        # instead of dumping one feed's leftovers in score order. On 2026-09-26
        # the evening 晚报 took 6 of 8 slots from a single subreddit that way,
        # because the 12h window held no press coverage at all (see the
        # digest.*.window_hours note in config/settings.yaml).
        by_source: dict[str, list[ArticleView]] = {}
        for item in overflow:
            by_source.setdefault(item.source_name or "?", []).append(item)
        while len(chosen) < top_items and by_source:
            for source in list(by_source):
                if len(chosen) >= top_items:
                    break
                chosen.append(by_source[source].pop(0))
                if not by_source[source]:
                    del by_source[source]
    return chosen
@dataclass
class Digest:
    kind: str
    messages: list[str] = field(default_factory=list)
    article_ids: list[int] = field(default_factory=list)
    date_label: str = ""
    empty: bool = False


class DigestService:
    def __init__(self, config: AppConfig | None = None, news: NewsService | None = None,
                 llm: LLMService | None = None) -> None:
        self.config = config or get_config()
        self.news = news or get_news_service()
        self.llm = llm

    def _llm(self, override: LLMService | None = None) -> LLMService:
        return override or self.llm or get_llm()

    # ------------------------------------------------------------- briefings
    async def generate(self, kind: str = "morning", *, chat_id: int | None = None,
                       llm: LLMService | None = None) -> Digest:
        kind = (kind or "morning").lower()
        if kind in {"daily", "morning", "早报"}:
            return await self.generate_daily(chat_id=chat_id, llm=llm)
        if kind in {"evening", "晚报"}:
            return await self.generate_evening(chat_id=chat_id, llm=llm)
        if kind in {"breaking"}:
            raise ValueError("generate_breaking() needs an article id")
        raise ValueError(f"unknown digest kind: {kind}")

    async def generate_daily(self, *, chat_id: int | None = None,
                             llm: LLMService | None = None) -> Digest:
        return await self._briefing("morning", chat_id=chat_id, llm=llm)

    async def generate_evening(self, *, chat_id: int | None = None,
                              llm: LLMService | None = None) -> Digest:
        return await self._briefing("evening", chat_id=chat_id, llm=llm)

    async def _briefing(self, kind: str, *, chat_id: int | None,
                        llm: LLMService | None) -> Digest:
        """A rolling window (configurable per briefing), not a calendar day.

        A calendar-day window at 08:00 would silently drop the overnight news
        that a morning briefing exists to surface.
        """
        cfg = self.config
        top_items = int(cfg.get(f"digest.{kind}.top_items", 10))
        window_hours = as_int(cfg.get(f"digest.{kind}.window_hours", 24), 24)
        min_score = float(cfg.get(f"digest.{kind}.min_score", cfg.settings.min_article_score))
        prefs = self.news.user_for(chat_id) if chat_id else {}
        if prefs:
            min_score = max(min_score, float(prefs.get("min_score") or 0))
        tz_name = prefs.get("timezone") or cfg.settings.timezone
        zone = F._zone(tz_name)
        date_label = datetime.now(zone).strftime("%Y-%m-%d")

        # The candidate pool is deliberately deeper than the briefing: with a
        # per-source cap there must be something left to pick after it bites.
        # Already-briefed rows are skipped, which is what lets the 24h window
        # overlap the other briefing of the day without repeating it.
        #
        # `top_items * 4` was not deep enough to make that cap real. Measured on
        # 2026-09-27: the 24h pool held 41 GitHub Trending rows tied at exactly
        # 78.0 and the next best row anywhere was 76.0, so a 32-row window
        # (evening: 8 * 4) contained *nothing but* that one source. The cap
        # allowed 3 of them, `len(chosen) < top_items` tripped the thin-day
        # backfill, the backfill re-admitted the overflow it had just rejected,
        # and the delivered 晚报 was 8/8 "xx 收获 N 星" lines with The Verge,
        # Hacker News and Reddit stories left out. A cap on the final list means
        # nothing unless the pool it chooses from is wider than the flood.
        depth = max(top_items * BRIEFING_DEPTH,
                    as_int(cfg.get("digest.candidate_limit", 200), 200))
        items = self.news.latest(limit=depth, min_score=min_score, hours=window_hours,
                                 order_by_score=True, skip_sent=True)
        if len(items) < top_items:
            # Thin window: widen once, and let yesterday's items back in rather
            # than send a briefing of three.
            seen = {a.id for a in items}
            wider = self.news.latest(limit=depth, min_score=min_score,
                                     hours=window_hours * 2, order_by_score=True)
            items += [a for a in wider if a.id not in seen]
        if not items:
            return Digest(kind=kind, messages=[], date_label=date_label, empty=True)

        day_items = select_briefing(
            items, top_items=top_items,
            max_per_source=as_int(cfg.get("digest.max_per_source", 3), 3))
        # 简报里每一条都保证中文：后台翻译轮还没覆盖到的，这里当场补齐
        # （简报只有标题行 + 摘要行，没有要点，所以不替它花要点的配额）
        day_items = await self.news.ensure_chinese(day_items, with_points=False)
        blocks = F.section_blocks(
            day_items, config=cfg, tz_name=tz_name,
            top_count=int(cfg.get("digest.top_count", 3)),
            show_summary=bool(cfg.get("digest.show_summary", True)),
        )
        overview = ""
        service = self._llm(llm)
        if service.enabled and bool(cfg.get("digest.use_llm_overview", True)):
            try:
                data = await service.digest_overview([a.to_dict() for a in day_items])
                overview = data.get("overview") or ""
                highlights = data.get("highlights") or []
                if highlights:
                    overview = (overview + "\n" + "\n".join(f"{F.BULLET} {h}" for h in highlights)).strip()
            except Exception as exc:
                log.info("digest overview skipped: %s", exc)

        stats_line = (
            f"<i>覆盖 {len(day_items)} 条重点 · 最近 {window_hours} 小时 · "
            f"共 {self.news.count_since(hours=window_hours)} 条入库</i>"
        )
        header = F.digest_header(kind, cfg, date=date_label)
        chunks: list[str] = []
        if overview:
            chunks.append(f"<b>今日总体判断</b>\n{F.esc(overview)}")
        chunks.extend(blocks)
        messages = F.split_messages(chunks, header=header, footer=stats_line,
                                   limit=int(cfg.get("digest.per_message_limit", 3700)))
        return Digest(kind=kind, messages=messages, article_ids=[a.id for a in day_items],
                      date_label=date_label)

    # ------------------------------------------------------------- breaking
    async def generate_breaking(self, article_id: int, *, chat_id: int | None = None) -> Digest:
        item = self.news.by_id(article_id)
        if item is None:
            return Digest(kind="breaking", messages=[], empty=True)
        tz_name = self.config.settings.timezone
        if chat_id:
            tz_name = (self.news.user_for(chat_id).get("timezone") or tz_name)
        message = F.breaking_card(item, config=self.config, tz_name=tz_name)
        return Digest(kind="breaking", messages=[message], article_ids=[item.id])

    def can_send_breaking(self, chat_id: int, *, article_id: int | None = None,
                          respect_cooldown: bool = True) -> tuple[bool, str]:
        """Cooldown + daily cap + user switch (design doc section 16.3).

        `respect_cooldown=False` is for the 2nd..nth story of the *same* processing
        round: the cooldown exists to stop a message cannon over time, but the
        first delivery of a round starts that clock immediately, so a second,
        independent event found in the very same round was rejected as
        "cooldown 60 min left". Measured 2026-09-26: four rows cleared the gate in
        a week and only one `breaking candidate` ever got logged - two of those
        four were processed in the same minute. The daily cap below still bounds a
        round, and "already sent as breaking" still stops repeats.
        """
        cfg = self.config
        breaking_cfg = cfg.get("breaking", {}) or {}
        cooldown = int(breaking_cfg.get("cooldown_minutes", cfg.settings.breaking_cooldown_minutes))
        max_per_day = int(breaking_cfg.get("max_per_day", cfg.settings.max_breaking_news_per_day))
        if not cfg.settings.breaking_news_enabled or not breaking_cfg.get("enabled", True):
            return False, "breaking news disabled in config"
        with session_scope() as session:
            # Per reader, always: the cap and the cooldown below are meaningless if a
            # missing row turns them into a global count.
            user = repo.ledger_user(session, chat_id, timezone=cfg.settings.timezone)
            if user is not None:
                if user.paused or not user.breaking_enabled:
                    return False, "user paused / breaking off"
            if article_id is not None:
                article = session.get(Article, article_id)
                if article is None:
                    return False, "article missing"
                # The rules the pipeline applied, re-applied here rather than
                # trusted: a re-queued or backfilled story reaches the sender with
                # a score and age that have moved since the pass that flagged it.
                # `user.breaking_threshold` is only consulted in AI mode: it
                # stores the model-era default of 90, which rule mode cannot
                # reach (top score measured: 78), so honouring it there would put
                # the feature straight back to sleep.
                ok, why = breaking.gate(article, config=cfg,
                                        user_threshold=user.breaking_threshold if cfg.ai_enabled else None)
                if not ok:
                    return False, f"not breaking: {why}"
                if repo.breaking_already_sent(session, user=user, article_id=article.id):
                    return False, "already sent to this reader"
                if article.event_id and repo.breaking_already_sent(
                        session, user=user, event_id=article.event_id):
                    return False, "same event already sent to this reader"
            # After the gate and the "already sent" checks on purpose: those are
            # final for this row and must settle it, while the quiet window is only
            # a wait. A stale row deferred as "quiet" would sit in the retry queue
            # forever, because nothing in it ever expires.
            quiet = quiet_window(cfg, getattr(user, "timezone", None))
            if quiet:
                return False, quiet
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            count = repo.pushes_since(session, user=user, kind="breaking", since=today_start)
            if count >= max_per_day:
                return False, f"{_DAILY_CAP} reached ({count}/{max_per_day})"
            last = repo.last_push_of(session, user=user, kind="breaking")
            if last is not None and cooldown > 0 and respect_cooldown:
                elapsed = (datetime.utcnow() - last.created_at).total_seconds() / 60
                if elapsed < cooldown:
                    return False, f"{_COOLDOWN} {cooldown - elapsed:.0f} min left"
        return True, "ok"

    # --------------------------------------------------------------- sending
    def record_delivery(self, *, chat_id: int | None, digest: Digest) -> None:
        if digest.kind == "breaking":
            with session_scope() as session:
                user = repo.ledger_user(session, chat_id, timezone=self.config.settings.timezone)
                for article_id in digest.article_ids:
                    article = session.get(Article, article_id)
                    repo.record_push(session, user=user, kind="breaking", article_id=article_id,
                                     event_id=article.event_id if article else None)
                    session.commit()
            self.news.mark_sent(digest.article_ids, breaking=True)
            return
        self.news.mark_sent(digest.article_ids)
        with session_scope() as session:
            user = repo.ledger_user(session, chat_id, timezone=self.config.settings.timezone)
            repo.record_push(session, user=user, kind=digest.kind)
            session.commit()


_service: DigestService | None = None


def get_digest_service() -> DigestService:
    global _service
    if _service is None:
        _service = DigestService()
    return _service
