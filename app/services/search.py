"""Search + the conversational news agent (design doc sections 14.2, 26, 31)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

from app.config import AppConfig, get_config
from app.database import repository as repo
from app.database.database import session_scope
from app.database.models import Article
from app.logging_setup import get_logger
from app.processing.normalize import keyword_hits
from app.services import format as F
from app.services.llm import LLMService, get_llm
from app.services.news import ArticleView, NewsService, get_news_service

log = get_logger("app")

# CJK interrogatives that carry no search meaning.
NOISE = [
    "最近", "今天", "昨日", "昨天", "有什么", "有没有", "值得关注的", "值得关注", "新的",
    "哪些", "什么", "请问", "帮我", "看看", "一下", "关于", "the", "latest", "recent",
    "what", "is", "are", "any", "news", "about", "of",
]


@dataclass
class AgentAnswer:
    text: str
    used_ids: list[int] = field(default_factory=list)
    fallback: bool = False
    intent: str = "search"
    query: str = ""


class SearchService:
    def __init__(self, config: AppConfig | None = None, news: NewsService | None = None,
                 llm: LLMService | None = None) -> None:
        self.config = config or get_config()
        self.news = news or get_news_service()
        # Injected by the app wiring and by tests; resolved lazily otherwise.
        self.llm = llm

    def _llm(self, override: LLMService | None = None) -> LLMService:
        return override or self.llm or get_llm()

    def search(self, query: str, *, days: int = 30, limit: int = 10,
               min_score: float = 0) -> list[ArticleView]:
        terms = clean_query(query)
        since = datetime.utcnow() - timedelta(days=max(1, days))
        with session_scope() as session:
            # Try the whole phrase first; if nothing matches, fall back to the
            # strongest single token so /search never comes back empty by accident.
            for candidate in _candidates(terms, query):
                articles = repo.query_articles(
                    session, since=since, search=candidate, limit=limit * 2,
                    min_score=min_score, order_by_score=False, require_processed=False,
                )
                if articles:
                    return [r for r in _views(session, articles)][:limit]
            return []

    def counts_by_source(self, *, days: int = 7) -> list[tuple[str, int]]:
        since = datetime.utcnow() - timedelta(days=days)
        with session_scope() as session:
            rows = session.execute(
                select(Article.source_name, func.count(Article.id))
                .where(Article.published_at >= since)
                .group_by(Article.source_name)
                .order_by(func.count(Article.id).desc())
            ).all()
        return [(str(name), int(count)) for name, count in rows]

    # ------------------------------------------------------- agent behaviour
    async def detect_intent(self, text: str, recent: list[ArticleView] | None = None,
                            llm: LLMService | None = None) -> dict[str, Any]:
        service = self._llm(llm)
        payload = [{"title": r.title, "final_score": r.final_score} for r in (recent or [])]
        if service.enabled:
            try:
                return await service.detect_intent(text, payload)
            except Exception as exc:
                log.info("intent detection fell back to rules: %s", exc)
        return rule_intent(text, payload)

    async def answer(self, question: str, *, chat_id: int | None = None,
                     recent: list[ArticleView] | None = None,
                     llm: LLMService | None = None) -> AgentAnswer:
        """The Phase-4 experience, driven from the local database."""
        service = self._llm(llm)
        lookback = int(self.config.get("bot.chat_lookback_days", 14))
        decision = await self.detect_intent(question, recent, service)
        intent = decision.get("intent", "search")
        days = int(decision.get("days") or lookback)

        if intent == "settings":
            return AgentAnswer(
                "请使用 /settings 查看当前设置，或用 /setinterest 描述你的兴趣，例如：\n"
                "<code>我主要关注 AI Agent、开源模型和 NVIDIA</code>",
                intent="settings", fallback=True,
            )
        if intent == "sources":
            return AgentAnswer(F.status_line(self.news.stats()) , intent="sources", fallback=True)
        if intent == "help":
            return AgentAnswer(F.help_text(), intent="help", fallback=True)

        if intent == "summarize":
            index = _index_from(decision.get("index"), question, recent)
            if index:
                item = self.news.nth_of_latest(index)
                if item:
                    detailed = await self.deep_summary(item.id, llm=service)
                    if detailed:
                        return AgentAnswer(detailed, [item.id], intent="summarize", fallback=True)
            return AgentAnswer("我不确定你指的是哪一条，先发 /news，再用 /summary 编号。",
                               intent="summarize", fallback=True)

        query = decision.get("query") or question
        results = self.search(query, days=days, limit=int(self.config.get("bot.chat_context_size", 12)))
        if intent == "latest" and not results:
            results = self.news.latest(limit=10, min_score=0, hours=24 * days)
        results = await self.news.ensure_chinese(results)
        if not results:
            return AgentAnswer(
                f"数据库里最近 {days} 天没有找到与 “{F.plain(query, 60)}” 相关的新闻。\n"
                "可以试试更短的关键词（如 <code>Agent</code>、<code>Claude</code>），"
                "或等下一轮采集后再问。",
                fallback=True, intent=intent, query=query,
            )
        if not service.enabled:
            # `display_line` on purpose: `ensure_chinese` above just paid for the
            # Chinese, and this used to render `a.summary or a.title` - so the
            # answer he actually gets (no model configured) listed English headlines
            # with a Chinese version sitting in the database.
            lines = [f"未配置 AI 模型，以下是数据库里与 “{F.esc(F.plain(query, 40))}” 相关的 {len(results)} 条新闻："]
            lines += [
                f"{i + 1}. {F.link(F.shorten(a.display_line, 120), a.url)} <i>({F.esc(a.source_name)} · "
                f"{F.short_date(a.published_at, self.config.settings.timezone)} · {a.final_score:.0f})</i>"
                for i, a in enumerate(results[:8])
            ]
            return AgentAnswer(F.clip("\n".join(lines)), [a.id for a in results],
                               fallback=True, intent=intent, query=query)

        try:
            text = await service.answer_question(
                question,
                [a.to_dict() for a in results],
                now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            )
        except Exception as exc:
            log.warning("agent answer fell back to list: %s", exc)
            lines = [f"AI 暂时不可用，先给你 {len(results)} 条相关新闻："]
            lines += [f"{i + 1}. {F.link(F.shorten(a.display_line, 120), a.url)}" for i, a in enumerate(results[:8])]
            return AgentAnswer(F.clip("\n".join(lines)), [a.id for a in results],
                               fallback=True, intent=intent, query=query)
        return AgentAnswer(F.clip(text, 3600), [a.id for a in results], intent=intent, query=query)

    async def deep_summary(self, article_id: int, *, llm: LLMService | None = None) -> str | None:
        """Layer-3 strong-model analysis (design doc section 23)."""
        service = self._llm(llm)
        item = self.news.by_id(article_id)
        if item is None:
            return None
        tz_name = self.config.settings.timezone
        await self.news.ensure_chinese([item])
        if not service.enabled:
            return F.clip(F.article_card(item, config=self.config, tz_name=tz_name)
                          + "\n\n<i>未配置 LLM_API_KEY，以上为规则摘要。</i>")
        related: list[dict[str, Any]] = []
        if item.event_id:
            with session_scope() as session:
                related = [
                    {"source_name": a.source_name, "title": a.title}
                    for a in repo.event_members(session, item.event_id)
                    if a.id != item.id
                ]
        try:
            deep = await service.deep_analyze(item.to_dict() | {"content": item.content}, related)
        except Exception as exc:
            log.info("deep analysis unavailable for #%s: %s", article_id, exc)
            return F.clip(
                F.article_card(item, config=self.config, tz_name=tz_name)
                + f"\n\n<i>AI 深度分析暂时失败（{F.esc(type(exc).__name__)}），以上为常规摘要。</i>"
            )
        return F.clip(F.article_card(item, config=self.config, tz_name=tz_name, deep=deep))


# ------------------------------------------------------------------ helpers
def clean_query(query: str) -> list[str]:
    text = (query or "").strip()
    text = re.sub(r"[？?！。，、,.]+", " ", text)
    for noise in NOISE:
        text = re.sub(re.escape(noise), " ", text, flags=re.I)
    pieces = [p for p in re.split(r"\s+", text) if len(p.strip()) >= 2]
    out: list[str] = []
    for piece in pieces:
        piece = piece.strip()
        if piece and piece.lower() not in [o.lower() for o in out]:
            out.append(piece)
    return out


def _candidates(terms: list[str], raw: str) -> list[str]:
    out: list[str] = []
    phrase = F.plain(raw, 80).strip()
    if phrase:
        out.append(phrase)
    for term in terms:
        if term not in out:
            out.append(term)
    # Chinese queries rarely match with spaces between terms.
    joined = "".join(terms[:3])
    if joined and joined not in out:
        out.append(joined)
    return [o for o in out if o][:5]


# "关注" is what he types in both directions: "我想关注 NVIDIA" (a setting) and
# "最近 AI Agent 有什么值得关注的？" (a question). Triggering on the word alone sent
# the question to the settings reply, which is a dead end in a rule-mode deployment
# where nothing else interprets the sentence. A question marker therefore wins.
_QUESTION_MARKERS = ("？", "?", "什么", "哪些", "多少", "怎么", "如何", "为什么", "有没有",
                     "最近", "今天", "昨天", "本周", "这周", "值得")
_CONFIG_MARKERS = ("设置", "提醒时间", "推送时间", "改成", "改为", "设为", "调整", "暂停",
                   "恢复推送", "兴趣", "interest", "notify")


def rule_intent(text: str, recent: list[dict[str, Any]]) -> dict[str, Any]:
    lowered = (text or "").lower()
    index = None
    match = re.search(r"第\s*([0-9一二三四五六七八九十]+)\s*[条个]?", text or "")
    if match:
        index = _to_int(match.group(1))
    if any(k in lowered for k in ("summary", "详细", "深度", "分析")) and index:
        return {"intent": "summarize", "query": "", "days": 14, "index": index}
    asks = any(k in text for k in _QUESTION_MARKERS)
    if any(k in lowered for k in _CONFIG_MARKERS) or ("关注" in text and not asks):
        return {"intent": "settings", "query": "", "days": 14, "index": None}
    if any(k in lowered for k in ("来源", "source", "数据源", "状态")):
        return {"intent": "sources", "query": "", "days": 14, "index": None}
    if any(k in lowered for k in ("帮助", "help", "怎么用", "能做什么")):
        return {"intent": "help", "query": "", "days": 14, "index": None}
    terms = clean_query(text)
    days = 14
    if any(k in lowered for k in ("今天", "today", "今日")):
        days = 1
    elif any(k in lowered for k in ("昨天", "yesterday")):
        days = 2
    elif any(k in lowered for k in ("本周", "这周", "一周", "7 天")):
        days = 7
    elif any(k in lowered for k in ("最近", "latest", "recent")):
        days = 3
    return {"intent": "search" if terms else "latest", "query": " ".join(terms) or text.strip(),
            "days": days, "index": index}


def _index_from(value: Any, text: str, recent: list[ArticleView] | None) -> int | None:
    if isinstance(value, int) and value > 0:
        return value
    match = re.search(r"第\s*([0-9一二三四五六七八九十]+)\s*[条个]?", text or "")
    if match:
        return _to_int(match.group(1))
    return None


def _to_int(value: str) -> int | None:
    digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    text = str(value or "").strip()
    if text.isdigit():
        return int(text)
    if text in digits:
        return digits[text]
    if text.startswith("十") and len(text) == 2 and text[1] in digits:
        return 10 + digits[text[1]]
    return None


def _views(session, articles) -> list[ArticleView]:
    from app.services.news import _view

    return [_view(session, a) for a in articles]


_service: SearchService | None = None


def get_search_service() -> SearchService:
    global _service
    if _service is None:
        _service = SearchService()
    return _service
