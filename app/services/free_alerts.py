"""Proactive "something just went free" alerts.

/免费 answers when asked; this pushes. It reuses the breaking-news discipline
(per-user pause, cooldown, daily cap, PushLog) because an unpaced promo feed is
just spam, and nothing is written off before the message is delivered: the
article marker and the gateway-model ledger are both set after Telegram accepts
the message, so a refused or undeliverable push stays re-triable.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Sequence

from app.config import AppConfig, as_float, as_int, get_config, quiet_window
from app.database import repository as repo
from app.database import session_scope
from app.logging_setup import get_logger
from app.services import format as fmt
from app.services.news import NewsService

log = get_logger("app")

KIND = "free_offer"
HEADING = "🆓 刚发现的限免"
HEADING_MODELS_ONLY = "🆓 <b>网关新出现的免费模型</b>"


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

        # Asked once per round, before any reader is served: the gateway ledger is one
        # per install, so every chat alerted in this round sees the same ⚡ block and it
        # is written off once, after a delivery, rather than once per chat.
        models, live_block = await self._pending_free_models()
        delivered = 0
        for chat_id in chat_ids:
            # A chat we are about to push to is a subscriber by definition, and the
            # per-user ledger below only works if the row exists.
            user_id = self._ensure_user(chat_id)
            with session_scope() as session:
                rows = repo.unannounced_free_offers(
                    session, limit=limit, min_confidence=min_confidence,
                    within_days=within_days, user_id=user_id,
                    require_title_subject=bool(self.cfg.get("require_title_subject", True)))
                ids = [row.id for row in rows]
            if not ids and not live_block:
                # Nothing this reader has not already heard - not a withheld alert,
                # just nothing to say. Logging a skip here used to bury the real ones.
                continue
            ok, reason = self._may_send(chat_id)
            if not ok:
                log.info("free-offer alert withheld for %s (%d offer(s) + %d new model(s)): %s",
                         chat_id, len(ids), len(models), reason)
                continue
            if not ids:
                # Only the gateway changed. Saying "没有采到限免公告" would be a lie
                # about the news when this reader was already sent news offers today.
                text = fmt.clip(f"{HEADING_MODELS_ONLY}\n\n{live_block}")
            else:
                views = [view for view in (self.news.by_id(article_id) for article_id in ids)
                         if view is not None]
                text = fmt.free_offer_list(
                    views, config=self.config, days=within_days,
                    live=live_block, heading=HEADING,
                    note="自动监控到新出现的免费额度，详情用 /免费 随时回看。")
            if not await self._sender.send(chat_id, text):
                continue
            delivered += 1
            with session_scope() as session:
                if ids:
                    for article_id in ids:
                        repo.record_push(session, user_id=user_id, kind=KIND,
                                         article_id=article_id)
                else:
                    # A models-only push still has to be counted, or the cooldown and
                    # the daily cap pace only the news half of this feed.
                    repo.record_push(session, user_id=user_id, kind=KIND)
                repo.mark_free_offers_sent(session, ids)
                session.commit()
            log.info("free-offer alert -> %s: %d offer(s), %d new model(s)",
                     chat_id, len(ids), len(models))
        if delivered and models:
            self._mark_models_announced(models)
        return delivered

    async def _pending_free_models(self) -> tuple[list[Any], str]:
        """(free models never announced here, formatted block) - writes nothing.

        The marking used to happen while building the message, so a Telegram failure
        (chat blocked the bot, network gave up, API error) burned the model id for
        good: `unannounced()` reads the same ledger, and the article half of this
        path had already learned to wait for a successful send.
        """
        if not bool(self.cfg.get("new_models", True)):
            return [], ""
        from app.services.free_models import get_free_model_watcher

        watcher = get_free_model_watcher(self.config)
        # `snapshot()` swallows its own failures and answers [] - an unreachable
        # gateway costs the ⚡ block, not the round.
        models = await watcher.snapshot()
        fresh = watcher.unannounced(models)
        if not fresh:
            return [], ""
        return fresh, fmt.free_models_section(fresh, config=self.config,
                                              limit=as_int(self.cfg.get("max_models"), 5),
                                              source=watcher.source_name)

    def _mark_models_announced(self, models: Sequence[Any]) -> None:
        """Write off the ⚡ block after delivery.

        One ledger for the install, so a chat we failed to reach is not retried for
        *models* the way the per-user offer ledger retries an article - today there is
        one subscriber, and the honest statement of the limit beats a state file per
        chat.
        """
        from app.services.free_models import get_free_model_watcher

        get_free_model_watcher(self.config).mark_announced([model.id for model in models])

    def _ensure_user(self, chat_id: int) -> int | None:
        if not chat_id:
            return None
        with session_scope() as session:
            user = repo.get_or_create_user(session, chat_id)
            session.commit()
            return user.id

    def _may_send(self, chat_id: int) -> tuple[bool, str]:
        cooldown = as_int(self.cfg.get("cooldown_minutes"), 90)
        max_per_day = as_int(self.cfg.get("max_per_day"), 4)
        with session_scope() as session:
            # Per reader from the first lookup: a missing row used to turn the cap
            # and the cooldown into a count over everybody's pushes.
            user = repo.ledger_user(session, chat_id, timezone=self.config.settings.timezone)
            if user is not None and user.paused:
                return False, "user paused"
            # A promo at 05:00 is the least defensible ping of the day, and nothing is
            # lost by waiting: neither the offer rows nor the gateway ledger is written
            # until a delivery succeeds, so the next round after the window simply
            # offers the same thing again.
            quiet = quiet_window(self.config, getattr(user, "timezone", None))
            if quiet:
                return False, quiet
            day_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            if repo.pushes_since(session, user=user, kind=KIND, since=day_start) >= max_per_day:
                return False, f"daily cap {max_per_day} reached"
            last = repo.last_push_of(session, user=user, kind=KIND)
            if last is not None and last.created_at > datetime.utcnow() - timedelta(minutes=cooldown):
                return False, f"cooldown {cooldown}m"
        return True, ""
