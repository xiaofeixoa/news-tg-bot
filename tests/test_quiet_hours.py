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
    from datetime import timedelta

    # 行必须钉在被注入的那颗钟上：v1.91 有一条用例用 `utcnow()` 造行、却把判断钟冻在
    # NIGHT，于是"这条新闻多久大" = 真钟与 NIGHT 的距离，真钟每走一小时它就离终局远一小时——
    # 今早 04:52Z 还绿、05:05Z 就红了。整批"夜里到达"的用例都按同一颗钟走。
    stored = repo.save_article(session, build_article(
        title=title, url=url, source_name="OpenAI", source_type="rss",
        published_at=NIGHT - timedelta(minutes=10),
        content=title + ". The company says the new model improves reasoning. " * 4))
    article = session.get(Article, stored.id)
    article.final_score = 96
    article.source_quality = 95
    article.is_processed = True
    article.category = "AI Models"
    session.commit()
    return int(stored.id)


def _offer_row(session, title: str, url: str) -> int:
    from datetime import timedelta

    article = repo.save_article(session, build_article(
        title=title, url=url, source_name="Linux.do 福利分类", source_type="rss",
        published_at=NIGHT - timedelta(minutes=10),
        content=title + " 详情", quality="C"))
    article.is_free_offer = True
    article.free_offer_tool = "Qoder"
    article.free_offer = {"tool": "Qoder", "kind": "编程 Agent/IDE", "signals": ["限免"],
                          "models": [], "confidence": 0.8}
    article.is_processed = True
    article.final_score = 70
    session.commit()
    return int(article.id)


def test_the_pinned_rows_do_not_lean_on_the_wall_clock(session):
    """这批用例的行必须只认被注入的那颗钟（v1.91 的红线就是从真钟漏进来得到的）。

    真钟每走一小时，用 `utcnow()` 造出来的行就离"过期"远一小时。这里把行的时间钉到分钟，
    谁再把真实时间塞回这批用例，这条先红——规矩写在 docstring 里不够，得有用例盯着。
    """
    from datetime import timedelta

    art = _first_hand_row(session, "OpenAI pins the clock", "https://openai.com/pin")
    with session_scope() as s:
        row = s.get(Article, art)
        drift = abs((row.published_at - (NIGHT - timedelta(minutes=10))).total_seconds())
        assert drift < 5, f"行又被真钟带着走了：漂移 {drift} 秒"


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
        # 这行必须钉在**被注入的那颗时钟**上。之前写的是 `datetime.utcnow() - 30h`，
        # 也就是"真实现在减 30 小时"，而下一行把判断时钟冻在 NIGHT（2026-09-30 20:00）：
        # 于是测出来的时效 = 30h −（真现在 − NIGHT），真钟每走一小时这条用例就离"过期"远一小时。
        # 2026-10-01 它就在全绿了十几分钟后自己变红了——文件开头那句 docstring 警告的正是这个。
        s.get(Article, art).published_at = NIGHT - timedelta(hours=30)
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


def _published_ago(session, art: int, hours: float) -> None:
    """把 published_at 往前挪：让"这条还能不能撑到放行"成为可以被精确设定的一件事。"""
    from datetime import timedelta

    with session_scope() as s:
        s.get(Article, art).published_at = NIGHT - timedelta(hours=hours)
        s.commit()


def test_a_row_that_expires_inside_the_window_is_not_promised_a_release(config, session, monkeypatch):
    """北京 04:00 挡下、07:00 放行——可这条只剩 1.5 小时时效，那句"自动补发"就是假话。"""
    art = _first_hand_row(session, "Anthropic announces a rebuilt reasoning model",
                          "https://openai.com/expire1")
    _published_ago(session, art, 22.5)
    monkeypatch.setattr("app.config._now_utc", lambda: NIGHT)
    ok, why = DigestService(quiet_config(config)).can_send_breaking(111111111, article_id=art)
    assert not ok, "时效撑不到放行，这条本来就不该发"
    assert "时效先到" in why and "届时无话可补" in why, why
    assert not deferral_worthwhile(why), "等下去也来不及：留在队列里就是一个没有结果的查询"
    assert QUIET_TOKEN not in why, "理由里不能带窗口 token，否则又被判成再等等"


def test_a_row_that_can_reach_the_opening_is_still_promised_a_release(config, session, monkeypatch):
    """反方向：时效够的那条，一句"07:00 之后自动补发"仍然要说出口，并且要留在队列里。"""
    art = _first_hand_row(session, "Google announces a new TPU generation",
                          "https://openai.com/expire2")
    _published_ago(session, art, 3.0)
    monkeypatch.setattr("app.config._now_utc", lambda: NIGHT)
    ok, why = DigestService(quiet_config(config)).can_send_breaking(111111111, article_id=art)
    assert not ok and QUIET_TOKEN in why, why
    assert deferral_worthwhile(why), "这才是等一会儿"
    assert "时效先到" not in why, why


def test_the_remaining_hours_to_the_opening_are_measured_not_guessed(config):
    """`quiet_opens_in` 必须和 `quiet_window` 判同一个窗口，否则两个函数会各自漂移。"""
    from datetime import timedelta

    from app.config import quiet_opens_in

    cfg = quiet_config(config)
    assert quiet_opens_in(cfg, "Asia/Shanghai", at=NIGHT) == pytest.approx(3.0), "北京 04:00 还差 3 小时"
    assert quiet_opens_in(cfg, "Asia/Shanghai", at=datetime(2026, 9, 30, 15, 0)) == pytest.approx(8.0)
    assert quiet_opens_in(cfg, "Asia/Shanghai", at=EXACT_OPEN) is None, "07:00 整点已经放行"
    assert quiet_opens_in(cfg, "Asia/Shanghai", at=DAYTIME) is None
    assert quiet_opens_in(quiet_config(config, "off"), "Asia/Shanghai", at=NIGHT) is None
    # 窗口内任意一分钟，两个函数必须同时表态
    for minutes in range(0, 24 * 60, 7):
        at = datetime(2026, 9, 30, 0, 0) + timedelta(minutes=minutes)   # 这是 UTC；北京时间 +8
        local_hour = (at + timedelta(hours=8)).hour
        inside = local_hour >= 23 or local_hour < 7
        assert bool(quiet_window(cfg, "Asia/Shanghai", at=at)) == inside, (at, inside)
        assert (quiet_opens_in(cfg, "Asia/Shanghai", at=at) is not None) == inside, (at, inside)


def test_the_gate_ages_against_the_same_injected_clock_as_the_quiet_window(
        config, session, monkeypatch):
    """一次"能不能发"的判定里只能有一个此刻。

    `gate()` 过去读 `datetime.utcnow()`，而静默窗口读 `app.config._now_utc()`：
    相差 1.6 小时就能让同一条新闻既是"没过期"又是"过期 24 小时"——写 v1.80 时实测撞到。
    """
    from app.processing import breaking

    art = _first_hand_row(session, "OpenAI announces a frontier reasoning model",
                          "https://openai.com/clock1")
    _published_ago(session, art, 22.5)
    monkeypatch.setattr("app.config._now_utc", lambda: NIGHT)
    with session_scope() as s:
        row = s.get(Article, art)
        ok, why = breaking.gate(row, config=quiet_config(config))
    assert ok, f"注入的钟说这条 22.5 小时，就该按 22.5 小时判：{why}"
