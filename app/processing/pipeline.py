"""The end-to-end pipeline (design doc section 8).

Collector -> Normalizer -> URL dedup -> title dedup -> keyword filter
-> AI relevance/category -> score -> summary -> tags -> SQLite
                                                    |-> Digest / Telegram

Everything here is defensive: one bad article or one dead provider leaves the
run standing and the news in the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import AppConfig, as_int, get_config
from app.database import repository as repo
from app.database.models import Article, Source
from app.logging_setup import get_logger
from app.processing import breaking, classifier, deduplicate, enrich, scorer, summarizer, tagger
from app.processing.normalize import is_blocked_title, is_blocked_url
from app.services.llm import LLMService

log = get_logger("app")


@dataclass
class CollectStats:
    fetched: int = 0
    stored: int = 0
    duplicates: int = 0
    blocked: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (f"fetched={self.fetched} stored={self.stored} dup={self.duplicates} "
                f"blocked={self.blocked} errors={len(self.errors)}")


@dataclass
class ProcessStats:
    scanned: int = 0
    processed: int = 0
    filtered: int = 0
    breaking: list[int] = field(default_factory=list)
    failed: int = 0

    def __str__(self) -> str:
        return (f"scanned={self.scanned} processed={self.processed} filtered={self.filtered} "
                f"breaking={len(self.breaking)} failed={self.failed}")


def source_quality(config: AppConfig, source_name: str) -> str:
    entry = config.source_by_name(source_name) or {}
    return str(entry.get("quality", "C"))


def _find_source(session: Session, name: str) -> Source | None:
    return session.scalar(select(Source).where(Source.name == name))


async def collect(
    session: Session,
    collectors: Sequence[Any],
    *,
    config: AppConfig | None = None,
    llm: LLMService | None = None,
    stats: CollectStats | None = None,
) -> CollectStats:
    """Run collectors and persist whatever is new. One failure = one error line."""
    config = config or get_config()
    stats = stats or CollectStats()
    # Keep the sources table a mirror of the config, not a fossil of the first
    # round that ever ran: a feed switched off must stop looking switched on.
    synced = repo.sync_sources(session, config.sources)
    if synced:
        session.commit()
        log.info("source registry synced with config: %d field(s) updated", synced)
    dedup_cfg = config.get("dedup", {}) or {}
    window = int(dedup_cfg.get("window_hours", 72))
    index = deduplicate.RecentIndex.from_articles(
        repo.recent_titles(session, datetime.utcnow() - timedelta(hours=window))
    )
    ai_budget = [int(dedup_cfg.get("ai_review_max_per_run", 10))]
    ai_dedup = llm if (llm and dedup_cfg.get("ai_review_enabled")) else None

    for collector in collectors:
        name = getattr(collector, "source_name", None) or collector.__class__.__name__
        try:
            items: list[dict[str, Any]] = list(await collector.collect())
        except Exception as exc:  # a dead source must not stop the run (section 21)
            log.warning("collector %s failed: %s", name, exc)
            stats.errors.append(f"{name}: {type(exc).__name__}: {exc}")
            source = _find_source(session, name)
            if source:
                repo.mark_source_fetch(session, source.id, ok=False, error=str(exc))
            session.commit()
            continue

        stored_here = 0
        source = _find_source(session, name)
        for data in items:
            stats.fetched += 1
            label = data.get("source_name") or name
            if is_blocked_url(data.get("url"), config.get("filters.url_blocklist", [])):
                stats.blocked += 1
                continue
            if is_blocked_title(data.get("title"), config.get("filters.title_blocklist", [])):
                stats.blocked += 1
                continue
            source = repo.get_or_create_source(
                session,
                name=label,
                type_=data.get("source_type", "rss"),
                # No `url=` on purpose: this is the article's own address, and
                # writing it here used to fight `sync_sources` (which mirrors the
                # feed URL from the config) once per round forever.
                quality=source_quality(config, label),
            )
            session.commit()
            data["source_id"] = source.id
            data.setdefault("community_heat", scorer.community_heat(data, config))
            try:
                outcome = await _store(session, data, index, config=config, llm=ai_dedup, ai_budget=ai_budget)
            except Exception as exc:
                log.exception("storing %r failed", data.get("title"))
                stats.errors.append(f"store: {type(exc).__name__}: {exc}")
                session.rollback()
                continue
            if outcome == "stored":
                stats.stored += 1
                stored_here += 1
            else:
                stats.duplicates += 1
        if source is not None:
            repo.mark_source_fetch(session, source.id, ok=True, items=stored_here)
        log.info("collector %s: %d new of %d fetched", name, stored_here, len(items))
    session.commit()
    return stats


async def _store(
    session: Session,
    data: dict[str, Any],
    index: deduplicate.RecentIndex,
    *,
    config: AppConfig,
    llm: LLMService | None,
    ai_budget: list[int],
) -> str:
    dup = await deduplicate.resolve_duplicate(
        session, data, index, config=config, llm=llm, ai_budget=ai_budget
    )
    if dup.duplicate and dup.matched_id:
        original = session.get(Article, dup.matched_id)
        if original is not None:
            if dup.method == "url":
                # Same URL again: fold into the existing row, never insert twice.
                merge_duplicate_row(session, original, data)
            else:
                _merge_into_event(session, original, data, method=dup.method)
            return "duplicate"
    article = repo.save_article(session, data)
    if article is None:
        return "duplicate"
    index.add(article)
    return "stored"


def merge_duplicate_row(session: Session, article: Article, data: dict[str, Any]) -> None:
    """Second sighting of an URL we already hold: keep one row, refresh signals."""
    changed = False
    if not article.content and data.get("content"):
        article.content = data["content"]
        changed = True
    if float(data.get("community_heat") or 0) > (article.community_heat or 0):
        article.community_heat = data.get("community_heat")
        changed = True
    if changed:
        article.updated_at = datetime.utcnow()
        log.debug("refreshed duplicate URL #%s %r", article.id, article.title[:60])


def _merge_into_event(session: Session, original: Article, data: dict[str, Any], *, method: str) -> None:
    """Same story, second coverage: keep the row for provenance, link it to the
    original's event, and spend no tokens on it (sections 10.5, 23)."""
    if repo.article_exists_by_hash(session, data.get("hash", "")) or (
            deduplicate.find_url_duplicate(session, data.get("url_hash", "")) is not None):
        # We already hold this exact URL somewhere - do not create a twin row.
        merge_duplicate_row(session, original, data)
        return
    event_id = original.event_id
    if event_id is None:
        event = repo.get_or_create_event(session, deduplicate.make_event_key(original.title),
                                         original.title, original)
        event_id = event.id
        original.event_id = event_id

    sibling = Article(
        source_id=data.get("source_id"),
        source_name=data.get("source_name", ""),
        source_type=data.get("source_type", "rss"),
        title=data.get("title", ""),
        url=data.get("url", ""),
        normalized_url=data.get("normalized_url", data.get("url", "")),
        url_hash=data.get("url_hash", ""),
        hash=data.get("hash", ""),
        title_norm=data.get("title_norm", ""),
        author=data.get("author"),
        content=data.get("content"),
        language=data.get("language", "en"),
        published_at=data.get("published_at") or datetime.utcnow(),
        meta={**(data.get("meta") or {}), "duplicate_of": original.id, "dedup_method": method},
        event_id=event_id,
        category=original.category,
        subcategory=original.subcategory,
        importance_score=original.importance_score,
        relevance_score=original.relevance_score,
        novelty_score=max(0.0, (original.novelty_score or 50) - 25),
        source_quality=data.get("source_quality") or original.source_quality,
        community_heat=max(original.community_heat or 0, data.get("community_heat") or 0),
        final_score=original.final_score,
        summary=original.summary,
        # The twin is stored as a finished row, so it has to carry the timestamp the
        # other two `is_processed=True` paths write. Without it the column lies by
        # omission: measured on the live box on 2026-09-27, 32 rows written after the
        # deploy were `is_processed=True` with a real `final_score` and
        # `processed_at IS NULL` - every one of them either a twin like this or a row
        # requeued below, and the twin pairs are visible as two identical titles with
        # the same score. `processed_at` is the only field that answers "when did we
        # score this", which is exactly what the breaking-news audit needed and could
        # not get.
        is_processed=True,
        processed_at=datetime.utcnow(),
    )
    session.add(sibling)
    session.flush()

    event = repo.event_for(session, event_id)
    if event is not None:
        members = repo.event_members(session, event_id)
        event.member_count = len(members)
        event.source_names = sorted({m.source_name for m in members if m.source_name})
        event.final_score = max([e.final_score or 0 for e in members] or [0])
        event.last_seen_at = datetime.utcnow()
        best = max(members, key=lambda m: (m.final_score or 0, m.source_quality or 0))
        event.canonical_article_id = best.id
        event.title = best.title
    log.debug("dedup[%s] %r -> event %s", method, data.get("title"), event_id)


async def process_pending(
    session: Session,
    *,
    config: AppConfig | None = None,
    llm: LLMService | None = None,
    limit: int | None = None,
    interests: list[dict[str, Any]] | None = None,
) -> ProcessStats:
    """Classify, score, summarise and tag everything still unprocessed."""
    config = config or get_config()
    llm = llm or LLMService(config)
    stats = ProcessStats()
    batch = limit or int(config.get("llm.process_batch_size", 25))
    articles = repo.unprocessed_articles(session, limit=batch)
    if not articles:
        return stats
    stats.scanned = len(articles)
    breaking_enabled = bool(config.get("breaking.enabled", True)) and bool(config.settings.breaking_news_enabled)
    ai_enabled = bool(config.get("llm.enabled", True)) and llm.enabled

    if not ai_enabled:
        log.info("LLM unavailable - rule pipeline only for %d article(s)", len(articles))

    budget = enrich.Budget(as_int(config.get("enrich.max_per_round", 5), 5))
    for article in articles:
        article_id = article.id
        try:
            outcome = await _process_one(
                session, article, config=config, llm=llm,
                interests=interests or [], ai_enabled=ai_enabled, enrich_budget=budget,
            )
        except Exception as exc:  # noqa: BLE001 - one bad row must not end the round
            # Roll back *first*. The old handler touched `article.process_attempts`
            # while the session was still poisoned by the failed flush, which raised
            # PendingRollbackError inside the except block, escaped `process_pending`
            # and skipped every article left in the batch - five times on the live
            # box on 2026-09-26. The "give up after 3 tries" rule below never got to
            # run because the round died before reaching it.
            session.rollback()
            stats.failed += 1
            row = session.get(Article, article_id)
            if row is not None:
                row.process_attempts = (row.process_attempts or 0) + 1
                row.process_error = f"{type(exc).__name__}: {exc}"[:400]
                if row.process_attempts >= 3:
                    # Abandoning a row also has to be timestamped: `is_processed`
                    # takes it out of the queue forever, so a NULL here would be a
                    # permanent hole in "when was this last looked at" - the same
                    # omission the twin path had. It records when we gave up, not a
                    # score time; `process_error` says which.
                    row.processed_at = datetime.utcnow()
                row.is_processed = row.process_attempts >= 3
                log.warning("processing failed for #%s (%s): %s",
                            row.id, (row.title or "")[:60], exc)
        else:
            if outcome == "filtered":
                stats.filtered += 1
            elif outcome == "failed":
                stats.failed += 1
            else:
                stats.processed += 1
                if outcome == "breaking" and breaking_enabled:
                    stats.breaking.append(article.id)
        try:
            session.commit()
        except Exception as exc:  # noqa: BLE001 - the flush can also fail at commit
            session.rollback()
            stats.failed += 1
            log.warning("commit failed after article #%s: %s", article_id, exc)
    if budget.used:
        log.info("fetched %d article page(s) for full text", budget.used)
    return stats


async def _process_one(
    session: Session,
    article: Article,
    *,
    config: AppConfig,
    llm: LLMService,
    interests: list[dict[str, Any]],
    ai_enabled: bool,
    enrich_budget: enrich.Budget | None = None,
) -> str:
    quality = source_quality(config, article.source_name)
    data: dict[str, Any] = {
        "id": article.id,
        "title": article.title,
        "url": article.url,
        "source_name": article.source_name,
        "content": article.content or "",
        "published_at": article.published_at,
        "meta": article.meta or {},
        "quality": quality,
        "language": article.language or "en",
        "community_heat": article.community_heat or 0,
    }
    keep, hits = classifier.rule_filter(data, config)
    if not keep:
        _settle_filtered(article, "no AI keyword hit", config)
        log.debug("filtered by rules: %r", article.title)
        return "filtered"

    if await enrich.maybe_enrich(data, article, config,
                                 budget=enrich_budget or enrich.Budget(0)):
        # Score the article on its own text. The verdict is never reversed here:
        # a measured HF page extracted 6.8 KB with zero keyword hits (a docs page),
        # and dropping news that the stub accepted would trade a scoring bug for
        # a recall bug. An unconvincing page simply stays low-scoring.
        _keep, hits = classifier.rule_filter(data, config)

    if not ai_enabled:
        # Cheap path: rules only. News still reaches Telegram, just coarser.
        category, subcategory, _confidence = classifier.rule_classify(data, config)
        data["tags"] = tagger.rule_tags(data, config)
        # Gentle curves on purpose: with an AI pass unavailable the keyword count
        # is a weak signal, and a flat 100 for everything would destroy ranking.
        scores = scorer.compute_scores(
            {**data, "category": category},
            importance=min(85.0, 28 + 4.5 * len(hits) + (12 if quality == "A" else 0)),
            relevance=min(88.0, 32 + 4.5 * len(hits)),
            quality=quality,
            interests=interests,
            config=config,
        )
        summary = summarizer.fallback_summary(data, language=data["language"])
        _apply(session, article, category=category or config.fallback_category, subcategory=subcategory,
               scores=scores, summary=summary, tags=data["tags"], method="rule", config=config)
    else:
        cls = await classifier.classify(data, interests=interests, config=config, llm=llm)
        if not cls.get("is_ai_related", True) and float(cls.get("relevance_score") or 0) < 30:
            _settle_filtered(article, "AI judged not relevant", config,
                             relevance=float(cls.get("relevance_score") or 0))
            return "filtered"
        summ = await summarizer.summarize(data, config=config, llm=llm)
        data["tags"] = tagger.merge_tags(tagger.rule_tags(data, config), cls.get("tags"), summ.get("tags"))
        scores = scorer.compute_scores(
            {**data, "category": cls["category"]},
            importance=max(float(cls.get("importance_score") or 0), float(summ.get("importance_score") or 0)),
            relevance=float(cls.get("relevance_score") or 50),
            novelty_value=float(cls.get("novelty_score") or 80),
            quality=quality,
            interests=interests,
            config=config,
        )
        _apply(session, article, category=cls["category"], subcategory=cls.get("subcategory"),
               scores=scores, summary=summ, tags=data["tags"], method=cls.get("method", "ai"),
               config=config)
        degraded = summ.get("error") or cls.get("error")
        if degraded:
            # Stored and pushed, but flagged: the AI pass was degraded.
            article.process_error = f"ai-degraded: {degraded}"[:300]
            article.meta = {**(article.meta or {}), "ai_degraded": True}

    verdict, why = breaking.gate(article, config=config, ai_enabled=ai_enabled)
    if verdict:
        # Recorded on the row so a live alert can be audited afterwards, and
        # logged: this feature ran for days with no log line at all, which is
        # exactly why "0 alerts" looked like "a quiet news day".
        article.meta = {**(article.meta or {}), "breaking_reason": why}
        log.info("breaking candidate #%s: %s - %s", article.id, (article.title or "")[:70], why)
        return "breaking"
    log.debug("not breaking #%s: %s", article.id, why)
    return "processed"


def _settle_filtered(article: Article, reason: str, config: AppConfig, *, relevance: float = 0) -> None:
    article.filtered_out = True
    article.is_processed = True
    article.processed_at = datetime.utcnow()
    article.process_error = reason
    article.relevance_score = relevance
    article.final_score = 0
    article.novelty_score = article.novelty_score or 50
    article.category = article.category or config.fallback_category
    article.summary = article.summary or article.title


def _apply(
    session: Session,
    article: Article,
    *,
    category: str,
    subcategory: str | None,
    scores: dict[str, float],
    summary: dict[str, Any],
    tags: Iterable[str],
    method: str,
    config: AppConfig,
) -> None:
    article.category = category
    article.subcategory = subcategory
    article.importance_score = scores.get("importance_score", 0)
    article.relevance_score = scores.get("relevance_score", 0)
    article.novelty_score = scores.get("novelty_score", 50)
    article.source_quality = scores.get("source_quality", 50)
    article.community_heat = scores.get("community_heat", 0)
    article.final_score = scores.get("final_score", 0)
    previous_summary = article.summary
    article.summary = (summary.get("summary") or article.title)[:600]
    if previous_summary and previous_summary != article.summary:
        # Any re-pass that rewrites the summary invalidates its translation:
        # `display_summary` prefers the Chinese line, so a stale one would hide
        # the improvement instead of showing it.
        enrich.drop_stale_translation(article)
    article.key_points = list(summary.get("key_points") or [])
    article.why_it_matters = (summary.get("why_it_matters") or "").strip() or None
    article.meta = {
        **(article.meta or {}),
        "pipeline": method,
        "score_detail": {k: v for k, v in scores.items() if k != "final_score"},
    }
    article.is_processed = True
    article.process_error = None
    article.process_attempts = (article.process_attempts or 0) + 1
    article.processed_at = datetime.utcnow()
    extra_tags = list(tags)
    if annotate_free_offer(article):
        extra_tags += ["免费", article.free_offer_tool or ""]
    repo.attach_tags(session, article, [t for t in extra_tags if t])
    _link_event(session, article)
    if not article.why_it_matters:
        # Rule mode never writes this field, so the card's 为什么值得关注 heading has
        # never appeared on a key-less box. Filled from the event link above, which
        # is why it runs after `_link_event` and not in `_process_one`.
        event = repo.event_for(session, article.event_id) if article.event_id else None
        article.why_it_matters = summarizer.compose_why_it_matters(
            article, event, config=config) or None


def annotate_free_offer(article: Article, *, config: AppConfig | None = None) -> bool:
    """Flag "this tool/model is free right now" items for the /免费 command."""
    from app.processing import free_offers

    offer = free_offers.detect_article(
        {"title": article.title, "summary": article.summary,
         "summary_zh": getattr(article, "summary_zh", None), "content": article.content},
        config=config)
    if offer is None:
        return False
    article.is_free_offer = True
    article.free_offer_tool = offer.tool
    article.free_offer = offer.to_dict()
    return True


def backfill_free_offers(session: Session, *, config: AppConfig | None = None,
                         limit: int = 5000) -> int:
    """Re-scan stored articles with the current vocabulary.

    Token-free and cheap, so it can run right after a deploy: adding a tool name
    to config/free_offers.yaml immediately applies to yesterday's news as well.
    """
    config = config or get_config()
    rows = list(session.scalars(select(Article).where(Article.is_archived.is_(False)).limit(limit)))
    marked = 0
    for article in rows:
        if annotate_free_offer(article, config=config):
            marked += 1
        elif article.is_free_offer:
            # 词表收紧后要能撤掉旧的误报，否则 /免费 会一直带着脏数据
            article.is_free_offer = False
            article.free_offer_tool = None
            article.free_offer = None
    session.flush()
    log.info("free-offer scan: %d of %d article(s) marked", marked, len(rows))
    return marked



def repair_truncated_summaries(session: Session, *, limit: int = 400,
                               within_hours: int = 60) -> int:
    """Re-cut stored summaries that a hard `[:160]` stopped mid-word.

    The briefing reads the summary the pipeline stored, so fixing the producer
    only helps tomorrow's news: tonight's lines still read `…with a wall of`.
    Rows the AI wrote are left alone - only rule-mode summaries are rebuilt,
    from the content already in the row, and any Chinese line derived from the
    cut text is dropped so it gets re-translated.
    """
    since = datetime.utcnow() - timedelta(hours=within_hours)
    rows = list(session.scalars(
        select(Article)
        .where(Article.is_processed.is_(True), Article.published_at >= since,
               Article.summary.is_not(None))
        .order_by(Article.id.desc()).limit(limit)))
    fixed = 0
    for row in rows:
        summary = (row.summary or "").strip()
        if len(summary) < 100:
            continue
        if summary.endswith("…") and "&" not in summary:
            continue          # already re-cut; a stray "&" means undecoded feed junk
        if (row.meta or {}).get("pipeline") != "rule" or not row.content:
            continue
        rebuilt = (summarizer.fallback_summary(
            {"title": row.title, "content": row.content}).get("summary") or "").strip()
        if not rebuilt or rebuilt == summary or len(rebuilt) > len(summary) + 1:
            continue
        row.summary = rebuilt
        if row.summary_zh:
            row.summary_zh = None
            row.translated_by = None
        fixed += 1
    return fixed


def repair_template_titles(session: Session, *, limit: int = 5000) -> int:
    """Give templated feed titles a proper Chinese form.

    Rows stored before the localizer exist either as the English original
    ("v1.2 released in owner/repo") or as machine-translated word-order junk
    ("owner/repo中发布的v1.2"). Both are token-free to fix, so this runs at boot
    next to the free-offer rescan.
    """
    from app.services.translate import localize_title

    # No LIKE filter here: the templated shapes are few and the patterns are
    # easy to get wrong (`(0 stars)` does not contain `) stars)`), while this
    # scan is cheap and token-free.
    rows = list(session.scalars(
        select(Article).where(Article.is_archived.is_(False)).limit(limit)))
    fixed = 0
    for row in rows:
        template = localize_title(row.title)
        if template and row.title_zh != template:
            row.title_zh = template
            row.translated_by = "template"
            fixed += 1
    session.flush()
    if fixed:
        log.info("repaired %d templated title(s) without spending tokens", fixed)
    return fixed


def repair_lost_units(session: Session, *, limit: int = 1500) -> int:
    """Clear the Chinese of any row whose translation lost a guarded measurement symbol.

    The placeholder guard only protects text translated after it shipped, so the
    library still holds "速度可达50吨/秒" for a source that said `50t/s` and
    "0.9 KB/令牌" for `0.9 KB/token`. Same invariant the brand sweep uses - a
    guarded symbol present in the English and absent from the Chinese means the
    answer is void - and clearing the field is the only honest repair: the queue
    redoes it under the guard, and if every route mangles it again the row shows
    English, which he prefers over a wrong unit.
    """
    from app.services.translate import term_in

    units = [str(u) for u in (get_config().get("translate.keep_units", []) or []) if str(u).strip()]
    if not units:
        return 0
    rows = list(session.scalars(
        select(Article).where(Article.is_archived.is_(False)).limit(limit)))
    dropped = 0
    for row in rows:
        for source, translated, field in ((row.title, row.title_zh, "title_zh"),
                                          (row.summary, row.summary_zh, "summary_zh")):
            if not source or not translated:
                continue
            # The source decides what must survive: a row that says `50t/s` has to
            # still say `t/s` in Chinese, or the number is measuring something else.
            wanted = [unit for unit in units if term_in(source, unit)]
            if wanted and not all(term_in(translated, unit) for unit in wanted):
                setattr(row, field, None)
                row.translated_by = None
                dropped += 1
    session.flush()
    if dropped:
        log.info("dropped %d Chinese field(s) that lost a guarded unit; requeued", dropped)
    return dropped

async def translate_pending(
    session: Session,
    *,
    config: AppConfig | None = None,
    translator: Any = None,
    limit: int | None = None,
) -> int:
    """Give processed-but-English rows a Chinese title/summary.

    Doubles as the backfill for news collected before translation existed: the
    queue is score-ordered, so the free provider's daily budget goes to the
    stories that can actually reach a briefing.
    """
    from app.services.translate import get_translator, localize_title, needs_translation

    config = config or get_config()
    translator = translator or get_translator(config)
    if not translator.enabled:
        return 0

    batch = limit or int(config.get("translate.per_run_limit", 120))
    rows = repo.untranslated_articles(session, limit=batch)
    if not rows:
        # An empty headline queue is the normal state on a box that has been
        # running for a week. Returning here would mean the 核心内容 pass below
        # never runs at all, which is how the config's promise stayed a promise.
        return len(await _translate_key_points(session, translator, config))

    titles = [row.title for row in rows if needs_translation(row.title)]
    summaries = [row.summary for row in rows if needs_translation(row.summary)]
    translated_titles = await translator.translate_many(titles, hint="title")
    translated_summaries = await translator.translate_many(summaries, hint="summary")

    mode = getattr(translator, "last_route", None) or translator.mode()
    done = 0
    for row in rows:
        before = (row.title_zh, row.summary_zh)
        if not row.title_zh:
            templated = localize_title(row.title)
            if templated:
                # Composed, not translated: correct word order and no quota spent.
                row.title_zh = templated
                row.translated_by = "template"
            else:
                zh_title = translated_titles.get(row.title)
                if zh_title:
                    row.title_zh = zh_title
                    row.translated_by = mode
                elif not needs_translation(row.title):
                    row.title_zh = row.title      # already Chinese: leave the queue
                    row.translated_by = "native"
        # Handled independently of the title. The old `continue` in the templated
        # branch meant every GitHub release row kept an English summary forever,
        # and a queue keyed only on `title_zh` never looked at them again.
        if not row.summary_zh and row.summary:
            zh_summary = translated_summaries.get(row.summary)
            if zh_summary:
                row.summary_zh = zh_summary
            elif not needs_translation(row.summary):
                row.summary_zh = row.summary
        if (row.title_zh, row.summary_zh) != before:
            done += 1
    session.flush()
    if done:
        log.info("translated %d/%d article(s) to Chinese via %s", done, len(rows), mode)
    # A row can be moved by both passes in one round; report it once, or the
    # "N article(s)" line overstates what the round did.
    pointed = await _translate_key_points(session, translator, config)
    return done + len(pointed - {row.id for row in rows})


async def _translate_key_points(session: Session, translator: Any, config: AppConfig) -> set[int]:
    """Spend whatever budget the headlines left on 核心内容, as `translate` promises.

    Returns the ids of rows whose bullets turned Chinese.

    `settings.yaml` has said "要点排在标题和摘要之后，剩余额度才轮到它" for weeks,
    but the background round only ever queued titles and summaries: a Chinese
    核心内容 block appeared solely on cards somebody happened to open, and every
    other row showed no block at all (295 of 423 openable rows on the live box).
    Two attempts per row, then it stops asking - a body sentence the free provider
    refuses must not take the whole budget every round for ever.
    """
    from app.services.news import CARD_POINTS
    from app.services.translate import needs_translation

    if not translator.budget.available():
        return set()
    scan = int(config.get("translate.points_scan_rows", 120))
    limit = int(config.get("translate.points_per_run", 40))
    rows = [row for row in repo.rows_with_key_points(session, limit=scan)
            if not (row.meta or {}).get("key_points_zh")
            and (row.meta or {}).get("key_points_tried", 0) < 2]
    if not rows:
        return set()
    asked: list[str] = []
    for row in rows:
        for point in (row.key_points or [])[:CARD_POINTS]:
            text = str(point or "").strip()
            if text and needs_translation(text) and text not in asked:
                asked.append(text)
    if not asked:
        return set()
    got = await translator.translate_many(asked[:limit], hint="summary")
    pointed: set[int] = set()
    for row in rows:
        points = [got[str(point).strip()] for point in (row.key_points or [])[:CARD_POINTS]
                  if str(point or "").strip() in got]
        if points:
            row.meta = {**(row.meta or {}), "key_points_zh": points}
            pointed.add(row.id)
        else:
            row.meta = {**(row.meta or {}),
                        "key_points_tried": (row.meta or {}).get("key_points_tried", 0) + 1}
    if pointed:
        log.info("translated key points for %d article(s), %d of %d string(s) asked",
                 len(pointed), len(got), min(len(asked), limit))
    return pointed


def _link_event(session: Session, article: Article) -> None:
    """Guarantee every processed article belongs to an event, so the digest and
    Telegram list views can collapse multi-source coverage to one line."""
    if article.event_id is None:
        event = repo.get_or_create_event(
            session, deduplicate.make_event_key(article.title), article.title, article
        )
        article.event_id = event.id
    event = repo.event_for(session, article.event_id)
    if event is None:
        return
    members = repo.event_members(session, article.event_id)
    event.member_count = len(members)
    event.source_names = sorted({m.source_name for m in members if m.source_name})
    event.final_score = max([m.final_score or 0 for m in members] + [event.final_score or 0])
    best = max(members, key=lambda m: (m.final_score or 0, m.published_at or datetime.min))
    event.canonical_article_id = best.id
    event.summary = best.summary
    for member in members:
        if member.id != best.id and (member.final_score or 0) < (best.final_score or 0):
            # The duplicate coverage inherits the canonical view, so any query
            # that reaches it still renders the same story consistently.
            member.category = best.category
            member.subcategory = best.subcategory
            member.importance_score = best.importance_score
            member.relevance_score = best.relevance_score
            member.novelty_score = max(0.0, (best.novelty_score or 0) - 25)
            member.final_score = best.final_score
            member.summary = best.summary
    event.last_seen_at = datetime.utcnow()
