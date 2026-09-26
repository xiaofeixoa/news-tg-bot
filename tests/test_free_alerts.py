"""Tests for the proactive "something just went free" push."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta

import pytest

from sqlalchemy import select

from app.database.models import PushLog
from app.database import repository as repo
from app.database import session_scope
from app.services.free_alerts import KIND, FreeAlertService
from app.services.free_models import FreeModel, FreeModelWatcher


class FakeSender:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[tuple[int, str]] = []

    async def send(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        return self.ok


def seed_offer(session, *, title="某 agent 限免一周", url="https://linux.do/t/1",
               confidence=0.8, tool="Qoder"):
    from app.processing.normalize import build_article

    data = build_article(title=title, url=url, source_name="Linux.do 福利分类",
                         source_type="rss", content=title + " 详情", quality="C")
    article = repo.save_article(session, data)
    article.is_free_offer = True
    article.free_offer_tool = tool
    article.free_offer = {"tool": tool, "kind": "编程 Agent/IDE", "signals": ["限免"],
                          "models": [], "confidence": confidence}
    article.is_processed = True
    article.final_score = 70
    session.commit()
    return article.id


def alert_config(config, tmp_path, **overrides):
    alert = {"enabled": True, "within_days": 7, "min_confidence": 0.6, "max_items": 3,
             "cooldown_minutes": 90, "max_per_day": 4, "new_models": False,
             **overrides}
    return dataclasses.replace(
        config, raw={**config.raw, "free": {**config.raw.get("free", {}),
                                           "alert": alert,
                                           "models": {"enabled": False,
                                                      "state_file": str(tmp_path / "m.json")}}})


async def test_nothing_to_announce_sends_nothing(config, session, tmp_path):
    service = FreeAlertService(alert_config(config, tmp_path), sender=FakeSender())
    assert await service.run([111]) == 0


async def test_new_offer_is_pushed_once_and_marked(config, session, tmp_path):
    article_id = seed_offer(session)
    sender = FakeSender()
    service = FreeAlertService(alert_config(config, tmp_path), sender=sender)

    assert await service.run([111]) == 1
    assert "限免" in sender.sent[0][1] and "刚发现的限免" in sender.sent[0][1]
    with session_scope() as s:
        assert s.get(repo.Article, article_id).free_offer_sent_at is not None
        assert repo.pushes_since(s, user=None, kind=KIND,
                                 since=datetime.utcnow() - timedelta(minutes=5)) == 1
    # the same offer must never be announced twice
    assert await service.run([111]) == 0
    assert len(sender.sent) == 1


async def test_low_confidence_offers_stay_quiet(config, session, tmp_path):
    seed_offer(session, url="https://linux.do/t/2", confidence=0.3, title="可能免费吧")
    sender = FakeSender()
    service = FreeAlertService(alert_config(config, tmp_path), sender=sender)
    assert await service.run([111]) == 0
    assert sender.sent == []


async def test_a_failed_send_leaves_the_offer_retriable(config, session, tmp_path):
    article_id = seed_offer(session, url="https://linux.do/t/3")
    service = FreeAlertService(alert_config(config, tmp_path), sender=FakeSender(ok=False))
    assert await service.run([111]) == 0
    with session_scope() as s:
        assert s.get(repo.Article, article_id).free_offer_sent_at is None


async def test_cooldown_and_daily_cap_are_respected(config, session, tmp_path):
    seed_offer(session, url="https://linux.do/t/4")
    service = FreeAlertService(alert_config(config, tmp_path, cooldown_minutes=90),
                               sender=FakeSender())
    ok, _ = service._may_send(111)
    assert ok
    with session_scope() as s:
        repo.record_push(s, user=None, kind=KIND)
        s.commit()
    ok, reason = service._may_send(111)
    assert not ok and "cooldown" in reason


async def test_paused_user_is_not_disturbed(config, session, tmp_path):
    seed_offer(session, url="https://linux.do/t/5")
    with session_scope() as s:
        user = repo.get_or_create_user(s, 111)
        repo.update_user(s, user, paused=True)
        s.commit()
    sender = FakeSender()
    service = FreeAlertService(alert_config(config, tmp_path), sender=sender)
    assert await service.run([111]) == 0
    assert sender.sent == []


async def test_disabled_alerts_never_send(config, session, tmp_path):
    seed_offer(session, url="https://linux.do/t/6")
    sender = FakeSender()
    service = FreeAlertService(alert_config(config, tmp_path, enabled=False), sender=sender)
    assert await service.run([111]) == 0
    assert service.enabled is False


async def test_without_a_sender_the_service_is_inert(config, tmp_path):
    service = FreeAlertService(alert_config(config, tmp_path), sender=None)
    assert service.enabled is False
    assert await service.run([111]) == 0


# ------------------------------------------------------- live model ledger
def _model(mid: str) -> FreeModel:
    return FreeModel(id=mid, name=mid, provider="x", url=f"https://openrouter.ai/{mid}")


async def test_first_snapshot_seeds_the_ledger_instead_of_crying_wolf(config, tmp_path,
                                                                     monkeypatch):
    payload = {"data": [
        {"id": "a/one:free", "name": "One", "created": 1_750_000_000,
         "context_length": 8192, "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "a/two:free", "name": "Two", "created": 1_751_000_000,
         "context_length": 8192, "pricing": {"prompt": "0", "completion": "0"}},
    ]}

    async def fake_fetch(self):
        return payload

    monkeypatch.setattr(FreeModelWatcher, "_fetch", fake_fetch)
    models_cfg = {"enabled": True, "state_file": str(tmp_path / "m.json"),
                  "url": "https://openrouter.ai/api/v1/models"}
    raw = {**config.raw, "free": {"models": models_cfg}}
    cfg = dataclasses.replace(config, raw=raw)
    watcher = FreeModelWatcher(cfg)

    models = await watcher.snapshot()
    assert len(models) == 2
    # nothing has been announced yet, and the first pass must not claim 2 "new"
    assert watcher.unannounced(models) == []

    payload["data"].append({"id": "a/three:free", "name": "Three", "created": 1_760_000_000,
                            "context_length": 8192, "pricing": {"prompt": "0", "completion": "0"}})
    models = await watcher.snapshot(force=True)
    fresh = [m.id for m in watcher.unannounced(models)]
    assert fresh == ["a/three:free"]
    watcher.mark_announced(fresh)
    assert watcher.unannounced(models) == []


async def test_every_subscriber_gets_the_offer(config, session, tmp_path):
    """A global "already sent" flag used to silence the second chat forever."""
    article_id = seed_offer(session, url="https://linux.do/t/7")
    with session_scope() as s:
        first = repo.get_or_create_user(s, 111)
        second = repo.get_or_create_user(s, 222)
        s.commit()
        ids = (first.id, second.id)

    sender = FakeSender()
    service = FreeAlertService(alert_config(config, tmp_path), sender=sender)
    assert await service.run([111, 222]) == 2
    assert [c for c, _ in sender.sent] == [111, 222]

    with session_scope() as s:
        for user_id in ids:
            rows = list(s.scalars(select(PushLog).where(
                PushLog.kind == KIND, PushLog.user_id == user_id)))
            assert [r.article_id for r in rows] == [article_id], user_id
    # neither of them hears about it twice
    assert await service.run([111, 222]) == 0


async def test_a_new_subscriber_still_hears_about_an_older_offer(config, session, tmp_path):
    """/start after the alert went out must not mean missing the promo."""
    seed_offer(session, url="https://linux.do/t/8")
    service = FreeAlertService(alert_config(config, tmp_path), sender=FakeSender())
    assert await service.run([111]) == 1

    sender2 = FakeSender()
    late = FreeAlertService(alert_config(config, tmp_path), sender=sender2, news=service.news)
    assert await late.run([333]) == 1
    assert "限免" in sender2.sent[0][1]


async def test_one_failing_chat_does_not_block_the_others(config, session, tmp_path):
    seed_offer(session, url="https://linux.do/t/9")

    class PartialSender:
        def __init__(self):
            self.sent = []

        async def send(self, chat_id, text, **kwargs):
            self.sent.append(chat_id)
            return chat_id != 111

    sender = PartialSender()
    service = FreeAlertService(alert_config(config, tmp_path), sender=sender)
    assert await service.run([111, 222]) == 1
    with session_scope() as s:
        first = repo.get_or_create_user(s, 111)
        second = repo.get_or_create_user(s, 222)
        s.commit()
        assert repo.pushes_since(s, user=first, kind=KIND,
                                 since=datetime.utcnow() - timedelta(minutes=5)) == 0
        assert repo.pushes_since(s, user=second, kind=KIND,
                                 since=datetime.utcnow() - timedelta(minutes=5)) == 1


async def test_alerts_skip_offers_whose_subject_is_only_in_the_body(config, session, tmp_path):
    """A weekly-digest post that happens to mention a free tool is not alert-worthy."""
    from app.processing.normalize import build_article

    data = build_article(title="社区福利周报第 38 期", url="https://linux.do/t/weekly",
                         source_name="Linux.do 福利分类", source_type="rss",
                         content="本期亮点：Kiro 现在免费开放，注册即可用。", quality="C")
    article = repo.save_article(session, data)
    article.is_free_offer = True
    article.free_offer_tool = "Kiro"
    article.free_offer = {"tool": "Kiro", "kind": "编程 Agent/IDE", "signals": ["免费"],
                          "models": [], "confidence": 0.9, "subject_in_title": False}
    article.is_processed = True
    session.commit()

    sender = FakeSender()
    service = FreeAlertService(alert_config(config, tmp_path), sender=sender)
    assert await service.run([111]) == 0
    assert sender.sent == []

    permissive = FreeAlertService(
        alert_config(config, tmp_path, require_title_subject=False), sender=sender)
    assert await permissive.run([111]) == 1
    assert "推断自正文" in sender.sent[-1][1]


def test_a_configured_zero_is_not_silently_replaced_by_the_default():
    """`int(cfg.get("x", 90) or 90)` turned cooldown_minutes: 0 back into 90."""
    from app.config import as_float, as_int

    assert as_int(0, 90) == 0
    assert as_int(None, 90) == 90
    assert as_int("", 90) == 90
    assert as_int("15", 90) == 15
    assert as_int("nonsense", 90) == 90
    assert as_float(0.0, 0.6) == 0.0
    assert as_float(None, 0.6) == 0.6


async def test_cooldown_zero_really_disables_the_cooldown(config, session, tmp_path):
    seed_offer(session, url="https://linux.do/t/10")
    sender = FakeSender()
    service = FreeAlertService(alert_config(config, tmp_path, cooldown_minutes=0),
                               sender=sender)
    assert await service.run([111]) == 1
    with session_scope() as s:
        user = repo.get_or_create_user(s, 111)
        repo.record_push(s, user=user, kind=KIND)
        s.commit()
    ok, reason = service._may_send(111)
    assert ok, reason
