"""Proactive "something just went free" alerts.

/免费 answers when asked; this pushes. It reuses the breaking-news discipline
(per-user pause, cooldown, daily cap, PushLog) because an unpaced promo feed is
just spam, and it only ever announces a row once - the marker lives on the
article, not in a timestamp we would have to guess at.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Sequence

from app.config import AppConfig, as_float, as_int, get_config
from app.database import repository as repo
from app.database import session_scope
from app.logging_setup import get_logger
from app.services import format as fmt
from app.services.news import NewsService

log = get_logger("app")

KIND = "free_offer"


class FreeAlertService:
    def __init__(self, config: AppConfig | None = None, *, sender: Any = None,
                 news: NewsService | None = None) -> None:
        self.config = config or get_config()
        self._sender = sender
        self.news = news or NewsService(self.config)

    @property
    def cfg(self) -> dict[str, Any]:
        return dict(self.config.get("free.alert", {}) or {})

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True)) and self._sender is not None

    async def run(self, chat_ids: Sequence[int]) -> int:
        """Announce each subscriber's still-unannounced offers. Returns messages sent."""
        if not self.enabled or not chat_ids:
            return 0
        limit = as_int(self.cfg.get("max_items"), 3)
        min_confidence = as_float(self.cfg.get("min_confidence"), 0.6)
        within_days = as_int(self.cfg.get("within_days"), 7)

        delivered = 0
        for chat_id in chat_ids:
            # A chat we are about to push to is a subscriber by definition, and the
            # per-user ledger below only works if the row exists.
            user_id = self._ensure_user(chat_id)
            ok, reason = self._may_send(chat_id)
            if not ok:
                log.info("free-offer alert skipped for %s: %s", chat_id, reason)
                continue
            with session_scope() as session:
                rows = repo.unannounced_free_offers(
                    session, limit=limit, min_confidence=min_confidence,
                    within_days=within_days, user_id=user_id,
                    require_title_subject=bool(self.cfg.get("require_title_subject", True)))
                ids = [row.id for row in rows]
            if not ids:
                continue
            views = [view for view in (self.news.by_id(article_id) for article_id in ids)
                     if view is not None]
            # The live-model block is a global ledger (one state file, not one per
            # subscriber), so it is offered to whoever happens to be alerted.
            new_models = await self._new_free_models()
            text = fmt.free_offer_list(
                views, config=self.config, days=within_days,
                live=new_models, heading="🆓 刚发现的限免",
                note="自动监控到新出现的免费额度，详情用 /免费 随时回看。")
            if not await self._sender.send(chat_id, text):
                continue
            delivered += 1
            with session_scope() as session:
                for article_id in ids:
                    repo.record_push(session, user_id=user_id, kind=KIND,
                                     article_id=article_id)
                repo.mark_free_offers_sent(session, ids)
                session.commit()
            log.info("free-offer alert -> %s: %d offer(s)", chat_id, len(ids))
        return delivered

    def _ensure_user(self, chat_id: int) -> int | None:
        if not chat_id:
            return None
        with session_scope() as session:
            user = repo.get_or_create_user(session, chat_id)
            session.commit()
            return user.id

    async def _new_free_models(self) -> str:
        """Live gateway models we have never announced, as a formatted block."""
        if not bool(self.cfg.get("new_models", True)):
            return ""
        from app.services.free_models import get_free_model_watcher

        watcher = get_free_model_watcher(self.config)
        try:
            models = await watcher.snapshot()
        except Exception as exc:  # noqa: BLE001
            log.info("free-model watch unavailable: %s", exc)
            return ""
        fresh = watcher.unannounced(models)
        if not fresh:
            return ""
        watcher.mark_announced([model.id for model in fresh])
        return fmt.free_models_section(fresh, config=self.config,
                                       limit=as_int(self.cfg.get("max_models"), 5),
                                       source=watcher.source_name)

    def _may_send(self, chat_id: int) -> tuple[bool, str]:
        cooldown = as_int(self.cfg.get("cooldown_minutes"), 90)
        max_per_day = as_int(self.cfg.get("max_per_day"), 4)
        with session_scope() as session:
            user = repo.get_user(session, chat_id) if chat_id else None
            if user is not None and user.paused:
                return False, "user paused"
            day_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            if repo.pushes_since(session, user=user, kind=KIND, since=day_start) >= max_per_day:
                return False, f"daily cap {max_per_day} reached"
            last = repo.last_push_of(session, user=user, kind=KIND)
            if last is not None and last.created_at > datetime.utcnow() - timedelta(minutes=cooldown):
                return False, f"cooldown {cooldown}m"
        return True, ""
