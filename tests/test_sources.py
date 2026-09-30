"""数据源登记表与配置的一致性（此前零覆盖：/stats 的源数量、维护告警都读它）。"""

from __future__ import annotations

import pathlib
from datetime import datetime

import pytest
import yaml

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
