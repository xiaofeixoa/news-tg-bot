"""Tests for the optional browser-TLS transport (Cloudflare JA3 work-around).

curl_cffi is an optional dependency, so every test drives it through a stub
instead of the network.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.collectors import base as B
from app.collectors.rss import RSSCollector


class FakeBrowserResponse:
    def __init__(self, payload: bytes = b"<rss>ok</rss>", status: int = 200) -> None:
        self.status_code = status
        self.content = payload
        self.headers = {"content-type": "application/rss+xml"}
        self.text = payload.decode("utf-8")

    def json(self) -> Any:
        import json as _json

        return _json.loads(self.text)


def make_source(**extra: Any) -> dict[str, Any]:
    return {"name": "Linux.do 福利分类", "type": "rss",
            "url": "https://linux.do/c/welfare/36.rss", "enabled": True,
            "category": "community", "quality": "C", **extra}


async def test_browser_tls_source_uses_the_impersonating_transport(monkeypatch):
    calls: dict[str, Any] = {}

    async def fake_browser_get(url, **kwargs):
        calls["url"] = url
        calls["impersonate"] = kwargs.get("impersonate")
        feed = ('<rss><channel><item><title>限时免费：某 agent 送 100 刀额度</title>'
                '<link>https://linux.do/t/x</link></item></channel></rss>')
        return FakeBrowserResponse(feed.encode("utf-8"))

    monkeypatch.setattr(B, "browser_tls_available", lambda: True)
    monkeypatch.setattr(B, "browser_get", fake_browser_get)
    collector = RSSCollector(make_source(browser_tls=True, attempts=1,
                                        impersonate="chrome124"))
    items = await collector.fetch()
    assert calls["impersonate"] == "chrome124"
    assert calls["url"].endswith("36.rss")
    assert items and "限时免费" in items[0]["title"]


async def test_httpx_is_still_used_without_the_flag(monkeypatch):
    used: list[str] = []

    async def fake_browser_get(url, **kwargs):  # pragma: no cover - must not run
        used.append("browser")
        return FakeBrowserResponse()

    class FakeClient:
        async def get(self, url, **kwargs):
            used.append("httpx")
            return FakeBrowserResponse(b"<rss><channel></channel></rss>")

    monkeypatch.setattr(B, "browser_tls_available", lambda: True)
    monkeypatch.setattr(B, "browser_get", fake_browser_get)
    collector = RSSCollector(make_source(attempts=1))
    monkeypatch.setattr(collector, "http", lambda: _done(FakeClient()))
    response = await collector.get("https://linux.do/c/welfare/36.rss")
    assert used == ["httpx"] and response.status_code == 200


async def test_missing_package_falls_back_to_httpx(monkeypatch):
    used: list[str] = []

    async def boom(url, **kwargs):  # pragma: no cover - must not run
        used.append("browser")
        raise B.BrowserTLSUnavailable("curl_cffi is not installed")

    class FakeClient:
        async def get(self, url, **kwargs):
            used.append("httpx")
            return FakeBrowserResponse(b"<rss><channel></channel></rss>")

    monkeypatch.setattr(B, "browser_tls_available", lambda: False)
    monkeypatch.setattr(B, "browser_get", boom)
    collector = RSSCollector(make_source(browser_tls=True, attempts=1))
    monkeypatch.setattr(collector, "http", lambda: _done(FakeClient()))
    await collector.get("https://linux.do/c/welfare/36.rss")
    assert used == ["httpx"]   # no curl_cffi: degrade to plain httpx, never crash


async def test_browser_tls_requested_but_missing_reports_clearly(monkeypatch):
    # available() says yes (e.g. import raced) but the call cannot be made
    async def boom(url, **kwargs):
        raise B.BrowserTLSUnavailable("curl_cffi is not installed")

    monkeypatch.setattr(B, "browser_tls_available", lambda: True)
    monkeypatch.setattr(B, "browser_get", boom)
    collector = RSSCollector(make_source(browser_tls=True, attempts=1))
    with pytest.raises(B.CollectorError) as exc:
        await collector.get("https://linux.do/c/welfare/36.rss")
    assert "curl_cffi" in str(exc.value)


def _done(value):
    import asyncio

    async def _coro():
        return value

    return _coro()


async def test_curl_errors_become_actionable_collector_errors(monkeypatch):
    async def boom(url, **kwargs):
        raise RuntimeError("Failed to perform, curl: (77) error adding trust anchors")

    monkeypatch.setattr(B, "browser_tls_available", lambda: True)
    monkeypatch.setattr(B, "browser_get", boom)
    collector = RSSCollector(make_source(browser_tls=True, attempts=1))
    with pytest.raises(B.CollectorError) as exc:
        await collector.get("https://linux.do/c/welfare/36.rss")
    assert "curl: (77)" in str(exc.value)
    assert "36.rss" in str(exc.value)


async def test_browser_http_status_is_still_checked(monkeypatch):
    async def forbidden(url, **kwargs):
        return FakeBrowserResponse(b"<html>Just a moment</html>", status=403)

    monkeypatch.setattr(B, "browser_tls_available", lambda: True)
    monkeypatch.setattr(B, "browser_get", forbidden)
    collector = RSSCollector(make_source(browser_tls=True, attempts=1))
    with pytest.raises(B.CollectorError) as exc:
        await collector.get("https://linux.do/c/welfare/36.rss")
    assert "HTTP 403" in str(exc.value)


def test_browser_response_exposes_the_httpx_surface():
    response = B.BrowserResponse(FakeBrowserResponse(b'{"a": 1}', status=200))
    assert response.status_code == 200
    assert response.json() == {"a": 1}
    assert response.text.startswith('{"a"')
    assert response.content == b'{"a": 1}'


async def test_close_browser_session_is_idempotent(monkeypatch):
    closed: list[bool] = []

    class Session:
        async def close(self):
            closed.append(True)

    B._browser_state["session"] = Session()
    await B.close_browser_session()
    await B.close_browser_session()
    assert closed == [True]
    assert B._browser_state["session"] is None


def test_source_config_documents_the_flag():
    from app.config import get_config

    entry = get_config().source_by_name("Linux.do 福利分类")
    assert entry is not None
    assert entry.get("browser_tls") is True
    assert "/c/welfare.rss" not in str(entry.get("url"))


# ------------------------------------------------------------------ delivery
class FakeBot:
    def __init__(self, fail_markup: bool = False, fail_other: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail_markup = fail_markup
        self.fail_other = fail_other

    async def send_message(self, **kwargs):
        from aiogram.exceptions import TelegramBadRequest

        self.calls.append(kwargs)
        if self.fail_markup and kwargs.get("parse_mode"):
            raise TelegramBadRequest(method="sendMessage", message="can't parse entities")
        if self.fail_other:
            raise TelegramBadRequest(method="sendMessage", message="chat not found")

    class _Session:
        async def close(self):
            return None

    session = _Session()


async def test_scheduled_pushes_ask_telegram_to_parse_html():
    """Digest text is HTML; without parse_mode it arrived as literal <b> tags."""
    from app.bot.sender import TelegramSender

    bot = FakeBot()
    sender = TelegramSender(bot)  # type: ignore[arg-type]
    assert await sender.send(1, "<b>早报</b>") is True
    assert bot.calls[0]["parse_mode"] == "HTML"


async def test_bad_markup_degrades_to_plain_text_instead_of_dropping_the_digest():
    from app.bot.sender import TelegramSender

    bot = FakeBot(fail_markup=True)
    sender = TelegramSender(bot)  # type: ignore[arg-type]
    assert await sender.send(1, "<b>broken") is True
    assert [c["parse_mode"] for c in bot.calls] == ["HTML", None]


async def test_other_api_errors_are_not_retried_as_plain_text():
    from app.bot.sender import TelegramSender

    bot = FakeBot(fail_other=True)
    sender = TelegramSender(bot)  # type: ignore[arg-type]
    assert await sender.send(1, "<b>x</b>") is False
    assert len(bot.calls) == 1
