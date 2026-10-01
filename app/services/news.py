"""Service layer: business rules the bot and scheduler share (§27).

Handlers never touch SQL; they call these services and get plain data objects
back, so SQLAlchemy sessions never leak into async Telegram code paths.
"""

from __future__ import annotations

import json
import os
import shutil
import time

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.config import AppConfig, as_int, get_config, local_day_start
from app.database import repository as repo
from app.database.database import session_scope
from app.database.models import Article, Source, User
from app.logging_setup import get_logger
from app.services.llm import LLMService, get_llm

log = get_logger("app")

# How many bullets of one article a card can show; mirrors the render cap in
# `format.article_card`, so we never pay to translate lines nobody sees.
CARD_POINTS = 5


@dataclass
class ArticleView:
    """Detachable snapshot of an article, safe to use after the session ends."""

    id: int
    title: str
    url: str
    source_name: str
    source_type: str
    category: str | None
    subcategory: str | None
    summary: str | None
    key_points: list[str] = field(default_factory=list)
    why_it_matters: str | None = None
    tags: list[str] = field(default_factory=list)
    published_at: datetime | None = None
    final_score: float = 0
    importance_score: float = 0
    relevance_score: float = 0
    novelty_score: float = 50
    source_quality: float = 50
    community_heat: float = 0
    language: str = "en"
    content: str | None = None
    event_id: int | None = None
    event_sources: list[str] = field(default_factory=list)
    event_members: int = 1
    meta: dict[str, Any] = field(default_factory=dict)
    title_zh: str | None = None
    summary_zh: str | None = None
    translated_by: str | None = None
    is_free_offer: bool = False
    free_offer: dict[str, Any] | None = None

    # 中文优先：显示层永远先看 zh 字段，缺失才回落到原文。
    # 两道出口都要遮凭据：`esc()` 是最后一道，但面板会**先截断再交给 esc**
    # （`head[:120]`、`display_title[:30]`），截到邮箱域名中间就拼不出完整的邮箱正则了。
    # 这一层的用例（tests/test_redact_secrets.py）第一版就是这么漏掉的。
    @property
    def display_title(self) -> str:
        from app.services.format import redact_secrets

        if not self.title_zh:
            return redact_secrets(self.title)
        from app.services.translate import fix_wrong_sense

        return redact_secrets(fix_wrong_sense(self.title_zh, self.title))

    @property
    def display_summary(self) -> str | None:
        from app.processing.normalize import strip_feed_boilerplate, unescape_entities

        # Stripping here as well fixes rows stored before the collector learned
        # to drop "20 posts - 17 participants / Read full topic", and decoding
        # fixes rows stored before feeds stopped leaking "&amp;#128064;".
        # 2026-09-26, his call: an untranslated summary is displayed in English
        # instead of being hidden. A missing line teaches him nothing, and the
        # free translation quota runs out most evenings.
        text = self.summary_zh or self.summary
        if self.summary_zh:
            from app.services.translate import fix_wrong_sense

            text = fix_wrong_sense(text, " ".join(filter(None, (self.title, self.content))))
        from app.services.format import redact_secrets

        return redact_secrets(unescape_entities(strip_feed_boilerplate(text))) or None

    @property
    def translated_summary(self) -> str | None:
        """Chinese summary only - the only kind allowed to replace the headline.

        「只有 `summary_zh` 非空」不等于"它是中文"：真机 2026-10-01 的只读探针数到
        近 24 小时 288 行非空里有 10 行一个汉字都没有（全库 39 行）——那是
        `pipeline._note_zh_miss` 认命时抄进去的原文（一段英文、一个 reddit 图片直链、
        GitHub 仓库名，甚至别人泄露的账号密码）。于是列表首行会用英文/URL 盖掉一个
        本来就有的中文标题。这里必须验内容，不能只验列非空。
        """
        from app.services.translate import has_cjk

        if not self.summary_zh or not has_cjk(self.summary_zh):
            return None
        return self.display_summary

    @property
    def display_line(self) -> str:
        """What a headline row shows: 中文摘要 > 标题（中文优先，否则原文）。"""
        return self.translated_summary or self.display_title

    @property
    def display_key_points(self) -> list[str]:
        """Bullets fit to render in a Chinese briefing - translated or AI-written.

        Rule-mode `key_points` are the first sentences of the English body. While
        none of them had Chinese at all (295 of the 423 openable rows on the live
        box) the card dropped the 核心内容 block in silence, which reads as a broken
        card, not as a missing translation. His standing call is that English beats
        nothing, so they render - labelled, via `key_points_in_english`.
        """
        stored = self.meta.get("key_points_zh")
        if isinstance(stored, list) and stored:
            from app.services.translate import fix_wrong_sense

            english = " ".join(str(point or "") for point in self.key_points)
            return [fix_wrong_sense(str(point), english) for point in stored
                    if str(point).strip()]
        from app.services.translate import has_cjk

        chinese = [point for point in self.key_points if has_cjk(point)]
        return chinese or [str(point) for point in self.key_points if str(point).strip()]

    @property
    def key_points_in_english(self) -> bool:
        """True when 核心内容 below is the untouched original, not our Chinese text."""
        if not self.key_points or self.meta.get("key_points_zh"):
            return False
        from app.services.translate import has_cjk

        return not any(has_cjk(point) for point in self.key_points)

    @property
    def needs_translation(self) -> bool:
        from app.services.translate import has_cjk, needs_translation

        if needs_translation(self.title) or needs_translation(self.summary):
            return True
        # Bullets count too, or the card below a translated headline stays English
        # forever; `key_points_zh` is written once so this does not re-spend.
        if self.meta.get("key_points_zh"):
            return False
        return any(needs_translation(point) and not has_cjk(point) for point in self.key_points)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "source_name": self.source_name,
            "source_type": self.source_type,
            "category": self.category,
            "subcategory": self.subcategory,
            "summary": self.summary,
            "key_points": self.key_points,
            "why_it_matters": self.why_it_matters,
            "tags": self.tags,
            "published_at": self.published_at.isoformat(timespec="minutes") if self.published_at else None,
            "final_score": self.final_score,
            "importance_score": self.importance_score,
            "relevance_score": self.relevance_score,
            "novelty_score": self.novelty_score,
            "source_quality": self.source_quality,
            "community_heat": self.community_heat,
            "language": self.language,
            "title_zh": self.title_zh,
            "summary_zh": self.summary_zh,
            "display_title": self.display_title,
            "display_summary": self.display_summary,
            "event_id": self.event_id,
            "event_sources": self.event_sources,
            "meta": self.meta,
        }
        return data

    @property
    def score_label(self) -> str:
        return f"{self.final_score:.0f}"

    def local_time(self, tz_name: str) -> str:
        if not self.published_at:
            return ""
        local = self.published_at.replace(tzinfo=timezone.utc).astimezone(_tz(tz_name))
        return local.strftime("%m-%d %H:%M")


def _tz(name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # pragma: no cover - unknown tz on a slim OS image
        return timezone.utc


def _views(session: Session, articles: Iterable[Article]) -> list[ArticleView]:
    out = []
    for article in articles:
        out.append(_view(session, article))
    return out


def _view(session: Session, article: Article) -> ArticleView:
    sources: list[str] = []
    members = 1
    if article.event_id:
        event = repo.event_for(session, article.event_id)
        if event is not None:
            # 同一口径两处：`summarizer` 只数"别家媒体"，卡片也必须只数列。
            # 线上 33 个多行事件里有 21 个其实只有一个来源（Linux.do 的转载、同一
            # feed 的第二条），旧写法会给正在读 TechCrunch AI 的他显示
            # 「相关来源：TechCrunch AI」——那不是补充信息，是说谎。
            names = [str(name) for name in (event.source_names or []) if name]
            outlets = list(dict.fromkeys(names))
            sources = [name for name in outlets if name != article.source_name]
            members = len(outlets) or 1
    return ArticleView(
        id=article.id,
        title=article.title,
        url=article.url,
        source_name=article.source_name,
        source_type=article.source_type,
        category=article.category,
        subcategory=article.subcategory,
        summary=article.summary,
        key_points=list(article.key_points or []),
        why_it_matters=article.why_it_matters,
        tags=[t.name for t in (article.tags or [])],
        published_at=article.published_at,
        final_score=article.final_score or 0,
        importance_score=article.importance_score or 0,
        relevance_score=article.relevance_score or 0,
        novelty_score=article.novelty_score or 0,
        source_quality=article.source_quality or 0,
        community_heat=article.community_heat or 0,
        language=article.language or "en",
        content=article.content,
        event_id=article.event_id,
        event_sources=sources,
        event_members=members,
        meta=dict(article.meta or {}),
        title_zh=article.title_zh,
        summary_zh=article.summary_zh,
        translated_by=article.translated_by,
        is_free_offer=bool(article.is_free_offer),
        free_offer=dict(article.free_offer or {}) if article.free_offer else None,
    )


@dataclass
class DayPage:
    """一天（读者当地日）的一页新闻，加上"这一天到底有几条"。

    `total` 与列表同义（按事件去重后能看到几条），不是库里的行数：
    屏幕上出现过的数字必须能从同一个定义算出来，否则"共 N 条"和"这里列出 M 条"
    会在同一行里互相反悔。
    """

    items: list[ArticleView]
    label: str
    total: int
    page: int
    pages: int


class NewsService:
    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()

    # ---------------------------------------------------------------- reads
    def latest(self, *, limit: int = 10, min_score: float | None = None,
               hours: int = 72, category: str | None = None,
               order_by_score: bool = False, skip_sent: bool = False) -> list[ArticleView]:
        """Recent articles. `order_by_score` asks for the best, not the newest.

        Both orders matter: /最新 wants arrival, while a briefing that lists
        "the 10 newest" ends up being whatever feed polled last. `skip_sent`
        leaves out what an earlier briefing already delivered.
        """
        since = datetime.utcnow() - timedelta(hours=hours)
        threshold = min_score if min_score is not None else self.default_min_score()
        with session_scope() as session:
            articles = repo.query_articles(
                session, since=since, min_score=threshold, category=category,
                limit=limit * 2, order_by_score=order_by_score, skip_sent=skip_sent,
            )
            return _views(session, articles[:limit])

    def count_eligible(self, *, hours: int = 24, min_score: float | None = None,
                       category: str | None = None) -> int:
        """这段时间里能看到几条，不构造视图（口径 = 列表的口径，按事件去重）。"""
        since = datetime.utcnow() - timedelta(hours=hours)
        threshold = min_score if min_score is not None else self.default_min_score()
        with session_scope() as session:
            return repo.count_eligible(session, since=since, min_score=threshold,
                                       category=category)

    def day(self, *, offset_days: int = 0, limit: int = 20, page: int = 1,
            min_score: float | None = None,
            tz_name: str | None = None) -> DayPage:
        """某一天（按读者当地日）的新闻，翻得动、也报得出总数。

        这里原来只有 `limit=20` 而没有任何"今天一共几条"的说法：真机 2026-10-01 量到
        `/today` 展示 20 条而当天符合条件的是 101 条，`/yesterday` 是 20 / 287。
        他看到的就是"今天的全部"，而第 21 条以后既看不到也没有入口。
        """
        tz_name = tz_name or self.config.settings.timezone
        zone = _tz(tz_name)
        today_local = datetime.now(zone).date() + timedelta(days=offset_days)
        start = datetime.combine(today_local, datetime.min.time(), tzinfo=zone).astimezone(
            timezone.utc
        ).replace(tzinfo=None)
        end = start + timedelta(days=1)
        threshold = min_score if min_score is not None else self.default_min_score()
        page = max(1, int(page))
        with session_scope() as session:
            total = repo.count_eligible(session, since=start, until=end, min_score=threshold)
            articles = repo.query_articles(
                session, since=start, until=end, min_score=threshold, limit=limit,
                offset=(page - 1) * limit, order_by_score=True,
            )
            pages = max(1, -(-total // limit))
            return DayPage(_views(session, articles), today_local.isoformat(),
                           total, min(page, pages), pages)

    def category_min_score(self) -> float:
        """分类页的门槛。列表与"这个分类有几条"必须用同一个数字，否则标题会少报。"""
        return self.default_min_score(min_floor=25)

    def count_category(self, category: str, *, days: int = 7) -> int:
        """该分类在 N 天窗口里能看到几条（与 `by_category` 同门槛、同去重口径）。"""
        since = datetime.utcnow() - timedelta(days=days)
        with session_scope() as session:
            return repo.count_eligible(session, since=since, category=category,
                                       min_score=self.category_min_score())

    def by_category(self, category: str, *, limit: int = 10, days: int = 3) -> list[ArticleView]:
        since = datetime.utcnow() - timedelta(days=days)
        with session_scope() as session:
            return _views(session, repo.query_articles(
                session, since=since, category=category, limit=limit, order_by_score=True,
                min_score=self.category_min_score(),
            ))

    def free_offers(self, *, days: int = 30, limit: int = 20,
                    tool: str | None = None) -> list[ArticleView]:
        """/免费 的数据：近期"某工具/模型现在免费"的资讯。"""
        with session_scope() as session:
            return _views(session, repo.free_offers(session, days=days, limit=limit * 2, tool=tool))[:limit]

    def free_offer_count(self, *, days: int = 30, tool: str | None = None) -> int:
        """`/免费` 标题要的"一共有几条"：SQL 数，不受 `free.limit` 那块页大小影响。"""
        with session_scope() as session:
            return repo.count_free_offers(session, days=days, tool=tool)

    def free_offer_tools(self, *, days: int = 30, limit: int = 12) -> list[dict[str, Any]]:
        from app.processing.free_offers import kind_emoji

        with session_scope() as session:
            rows = repo.free_offer_tools(session, days=days, limit=limit)
        return [{"tool": name, "count": count, "emoji": kind_emoji("其他"), } for name, count in rows]

    def by_id(self, article_id: int) -> ArticleView | None:
        with session_scope() as session:
            article = repo.get_article(session, article_id)
            return _view(session, article) if article else None

    def nth_of_latest(self, index: int, *, limit: int = 10) -> ArticleView | None:
        items = self.latest(limit=limit)
        if 1 <= index <= len(items):
            return items[index - 1]
        return None

    def trending(self, *, hours: int = 48, limit: int = 10) -> list[ArticleView]:
        with session_scope() as session:
            return _views(session, repo.trending(session, hours=hours, limit=limit))

    def count_since(self, *, hours: int = 24) -> int:
        with session_scope() as session:
            return repo.count_since(session, datetime.utcnow() - timedelta(hours=hours))

    def sources(self) -> list[dict[str, Any]]:
        # 采集器此刻真实退避了多久，只有运行中的进程知道；`/来源` 要说"还在等"，
        # 而不是去猜 `last_error` 的中文文案（v1.68 之后等待也会留在那一格里）。
        from app.collectors.base import cooling

        with session_scope() as session:
            return [
                {
                    "name": s.name,
                    "type": s.type,
                    "enabled": s.enabled,
                    "quality": s.quality,
                    "url": s.url,
                    "last_fetch_at": s.last_fetch_at,
                    "last_success_at": s.last_success_at,
                    "last_error": s.last_error,
                    "error_count": s.error_count,
                    "items": s.item_count,
                    "wait_left": cooling(str(s.url or "")) if s.url else 0.0,
                }
                for s in repo.all_sources(session)
            ]

    def configured_sources(self) -> list[dict[str, Any]]:
        return [
            {"name": s.get("name"), "type": s.get("type", "rss"), "enabled": bool(s.get("enabled", True)),
             "quality": s.get("quality", "C"), "url": s.get("url") or s.get("rss_url")}
            for s in self.config.sources
        ]

    def topics(self) -> list[dict[str, Any]]:
        with session_scope() as session:
            since = datetime.utcnow() - timedelta(days=7)
            # SQL counts the whole window. This used to tally one
            # `query_articles(limit=1000)` page, which quietly turned every number
            # on `/topics` into a floor: measured 2026-10-01, 报表 1000 条 / 真实 1801 条，
            # `Other` 显示 68 而真值是 335。
            counts = repo.category_counts(session, since=since)
        # 没有分类的行并到兜底栏目（以前在 Python 里逐行数时就是这个语义）。
        misc = counts.pop("", 0)
        if misc:
            fallback = self.config.fallback_category
            counts[fallback] = counts.get(fallback, 0) + misc
        order = self.config.get("digest.section_order", []) or []
        ranked = sorted(counts.items(), key=lambda kv: (order.index(kv[0]) if kv[0] in order else 99, -kv[1]))
        return [
            {
                "category": name,
                # 中文栏目名给人看；`category` 仍然是英文键（数据库列与 prompt 都用它）。
                "label": self.config.category_label(name),
                "count": count,
                "emoji": self.config.get(f"digest.section_emoji.{name}", "📰"),
                "description": self.config.category_meta(name).get("description", ""),
            }
            for name, count in ranked
        ]

    async def ensure_chinese(self, items: list[ArticleView], *,
                             with_points: bool = True) -> list[ArticleView]:
        """Translate exactly these items, right before they are rendered.

        Covers the gap between "collected" and "the nightly translation round has
        reached it": whatever the user is about to look at gets Chinese first,
        and the result is written back so it is paid for only once.

        Per field, and only for the fields this surface shows. The item-level
        decision re-asked for a headline that is already in `title_zh` whenever
        anything else about that row was missing: measured on the live box, one
        chat question over 12 already-translated rows cost 9.7 seconds and asked
        the free provider for 24 strings it had answered before. `with_points`
        covers the other half - a list has no bullets to render.
        """
        from app.services.translate import get_translator

        translator = get_translator(self.config)
        if not translator.enabled:
            return items
        todo = [item for item in items if item.needs_translation]
        if not todo:
            return items
        titles = await translator.translate_many(
            [i.title for i in todo if not i.title_zh], hint="title")
        summaries = await translator.translate_many(
            [i.summary for i in todo if i.summary and not i.summary_zh], hint="summary")
        # Bullets are asked for last: the headline and the one-line summary own the
        # free quota, and whatever is left after them belongs to these.
        bullets = [point for item in todo if with_points and not item.meta.get("key_points_zh")
                   for point in (item.key_points or [])[:CARD_POINTS]]
        points = await translator.translate_many(bullets, hint="summary") if bullets else {}
        if not (titles or summaries or points):
            return items
        mode = translator.mode()
        with session_scope() as session:
            for item in todo:
                zh_title = titles.get(item.title)
                zh_summary = summaries.get(item.summary or "")
                zh_points = [points[p] for p in (item.key_points or [])[:CARD_POINTS] if p in points]
                if not (zh_title or zh_summary or zh_points):
                    continue
                if zh_title:
                    item.title_zh = zh_title
                if zh_summary:
                    item.summary_zh = zh_summary
                if zh_points:
                    item.meta = {**(item.meta or {}), "key_points_zh": zh_points}
                row = repo.get_article(session, item.id)
                if row is not None:
                    if zh_title:
                        row.title_zh = zh_title
                    if zh_summary or item.summary_zh:
                        row.summary_zh = row.summary_zh or item.summary_zh
                    if zh_points:
                        row.meta = {**(row.meta or {}), "key_points_zh": zh_points}
                    row.translated_by = mode
            session.commit()
        return items

    # ------------------------------------------------------------ disk history
    DISK_HISTORY_FILE = "disk_history.json"
    DISK_WINDOW_HOURS = 24
    # 跨度不到半天就不给"还剩几天"：4 小时的斜率不是"每天"的速率（见 disk_trend）
    MIN_COUNTDOWN_SPAN_HOURS = 12

    def _disk_history_path(self) -> Path:
        return self.config.settings.data_path / self.DISK_HISTORY_FILE

    def _disk_samples(self, now: float) -> list[list[float]]:
        try:
            raw = json.loads(self._disk_history_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        cutoff = now - self.DISK_WINDOW_HOURS * 3600
        out: list[list[float]] = []
        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            try:
                when, mb = float(item[0]), int(item[1])
            except (TypeError, ValueError):
                continue
            if when >= cutoff:
                out.append([when, mb])
        return sorted(out)

    def record_disk_sample(self, *, free_mb: int | None = None, at: float | None = None) -> bool:
        """健康检查每轮记一个点：只有"剩多少"判断不了"还剩多久"。

        这台机器 09-26 剩 769MB、10-01 剩 1818MB——轮转一来数字就会跳回去。我据此
        写过"每天涨 176MB、约三天写满"，那是把两个点连成一条直线的结果。
        """
        path = self._disk_history_path()
        now = time.time() if at is None else float(at)
        if free_mb is None:
            free_mb = self.disk_free_mb()
        samples = self._disk_samples(now)
        samples.append([round(now), int(free_mb)])
        tmp = Path(str(path) + ".tmp")
        try:
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(samples[-96:]), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            return False
        return True

    def disk_trend(self, *, at: float | None = None) -> tuple[int | None, float | None, float | None]:
        """(净变化 MB, 按这个速度还能撑几天, 这些样本实际跨了几小时)。

        天数只在真的在变少、**而且样本够长**时才给：一个刚被 logrotate 放回 1GB 的盘不该
        被读成倒计时，一小时以内的抖动也不算趋势（两台机器各 13 分钟一轮，两点连线最容易骗人）。
        跨度必须一起返回：2026-10-01 真机只有 9 个点、跨 4.27 小时，渲染层却写着"最近 24h"，
        于是同一行里 -58MB（4 小时）与"约 5.4 天"（把它放大成一天速率）互相反悔。
        """
        now = time.time() if at is None else float(at)
        samples = self._disk_samples(now)
        if len(samples) < 2:
            return None, None, None
        (first_t, first_mb), (last_t, last_mb) = samples[0], samples[-1]
        span = last_t - first_t
        if span < 3600:
            return None, None, None
        delta = int(last_mb - first_mb)
        span_hours = round(span / 3600.0, 1)
        days = None
        if delta < 0 and span >= self.MIN_COUNTDOWN_SPAN_HOURS * 3600:
            # `delta < 0` 与跨度是这里仅有的两道闸门，所以 per_day 必为正，
            # 不需要第三道 `if per_day > 0`（反向验证拆掉它没有任何用例变红，说明它是死条件）。
            per_day = -delta * (86400.0 / span)
            days = round(last_mb / per_day, 1)
        return delta, days, span_hours

    def disk_free_mb(self) -> int:
        return int(shutil.disk_usage(str(self.config.settings.data_path)).free / 1024 / 1024)

    def stats(self) -> dict[str, Any]:
        day_ago = datetime.utcnow() - timedelta(hours=24)
        threshold = as_int(self.config.get("alerts.source_fail_threshold"), 5)
        with session_scope() as session:
            # 行数与"最久等了多久"都数整张表（`backlog_stats`）：以前这两个数是从
            # 最新 200 条那一页里算的，积压越大反而显得越轻。
            pending, oldest_hours = repo.backlog_stats(session)
            total = repo.count_since(session, datetime(1970, 1, 1))
            delivering = repo.sources_that_delivered(session, day_ago)
            failing, blipping = repo.sources_needing_attention(session, threshold=threshold)
        configured = self.configured_sources()
        enabled = {str(s["name"]) for s in configured if s["enabled"]}
        free_mb = self.disk_free_mb()
        delta, days, span_hours = self.disk_trend()
        return {
            "total_articles": total,
            "last_24h": self.count_since(hours=24),
            "pending_processing": pending,
            "llm_enabled": get_llm().enabled,
            # "sources" means *enabled*, which is what every caller here reports:
            # the configured count made a half-disabled setup look healthier.
            "sources": len(enabled),
            "sources_configured": len(configured),
            "sources_delivering": len(delivering & enabled),
            # 两个词、两件事：持续失败的要按名字告诉他，抖一下的只报个数。
            "sources_failing": len(failing),
            "sources_blipping": len(blipping),
            "source_fail_threshold": threshold,
            "sources_failing_detail": [{"name": s.name, "errors": s.error_count or 0,
                                         "error": (s.last_error or "").strip()} for s in failing],
            # 磁盘写满是静默死亡：SQLite 报错、Bot 停止入库，看起来像"今天没新闻"。
            # 但光有"剩多少"会被读错，所以方向、**实际跨度**与天数一起给
            # （跨度是真的跨度，不是标签上写死的 24h；样本不够或不够长就留 None）。
            "disk_free_mb": free_mb,
            "disk_delta_mb": delta,
            "disk_span_hours": span_hours,
            "disk_days_left": days,
            # `/stats` 会说"积压最久的一条等了多久"，所以这里不能只带个数：
            # 只加一个没人读的数字，等于制造下一条"显示了但没接线"的缺陷。
            "processing_oldest_hours": oldest_hours,
        }

    # ------------------------------------------------------------- settings
    def default_min_score(self, *, min_floor: float | None = None) -> float:
        value = float(self.config.settings.min_article_score or 0)
        return value if min_floor is None else max(min_floor, value)

    def breaking_quota(self, chat_id: int) -> dict[str, Any]:
        """今天（读者的当地日）突发名额的用量，口径与 `can_send_breaking` 完全一致。

        面板上原来只写"突发新闻：开"，而 2026-10-01 的真相是当天 5/5 已经用完：
        夜里积压的突发在 07:03 一次放光名额，#1813 之后只拿到
        `daily cap reached (5/5)`。一个正在生效的上限如果不在他看的那一屏上，
        "开"就是一个会让人等电话的假状态（和 §11 v1.63 那根 90 分按钮同一族）。
        """
        from app.services.digest import breaking_limits  # 延迟导入：digest 反过来依赖本模块

        config = self.config
        limits = breaking_limits(config)
        with session_scope() as session:
            # 只读：`get_user` 而不是 `get_or_create_user`——面板不该因为有人看了一眼设置
            # 就往订阅表里插一行。没有账本行就没有推送过，而 `pushes_since(user=None)`
            # 的含义是"数所有人"，绝不能当成 0 传进去（v1.50 那个全局标志的同一个坑）。
            user = repo.get_user(session, chat_id)
            if user is None:
                return {"used": 0, "limit": limits["max_per_day"],
                        "cooldown_minutes": limits["cooldown_minutes"],
                        "zone": config.settings.timezone}
            zone = user.timezone or config.settings.timezone
            used = repo.pushes_since(session, user=user, kind="breaking",
                                     since=local_day_start(zone, config=config))
        return {"used": int(used), "limit": limits["max_per_day"],
                "cooldown_minutes": limits["cooldown_minutes"], "zone": zone}

    def user_for(self, chat_id: int, *, display_name: str | None = None,
                 user_id: int | None = None, tz: str | None = None) -> dict[str, Any]:
        with session_scope() as session:
            user = repo.get_or_create_user(
                session, chat_id, user_id=user_id, display_name=display_name,
                timezone=tz or self.config.settings.timezone,
            )
            session.commit()
            return self._user_dict(user, repo.interests_of(session, user))

    def update_user(self, chat_id: int, **fields: Any) -> dict[str, Any]:
        with session_scope() as session:
            user = repo.get_user(session, chat_id) or repo.get_or_create_user(
                session, chat_id, timezone=self.config.settings.timezone
            )
            repo.update_user(session, user, **fields)
            session.commit()
            return self._user_dict(user, repo.interests_of(session, user))

    def set_interests(self, chat_id: int, interests: list[dict[str, Any]], *, replace: bool = True) -> dict[str, Any]:
        with session_scope() as session:
            user = repo.get_or_create_user(session, chat_id, timezone=self.config.settings.timezone)
            added = repo.set_interests(session, user, interests, replace=replace)
            session.commit()
            payload = self._user_dict(user, repo.interests_of(session, user))
            payload["added"] = added
            return payload

    def interests(self, chat_id: int) -> list[dict[str, Any]]:
        with session_scope() as session:
            user = repo.get_user(session, chat_id)
            if user is None:
                return []
            return [
                {"type": i.type, "value": i.value, "weight": i.weight}
                for i in repo.interests_of(session, user)
            ]

    def _user_dict(self, user: User, interests: list[Any]) -> dict[str, Any]:
        return {
            "chat_id": user.telegram_chat_id,
            "timezone": user.timezone,
            "language": user.language,
            "daily_enabled": user.daily_enabled,
            "daily_time": user.daily_time,
            "evening_enabled": user.evening_enabled,
            "evening_time": user.evening_time,
            "breaking_enabled": user.breaking_enabled,
            "breaking_threshold": user.breaking_threshold,
            "min_score": user.min_score,
            "paused": user.paused,
            "interests": [
                {"type": i.type, "value": i.value, "weight": i.weight} for i in interests
            ],
        }

    # ------------------------------------------------------------- pipeline
    async def process_now(self, *, limit: int | None = None, llm: LLMService | None = None) -> Any:
        from app.processing.pipeline import process_pending

        with session_scope() as session:
            interests = self.interest_payload()
            stats = await process_pending(session, config=self.config, llm=llm, limit=limit,
                                          interests=interests)
            session.commit()
            return stats

    def interest_payload(self) -> list[dict[str, Any]]:
        """Merge every registered user's interests into one scoring hint list."""
        with session_scope() as session:
            payload: list[dict[str, Any]] = []
            for user in session.query(User).all():
                for interest in repo.interests_of(session, user):
                    payload.append({"type": interest.type, "value": interest.value,
                                    "weight": interest.weight})
            return payload

    def mark_sent(self, article_ids: Iterable[int], *, breaking: bool = False) -> None:
        ids = [int(i) for i in article_ids]
        if not ids:
            return
        with session_scope() as session:
            repo.mark_sent(session, ids, breaking=breaking)
            session.commit()

    def archive_old(self) -> int:
        days = int(self.config.get("app.retention_days", 0) or 0)
        with session_scope() as session:
            moved = repo.archive_older_than(session, days)
            session.commit()
            return moved


_service: NewsService | None = None


def get_news_service() -> NewsService:
    global _service
    if _service is None:
        _service = NewsService()
    return _service
