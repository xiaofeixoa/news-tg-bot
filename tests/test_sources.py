"""数据源登记表与配置的一致性（此前零覆盖：/stats 的源数量、维护告警都读它）。"""

from __future__ import annotations

import dataclasses
import pathlib
from datetime import datetime

import pytest
import yaml
from sqlalchemy import select

from app.config import get_config
from app.database import repository as repo
from app.database.models import Source
from app.scheduler import jobs as jobs_mod


def configured(name: str, *, enabled: bool = True, url: str = "https://x/feed",
               quality: str = "B", type_: str = "rss") -> dict:
    return {"name": name, "type": type_, "url": url, "quality": quality, "enabled": enabled}


def test_sync_creates_rows_from_the_config(session):
    changed = repo.sync_sources(session, [configured("OpenAI"), configured("Linux", enabled=False)])
    session.commit()
    assert changed == 2
    rows = {s.name: s for s in session.query(Source)}
    assert rows["OpenAI"].enabled is True and rows["Linux"].enabled is False
    assert repo.sync_sources(session, [configured("OpenAI"), configured("Linux", enabled=False)]) == 0


def test_turning_a_source_off_clears_the_failure_it_was_accumulating(session):
    """VentureBeat AI 的情形：配置里关了，库里还留着 114 次失败。"""
    repo.sync_sources(session, [configured("VentureBeat AI")])
    session.commit()
    source = session.query(Source).filter_by(name="VentureBeat AI").one()
    for _ in range(3):
        repo.mark_source_fetch(session, source.id, ok=False, error="HTTP 429")
    session.commit()
    assert source.error_count == 3 and source.last_error

    repo.sync_sources(session, [configured("VentureBeat AI", enabled=False)])
    session.commit()
    assert source.enabled is False
    assert source.error_count == 0 and source.last_error is None, \
        "关掉的源不该带着旧失败计数被重新打开时算成正在坏"


def test_a_source_dropped_from_the_config_stops_being_reported(session):
    repo.sync_sources(session, [configured("Gone")])
    session.commit()
    assert session.query(Source).filter_by(name="Gone").one().enabled is True
    repo.sync_sources(session, [configured("OpenAI")])
    session.commit()
    assert session.query(Source).filter_by(name="Gone").one().enabled is False


def test_url_and_quality_follow_the_config(session):
    repo.sync_sources(session, [configured("The Verge AI", url="https://old/feed", quality="C")])
    session.commit()
    row = session.query(Source).filter_by(name="The Verge AI").one()
    repo.sync_sources(session, [configured("The Verge AI", url="https://new/feed", quality="B")])
    session.commit()
    assert row.url == "https://new/feed" and row.quality == "B"


@pytest.mark.asyncio
async def test_sync_and_a_collection_round_do_not_fight(session):
    """回归用例：第一轮部署后线上连着两回合都报告"更新了 25/14 个字段"。

    原因是采集循环会把每条新闻自己的 URL 写进 sources.url，而同步又把配置里的
    feed URL 写回去——两边每轮互相覆盖，登记表永远"有变化"。
    """
    from datetime import datetime

    from app.config import get_config
    from app.processing.normalize import build_article
    from app.processing.pipeline import collect

    cfg = get_config()

    def item(url: str) -> dict:
        return build_article(title="OpenAI ships a reasoning model", url=url, source_name="OpenAI",
                             content="OpenAI releases a new model with cheaper inference.",
                             published_at=datetime.utcnow())

    class Feed:
        source_name = "OpenAI"

        async def collect(self):
            return [item("https://openai.com/index/a"), item("https://openai.com/index/b")]

    await collect(session, [Feed()], config=cfg, llm=None)
    session.commit()
    before = {s.name: (s.url, s.enabled, s.quality) for s in session.query(Source)}
    assert before["OpenAI"][0] == next((s.get("url") for s in cfg.sources if s.get("name") == "OpenAI"), None), \
        "sources.url 是 feed 地址，不是某篇文章的地址"

    changed = repo.sync_sources(session, cfg.sources)
    session.commit()
    assert changed == 0, f"登记表没收敛，每轮都会重写：{before} -> " \
                         f"{ {s.name: (s.url, s.enabled, s.quality) for s in session.query(Source)} }"


def test_status_line_warns_before_the_disk_kills_collection():
    """写满之后不会有报错声——SQLite 只是拒绝写入，看起来像"今天没新闻"。"""
    from app.services import format as fmt

    base = {"total_articles": 700, "last_24h": 100, "llm_enabled": False, "sources": 20,
            "sources_configured": 37, "sources_delivering": 15}
    quiet = fmt.status_line(dict(base, disk_free_mb=8192))
    assert "磁盘" not in quiet
    loud = fmt.status_line(dict(base, disk_free_mb=770))
    assert "磁盘只剩 0.8GB" in loud and "告警线" in loud, loud


def test_stats_reports_the_free_space_it_is_running_on(session, tmp_path):
    from app.services.news import get_news_service

    stats = get_news_service().stats()
    assert isinstance(stats["disk_free_mb"], int) and stats["disk_free_mb"] > 0


@pytest.mark.asyncio
async def test_maintenance_logs_a_disk_warning_at_the_threshold(session, monkeypatch):
    from app.config import AppConfig
    from app.services.news import NewsService

    config = AppConfig(settings=get_config().settings, raw={}, sources=[])
    jobs = jobs_mod.NewsJobs(config)
    warned: list[str] = []
    monkeypatch.setattr(jobs_mod.log, "warning", lambda *a, **k: warned.append(str(a[0]) % a[1:] if a else ""))
    monkeypatch.setattr(NewsService, "stats", lambda self: {"disk_free_mb": 300})
    await jobs.run_maintenance()
    assert any("only 300MB free" in w for w in warned), warned

    warned.clear()
    monkeypatch.setattr(NewsService, "stats", lambda self: {"disk_free_mb": 4096})
    await jobs.run_maintenance()
    assert not any("free" in w for w in warned), warned


@pytest.mark.asyncio
async def test_maintenance_stops_warning_about_sources_he_switched_off(session, monkeypatch):
    from app.config import AppConfig

    repo.sync_sources(session, [configured("Dead But Enabled")])
    source = session.query(Source).filter_by(name="Dead But Enabled").one()
    for _ in range(6):
        repo.mark_source_fetch(session, source.id, ok=False, error="HTTP 403")
    session.commit()

    config = AppConfig(settings=get_config().settings, raw={"sources": []}, sources=[])
    jobs = jobs_mod.NewsJobs(config)
    warned: list[tuple] = []
    monkeypatch.setattr(jobs_mod.log, "warning", lambda *a, **k: warned.append(a))
    await jobs.run_maintenance()
    assert any("Dead But Enabled" in str(part) for w in warned for part in w), \
        "启用的坏源必须继续报警"

    warned.clear()
    config.sources = [configured("Dead But Enabled", enabled=False)]
    repo.sync_sources(session, config.sources)
    session.commit()
    await jobs.run_maintenance()
    assert not warned, f"关掉的源不该再报：{warned}"


def test_a_spoofed_browser_user_agent_must_come_with_browser_tls():
    """UA 与 TLS 指纹是一套签名：只改 UA 会被判成机器人。

    线上实测 2026-09-30（美西 VPS，真实采集器同一 URL 各一次）：戴着 Chrome/124 UA、
    走 httpx 握手的 `Reddit LocalLLaMA RSS` 得到 **HTTP 403** 并连着失败 15 次；
    换成默认的 `AI-News-Radar/1.0 (+personal research agent…)` 单次请求 **200 / 50 条**。
    Linux.do 那条相反——Cloudflare 按 TLS 指纹拦 python 客户端，所以浏览器 UA 必须与
    `browser_tls` 成对出现。两者拆开写，就等于发明了一个真实浏览器不会有的签名。
    """
    cfg = yaml.safe_load(pathlib.Path("config/sources.yaml").read_text(encoding="utf-8"))
    offenders = []
    for source in cfg["sources"]:
        ua = str((source.get("headers") or {}).get("User-Agent") or "")
        claims_browser = any(tag in ua for tag in ("Mozilla/", "Chrome", "Safari", "Gecko"))
        if claims_browser and not source.get("browser_tls"):
            offenders.append((source.get("name"), ua[:44]))
    assert offenders == [], f"这些源伪装了浏览器 UA 却没配 browser_tls：{offenders}"


def test_the_reddit_rss_sources_identify_themselves_rather_than_spoof():
    """Reddit 只肯伺候自报家门的客户端：这条把测到的那个例外钉在配置里。"""
    cfg = yaml.safe_load(pathlib.Path("config/sources.yaml").read_text(encoding="utf-8"))
    reddit = [s for s in cfg["sources"] if "reddit.com" in str(s.get("url") or "")]
    assert reddit, "reddit 源不该从配置里消失（近 7 天它入库 351 条）"
    for source in reddit:
        ua = str((source.get("headers") or {}).get("User-Agent") or "")
        assert "Mozilla" not in ua, f"{source.get('name')} 又戴回 Chrome UA 了：{ua[:40]}"
    assert all("attempts" not in source or int(source["attempts"]) == 1 for source in reddit),         "reddit 按 IP 限流，一轮多次请求只会互相抢额度"


# ------------------------------------------------ 什么才算"这个源坏了"
def _source_with_errors(session, name: str, *, failures: int, error: str = "HTTP 403") -> None:
    """`sync_sources` 会把没列出的源当成"配置里删掉了"并顺手清零，所以这里直接 upsert。"""
    source = repo.get_or_create_source(session, name, type_="rss", url=f"https://example.org/{name}.rss",
                                      quality="B", enabled=True)
    session.commit()
    for _ in range(failures):
        repo.mark_source_fetch(session, source.id, ok=False, error=error)
    session.commit()


def _config_with_alert(config, **values):
    base = dict(config.raw or {})
    alerts = {**(base.get("alerts") or {}), **values}
    base["alerts"] = alerts
    return dataclasses.replace(config, raw=base)


def test_a_healable_hiccup_is_not_reported_as_a_failing_source(session):
    """线上实测 2026-09-30：/stats 说"3 个正在报错"，那三个是 GitHub 配额抖动（1-2 次），
    而真正的坏源（Reddit 连着 15 次 403）说的是同一句话。分不开就是他没有行动依据。"""
    from app.services.format import status_line
    from app.services.news import NewsService

    _source_with_errors(session, "GitHub Releases", failures=2, error="GitHub API 匿名配额用完")
    _source_with_errors(session, "Reddit LocalLLaMA RSS", failures=15)
    stats = NewsService(get_config()).stats()
    assert stats["sources_failing"] == 1, stats
    assert stats["sources_blipping"] == 1, stats
    assert stats["sources_failing_detail"][0]["name"] == "Reddit LocalLLaMA RSS"
    line = status_line(stats)
    assert "持续失败：Reddit LocalLLaMA RSS（连续 15 次：HTTP 403）" in line, line
    assert "1 个刚抖了一下" in line, line
    assert "正在报错" not in line, line


@pytest.mark.asyncio
async def test_the_same_configured_threshold_drives_stats_and_the_health_log(session, monkeypatch):
    """一个配置数字，两个读者：改它必须同时改变 /stats 与运维报警。"""
    from app.services.news import NewsService

    _source_with_errors(session, "Quirky Feed", failures=2, error="HTTP 500")
    warned: list[str] = []
    monkeypatch.setattr(jobs_mod.log, "warning",
                        lambda msg, *a, **k: warned.append(str(msg) % a if a else str(msg)))

    roomy = _config_with_alert(get_config(), source_fail_threshold=5)
    await jobs_mod.NewsJobs(roomy).run_maintenance()
    assert not [w for w in warned if "Quirky" in w], warned
    assert NewsService(roomy).stats()["sources_failing"] == 0

    warned.clear()
    strict = _config_with_alert(get_config(), source_fail_threshold=1)
    await jobs_mod.NewsJobs(strict).run_maintenance()
    assert [w for w in warned if "Quirky" in w and "in a row" in w], warned
    stats = NewsService(strict).stats()
    assert stats["sources_failing"] == 1, stats
    assert stats["sources_blipping"] == 0, stats


def test_a_success_clears_the_streak_so_the_warning_means_what_it_says(session):
    """"连续 N 次"只有在成功时归零才是真话（mark_source_fetch 的这条规矩由用例钉住）。"""
    from app.services.news import NewsService

    _source_with_errors(session, "Flaky Feed", failures=9)
    assert NewsService(get_config()).stats()["sources_failing"] == 1
    source = session.scalar(select(Source).where(Source.name == "Flaky Feed"))
    repo.mark_source_fetch(session, source.id, ok=True, items=3)
    session.commit()
    stats = NewsService(get_config()).stats()
    assert stats["sources_failing"] == 0 and stats["sources_blipping"] == 0, stats


class _Capture:
    """真实的 logging handler：`warn_once` 走 `logger.log(level, …)`，
    只替 `.warning`/`.info` 方法是捕不到的（第一版就漏在这里）。"""

    def __init__(self, logger):
        import logging as _logging

        self.records: list[tuple[str, str]] = []

        class _Sink(_logging.Handler):
            def emit(_self, record):  # noqa: N805
                self.records.append((record.levelname, record.getMessage()))

        self.handler = _Sink()
        self.logger = logger
        # 测试进程没跑 setup_logging，"news" 这个 logger 的等级继承 root（WARNING），
        # INFO 记录会在到达 handler 之前就被丢掉：把它调到和线上一致。
        self.previous = logger.level
        logger.setLevel(_logging.INFO)
        logger.addHandler(self.handler)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.previous)
        return False



# --------------------------- 我们在守 Retry-After，不是源坏了
class _Cooling:
    """一个"这轮什么都不做"的采集器：对方说了等 N 分钟。"""

    source_name = "Linux.do 福利分类"

    def __init__(self, error: Exception):
        self.error = error

    async def collect(self):
        raise self.error


def _source_row(session, name: str) -> Source:
    return session.scalar(select(Source).where(Source.name == name))


@pytest.mark.asyncio
async def test_a_scheduled_wait_does_not_become_a_source_failure(session, monkeypatch):
    """线上实测：一句"源服务器要求降速"5 天打了 246 行，还被记进 error_count。

    我们是 10 分钟一轮，对方要 100 分钟，于是礼貌自己就把连击推到 5 的门槛上——
    健康检查和 `/stats` 会报"这个源持续失败"，而它其实什么也没做错。
    """
    from app.collectors.base import SourceCooling
    from app.logging_setup import reset_warned_once
    from app.processing.pipeline import collect

    reset_warned_once()
    repo.get_or_create_source(session, "Linux.do 福利分类", type_="rss",
                             url="https://linux.do/c/welfare/36.rss", quality="C", enabled=True)
    session.commit()

    pipeline_log = __import__("app.processing.pipeline", fromlist=["log"]).log
    cfg = get_config()
    error = SourceCooling("https://linux.do/c/welfare/36.rss -> 源服务器要求降速，还剩 97 分钟再试")
    with _Capture(pipeline_log) as cap:
        for _ in range(5):                  # 旧逻辑：5 轮就点亮"持续失败"
            stats = await collect(session, [_Cooling(error)], config=cfg, llm=None)
            session.commit()
    assert stats.errors == [] and len(stats.skipped) == 1, stats
    assert "waiting=1" in str(stats) and "errors=0" in str(stats), str(stats)

    row = _source_row(session, "Linux.do 福利分类")
    assert (row.error_count or 0) == 0, f"礼貌的等待不该记成连击：{row.error_count}"
    assert "降速" in (row.last_error or ""), row.last_error
    assert [lvl for lvl, _ in cap.records if lvl in ("WARNING", "ERROR")] == [], cap.records
    said = [m for _l, m in cap.records if m.startswith("collector Linux.do")]
    assert len(said) == 1, f"同一件事每轮重播：{said}"


@pytest.mark.asyncio
async def test_the_wait_travels_through_the_real_collector_path(session):
    """上面那几条是自己 raise 异常，测不到"真代码到底抛什么"。

    少了这一条就会漏掉最要紧的接线：`BaseCollector.get()` 若把这次等待退回
    普通的 `CollectorError`，管道依旧会把它记成连击，而所有单元测试照样绿。
    """
    from app.collectors.base import cool_down, cooling
    from app.collectors.rss import RSSCollector
    from app.logging_setup import reset_warned_once
    from app.processing.pipeline import collect

    reset_warned_once()
    url = "https://example.org/parked.rss"
    repo.get_or_create_source(session, "Parked Feed", type_="rss", url=url,
                              quality="C", enabled=True)
    session.commit()
    pipeline_log = __import__("app.processing.pipeline", fromlist=["log"]).log
    collector = RSSCollector({"name": "Parked Feed", "type": "rss", "url": url,
                              "attempts": 1, "quality": "C"})
    cool_down(url, 600)
    assert cooling(url) > 0, "先把这个主机挂起，模拟对方给的 Retry-After"
    with _Capture(pipeline_log), _Capture(repo.log):
        stats = await collect(session, [collector], config=get_config(), llm=None)
        session.commit()
    row = _source_row(session, "Parked Feed")
    assert (row.error_count or 0) == 0, f"真路径把等待记成了失败：{row.error_count}"
    assert "降速" in (row.last_error or ""), row.last_error
    assert stats.errors == [] and len(stats.skipped) == 1, stats


@pytest.mark.asyncio
async def test_a_real_failure_right_after_a_wait_still_builds_the_streak(session):
    """不把等待算进连击，也不能顺手把真实的连击清掉。"""
    from app.collectors.base import CollectorError, SourceCooling
    from app.logging_setup import reset_warned_once
    from app.processing.pipeline import collect

    reset_warned_once()
    repo.get_or_create_source(session, "Flaky After Wait", type_="rss",
                              url="https://example.org/flaky.rss", quality="C", enabled=True)
    session.commit()
    cfg = get_config()
    pipeline_log = __import__("app.processing.pipeline", fromlist=["log"]).log

    class Feed:
        source_name = "Flaky After Wait"

        async def collect(self):
            raise CollectorError("HTTP 500")

    with _Capture(pipeline_log), _Capture(repo.log):
        await collect(session, [Feed()], config=cfg, llm=None)
        session.commit()
        row = _source_row(session, "Flaky After Wait")
        assert row.error_count == 1, row.error_count

        cooling = _Cooling(SourceCooling("https://example.org/flaky.rss -> 源服务器要求降速，还剩 3 分钟再试"))
        cooling.source_name = "Flaky After Wait"
        await collect(session, [cooling], config=cfg, llm=None)
        session.commit()
    row = _source_row(session, "Flaky After Wait")
    assert row.error_count == 1, f"等待不该加连击，也不该清零：{row.error_count}"


def test_warn_once_says_a_repeating_state_once_per_process():
    import logging as _logging

    from app.logging_setup import reset_warned_once, warn_once

    reset_warned_once()
    seen: list[tuple[int, str]] = []

    class Sink:
        def log(self, level, msg, *args):
            seen.append((level, msg % args if args else str(msg)))

        def debug(self, msg, *args):
            seen.append((_logging.DEBUG, msg % args if args else str(msg)))

    assert warn_once(Sink(), "some-key", "第一轮说明：%s", "原因") is True
    assert warn_once(Sink(), "some-key", "第二轮说明：%s", "原因") is False
    assert warn_once(Sink(), "other-key", "另一件事：%s", "原因") is True
    assert [lvl for lvl, _ in seen] == [_logging.INFO, _logging.DEBUG, _logging.INFO], seen


@pytest.mark.asyncio
async def test_the_github_token_hint_is_not_replayed_every_round(session, monkeypatch):
    """这一句 5 天打了 115 行：同一个缺密钥状态，每轮都说一次。"""
    from app.logging_setup import reset_warned_once

    reset_warned_once()
    cfg = get_config()
    if cfg.settings.github_token:           # 只有在真没配的时候这条才有意义
        pytest.skip("这台机器配了 GITHUB_TOKEN")
    jobs = jobs_mod.NewsJobs(cfg)
    with _Capture(jobs_mod.log) as cap:
        jobs._warn_github_budget()
        jobs._warn_github_budget()
        jobs._warn_github_budget()
    said = [m for lvl, m in cap.records if "GITHUB_TOKEN" in m]
    assert len(said) == 1, cap.records
