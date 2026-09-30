"""静默时段：夜里到的大新闻排队，早上自动补发，一条都不丢。

钉住时钟的用例一律用注入的 `at`（或 monkeypatch `_now_utc`），因为套件本身跑在
Asia/Shanghai 而 CI 在 UTC——真实时钟会把同一批测试在北京 23:00 之后集体变红。
"""

from __future__ import annotations

import dataclasses
from datetime import datetime

import pytest
import yaml

from app.config import QUIET_TOKEN, quiet_window
from app.database import repository as repo
from app.database import session_scope
from app.database.models import Article
from app.processing.normalize import build_article
from app.services.digest import DigestService, deferral_worthwhile
from app.services.free_alerts import FreeAlertService

# 北京 04:00 / 07:05 / 22:30 / 12:00，全部以 naive UTC 记账（与库内一致）
NIGHT = datetime(2026, 9, 30, 20, 0)
MORNING = datetime(2026, 9, 30, 23, 5)
BEFORE_OPEN = datetime(2026, 9, 30, 16, 0)      # 北京 00:00，窗口正中
DAYTIME = datetime(2026, 9, 30, 4, 0)          # 北京 12:00
EXACT_OPEN = datetime(2026, 9, 30, 23, 0)      # 北京 07:00，整点即结束


def quiet_config(config, window: str = "23:00-07:00"):
    base = dict(config.raw or {})
    node = dict(base.get("breaking") or {})
    node["quiet_hours"] = window
    base["breaking"] = node
    return dataclasses.replace(config, raw=base)


def _first_hand_row(session, title: str, url: str) -> int:
    from app.services.news import get_news_service

    get_news_service().user_for(111111111)
    stored = repo.save_article(session, build_article(
        title=title, url=url, source_name="OpenAI", source_type="rss",
        content=title + ". The company says the new model improves reasoning. " * 4))
    article = session.get(Article, stored.id)
    article.final_score = 96
    article.source_quality = 95
    article.is_processed = True
    article.category = "AI Models"
    session.commit()
    return int(stored.id)


def _offer_row(session, title: str, url: str) -> int:
    article = repo.save_article(session, build_article(
        title=title, url=url, source_name="Linux.do 福利分类", source_type="rss",
        content=title + " 详情", quality="C"))
    article.is_free_offer = True
    article.free_offer_tool = "Qoder"
    article.free_offer = {"tool": "Qoder", "kind": "编程 Agent/IDE", "signals": ["限免"],
                          "models": [], "confidence": 0.8}
    article.is_processed = True
    article.final_score = 70
    session.commit()
    return int(article.id)


class Sender:
    def __init__(self) -> None:
        self.delivered: list[int] = []
        self.texts: list[str] = []

    async def send_digest(self, chat_id, digest):
        self.delivered.extend(digest.article_ids)
        self.texts.extend(digest.messages)
        return len(digest.messages)


class Chat:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        return True


# ---------------------------------------------------------------- the clock rule
def test_the_window_belongs_to_the_reader_not_the_server(config):
    cfg = quiet_config(config)
    assert quiet_window(cfg, "Asia/Shanghai", at=NIGHT), "同一行日志的北京时间 04:00 该静默"
    assert quiet_window(cfg, "UTC", at=NIGHT) == "", "同一瞬间在 UTC 是 20:00，不该静默"
    assert quiet_window(cfg, "America/Los_Angeles", at=NIGHT) == "", "太平洋时间 13:00 更不该静默"


def test_the_edges_of_the_window_are_exclusive_at_the_end(config):
    cfg = quiet_config(config)
    assert quiet_window(cfg, "Asia/Shanghai", at=BEFORE_OPEN), "北京 00:00 在窗口内"
    assert quiet_window(cfg, "Asia/Shanghai", at=DAYTIME) == "", "中午不该静默"
    assert quiet_window(cfg, "Asia/Shanghai", at=EXACT_OPEN) == "", "07:00 整点就是放行时刻"
    assert quiet_window(cfg, "Asia/Shanghai", at=NIGHT).endswith("07:00 之后自动补发）"), \
        "被挡下的理由要说清什么时候会再发"


@pytest.mark.parametrize("window", ["", "off", "none", "false", "23:00", "25:00-07:00",
                                   "23:00-23:00", "abc-de", "23:00 - 07:00x"])
def test_a_window_that_is_not_a_window_does_not_silence_anything(config, window):
    """写错配置不该变成"从此再也没有突发"——宁可照发并在日志里说一次。"""
    cfg = quiet_config(config, window)
    assert quiet_window(cfg, "Asia/Shanghai", at=NIGHT) == ""


def test_the_shipped_default_is_an_overnight_window(config):
    """conftest 把套件钉成 off，所以这里直接读仓库里那份 YAML。"""
    path = __import__("pathlib").Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))["breaking"]["quiet_hours"]
    assert raw == "23:00-07:00", f"线上默认窗口变了却没改这条测试：{raw}"
    cfg = quiet_config(config, raw)
    assert quiet_window(cfg, "Asia/Shanghai", at=NIGHT)
    assert quiet_window(cfg, "Asia/Shanghai", at=DAYTIME) == ""


def test_the_settings_text_states_the_quiet_window(config):
    from app.processing import breaking

    on = breaking.describe(quiet_config(config), ai_enabled=False)
    assert "23:00-07:00 静默" in on and "自动补发" in on, on
    assert "静默" not in breaking.describe(config, ai_enabled=False), "关掉窗口，文案也要闭嘴"


# ---------------------------------------------------------------- the two paths
def test_a_night_breaking_story_is_deferred_not_dropped(config, session, monkeypatch):
    art = _first_hand_row(session, "Anthropic announces a rebuilt reasoning model",
                          "https://openai.com/quiet1")
    cfg = quiet_config(config)
    monkeypatch.setattr("app.config._now_utc", lambda: NIGHT)
    service = DigestService(cfg)
    ok, why = service.can_send_breaking(111111111, article_id=art)
    assert not ok and QUIET_TOKEN in why, why
    assert deferral_worthwhile(why), "静默是等一会儿，不是判死刑"
    monkeypatch.setattr("app.config._now_utc", lambda: MORNING)
    ok2, why2 = service.can_send_breaking(111111111, article_id=art)
    assert ok2, f"出窗口后同一行就该能发：{why2}"


def test_the_gate_still_decides_first_so_a_stale_story_cannot_hide_in_the_window(
        config, session, monkeypatch):
    """顺序很重要：过期/已发过的行必须被判"终局"，不能被写成"再等等"永远挂着。"""
    from datetime import timedelta

    art = _first_hand_row(session, "OpenAI announced an ancient outage", "https://openai.com/quiet3")
    with session_scope() as s:
        s.get(Article, art).published_at = datetime.utcnow() - timedelta(hours=30)
        s.commit()
    monkeypatch.setattr("app.config._now_utc", lambda: NIGHT)
    ok, why = DigestService(quiet_config(config)).can_send_breaking(111111111, article_id=art)
    assert not ok and QUIET_TOKEN not in why, f"出窗的行该拿到终局理由，不是“再等等”：{why}"
    assert not deferral_worthwhile(why), why


@pytest.mark.asyncio
async def test_the_retry_queue_carries_it_over_the_window(config, session, monkeypatch):
    """整条路径：夜里那一轮什么都不发、也不记账；早上第一轮补发并清标记。"""
    from app.scheduler.jobs import NewsJobs

    art = _first_hand_row(session, "OpenAI unveils a quiet-hours regression test",
                          "https://openai.com/quiet2")
    cfg = quiet_config(config)
    sender = Sender()
    jobs = NewsJobs(cfg, sender=sender)
    jobs.chat_ids = lambda: [111111111]

    monkeypatch.setattr("app.config._now_utc", lambda: NIGHT)
    assert await jobs.send_breaking([art]) == 0
    assert sender.delivered == [], "静默时段里不该有任何东西发出去"
    with session_scope() as s:
        assert (s.get(Article, art).meta or {}).get("breaking_defer"), "标记要留着，早上才有得补"
        assert s.get(Article, art).is_sent is not True

    monkeypatch.setattr("app.config._now_utc", lambda: MORNING)
    assert await jobs.send_breaking([]) == 1
    assert sender.delivered == [art]
    with session_scope() as s:
        assert not (s.get(Article, art).meta or {}).get("breaking_defer"), "补发后标记该清掉"


@pytest.mark.asyncio
async def test_the_free_offer_path_waits_too(config, session, tmp_path, monkeypatch):
    """05:00 的限免推送是最不该吵人的一种，而且它本来就要等发送成功才记账。"""
    article_id = _offer_row(session, "某 agent 限免一周", "https://linux.do/t/quiet1")
    qc = quiet_config(config)
    # 关掉定价接口：套件不许联网（和 tests/test_free_alerts.py 同一个约定）。
    cfg = dataclasses.replace(qc, raw={**qc.raw, "free": {
        **qc.raw.get("free", {}),
        "models": {"enabled": False, "state_file": str(tmp_path / "m.json")}}})

    monkeypatch.setattr("app.config._now_utc", lambda: NIGHT)
    night = FreeAlertService(cfg, sender=Chat())
    assert await night.run([111]) == 0
    with session_scope() as s:
        assert s.get(Article, article_id).free_offer_sent_at is None, "没发出去就不该记为已推送"

    monkeypatch.setattr("app.config._now_utc", lambda: MORNING)
    chat = Chat()
    morning = FreeAlertService(cfg, sender=chat)
    assert await morning.run([111]) == 1
    assert "限免" in chat.sent[0][1]
