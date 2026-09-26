"""Collector plug-in framework (design doc sections 4, 21).

A new source type = one subclass + a `register` decorator; nothing in the core
pipeline changes. Sources of the same type come from config/sources.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Iterable, Sequence

import httpx
import os
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import AppConfig, as_float, get_config
from app.logging_setup import get_logger
from app.processing.normalize import build_article, clean_text, keyword_hits

log = get_logger("collector")

DEFAULT_USER_AGENT = (
    "AI-News-Radar/1.0 (+personal research agent; python-httpx/feedparser)"
)
CLIENT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

_registry: dict[str, type["BaseCollector"]] = {}
_shared_client: httpx.AsyncClient | None = None


class CollectorError(RuntimeError):
    pass


def describe_error(exc: BaseException, url: str = "") -> str:
    """Transport exceptions often stringify to '' - keep class name and URL.

    Without this, logs/collector.log reads "arXiv query failed:" and the
    operator has nothing to act on.
    """
    detail = str(exc).strip() or type(exc).__name__
    prefix = f"{url} " if url else ""
    return f"{prefix}{type(exc).__name__}: {detail}"[:300]


# --------------------------------------------------------------------------
# Rate-limit backoff
#
# A 429 used to be retried two more times about half a second later, then
# re-attempted in full on the next round as if nothing had happened. VentureBeat
# AI logged 69 failures in six hours (~200 requests) for zero articles, and
# Reddit's "x-ratelimit-remaining: 0, reset in 31s" was ignored the same way.
# A source that tells us when to come back is now believed - and the ask is
# skipped entirely until then, so the budget goes to sources that answer.
# --------------------------------------------------------------------------
_cooldowns: dict[str, float] = {}          # host -> "do not ask before" (monotonic)
MIN_COOLDOWN = 30.0
MAX_COOLDOWN = 6 * 3600.0
DEFAULT_COOLDOWN = 30 * 60.0


class RateLimited(CollectorError):
    """The source asked us to stay away; retrying now would only dig us deeper."""

    def __init__(self, url: str, seconds: float) -> None:
        self.url = url
        self.seconds = seconds
        super().__init__(f"{url} -> HTTP 429，源服务器要求降速，已退避 {max(1, int(round(seconds / 60)))} 分钟")


def _host(url: str) -> str:
    try:
        return httpx.URL(url).host or url
    except Exception:  # pragma: no cover - malformed config URL
        return url


def _header_seconds(value: Any, *, now: float | None = None) -> float | None:
    """`Retry-After` and friends are either a number of seconds or an HTTP date.

    Reddit sends `x-ratelimit-remaining: 0.0`, so this parses floats, not just
    digits - `str.isdigit()` would drop the one value that matters most.
    Named `_header_seconds`, not `_seconds`: a helper with that name already
    converts httpx timeouts to plain seconds further down this module, and
    shadowing it silently turned every `Retry-After` into a default wait.
    """
    import math

    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        seconds = math.nan
    if math.isfinite(seconds):
        return seconds
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime

    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    reference = datetime.now(timezone.utc) if now is None else datetime.fromtimestamp(
        now, tz=timezone.utc)
    return (when - reference).total_seconds()


def _hdr(headers: Any, key: str) -> Any:
    """Case-insensitive header lookup that survives a plain dict.

    httpx gives case-insensitive `Headers`, but `BrowserResponse.headers` is a
    dict built from curl_cffi, which keeps the server's casing. The test double
    for it bit this once already.
    """
    if headers is None:
        return None
    try:
        value = headers.get(key)
    except (AttributeError, TypeError):
        value = None
    if value is not None:
        return value
    try:
        items = list(headers.items())
    except (AttributeError, TypeError):
        return None
    for name, got in items:
        if str(name).lower() == key.lower():
            return got
    return None


def backoff_seconds(headers: Any, *, default: float = DEFAULT_COOLDOWN) -> float:
    """How long this response asks us to stay away, clamped to sane bounds."""
    import time as _time

    seconds: float | None = _header_seconds(_hdr(headers, "retry-after"))
    if seconds is None:
        # Reddit and GitHub report "you are out" plus when the window rolls over;
        # the reset is epoch seconds there and a countdown on some proxies.
        remaining = _header_seconds(_hdr(headers, "x-ratelimit-remaining"))
        reset = _header_seconds(_hdr(headers, "x-ratelimit-reset"))
        if remaining == 0 and reset is not None:
            seconds = max(0.0, reset - _time.time()) if reset > 1e9 else reset
    if seconds is None:
        seconds = default
    return max(MIN_COOLDOWN, min(seconds, MAX_COOLDOWN))


def out_of_budget(headers: Any) -> bool:
    """True when a response says there is nothing left to spend on this host."""
    return _header_seconds(_hdr(headers, "x-ratelimit-remaining")) == 0


def cool_down(url: str, seconds: float) -> float:
    """Block `url`'s host for a while; returns the wait actually applied."""
    import time as _time

    seconds = max(MIN_COOLDOWN, min(seconds, MAX_COOLDOWN))
    _cooldowns[_host(url)] = _time.monotonic() + seconds
    return seconds


def cooling(url: str) -> float:
    """Seconds left before this host may be asked again (0 when it is free)."""
    import time as _time

    host = _host(url)
    until = _cooldowns.get(host)
    if until is None:
        return 0.0
    left = until - _time.monotonic()
    if left <= 0:
        _cooldowns.pop(host, None)
        return 0.0
    return left


def cooling_hosts() -> dict[str, float]:
    """What this process is parked on, host -> seconds left.

    Cooldowns live in memory and use the monotonic clock, so only the running
    service can see them; what reaches the operator is the error text, which the
    pipeline stores in `sources.last_error` and `/来源` prints.
    """
    import time as _time

    now = _time.monotonic()
    return {host: round(until - now) for host, until in _cooldowns.items() if until > now}


async def get_client() -> httpx.AsyncClient:
    """One pooled client for all collectors; the app closes it on shutdown."""
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=CLIENT_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*"},
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _shared_client


async def close_client() -> None:
    global _shared_client
    if _shared_client and not _shared_client.is_closed:
        await _shared_client.aclose()
    _shared_client = None


# --------------------------------------------------------------------------
# Browser-TLS transport (optional)
#
# Cloudflare and friends fingerprint the TLS handshake, not the headers, so a
# Python client can be rejected with 403 while `curl` on the same box and second
# gets 200. curl_cffi replays a real Chrome handshake; sources that need it set
# `browser_tls: true` in sources.yaml. Without the package we fall back to httpx
# and say so once - a dead source is recoverable, a crash on import is not.
# --------------------------------------------------------------------------
_browser_state: dict[str, Any] = {"warned": False, "session": None}


def browser_tls_available() -> bool:
    try:
        import curl_cffi  # noqa: F401
    except Exception:
        return False
    return True


class BrowserResponse:
    """The slice of httpx.Response that collectors actually touch."""

    def __init__(self, response: Any) -> None:
        self._response = response
        self.status_code = int(response.status_code)
        self.content = response.content or b""
        self.headers = dict(response.headers or {})

    @property
    def text(self) -> str:
        try:
            return str(self._response.text)
        except Exception:  # pragma: no cover - decoder differences across versions
            return self.content.decode("utf-8", "replace")

    def json(self) -> Any:
        import json as _json

        try:
            return self._response.json()
        except Exception:
            return _json.loads(self.text)


async def get_browser_session() -> Any:
    global _browser_state
    session = _browser_state.get("session")
    if session is not None:
        return session
    from curl_cffi.requests import AsyncSession

    proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or None
    session = AsyncSession(impersonate="chrome", timeout=20.0,
                           allow_redirects=True, proxy=proxy)
    _browser_state["session"] = session
    return session


async def close_browser_session() -> None:
    session = _browser_state.get("session")
    _browser_state["session"] = None
    if session is not None:
        try:
            await session.close()
        except Exception:  # pragma: no cover - shutdown must never raise
            log.debug("browser TLS session close failed", exc_info=True)


async def browser_get(url: str, *, params: dict[str, Any] | None = None,
                      headers: dict[str, str] | None = None,
                      timeout: float | None = None,
                      impersonate: str = "chrome") -> BrowserResponse:
    """One GET over a browser-shaped TLS handshake."""
    try:
        from curl_cffi.requests import AsyncSession  # noqa: F401
    except Exception as exc:
        if not _browser_state["warned"]:
            _browser_state["warned"] = True
            log.warning("browser_tls requested but curl_cffi is missing "
                        "(pip install curl_cffi); falling back to httpx: %s", exc)
        raise BrowserTLSUnavailable(str(exc)) from exc

    session = await get_browser_session()
    seconds = float(timeout) if isinstance(timeout, (int, float)) else 20.0
    response = await session.get(url, params=params, headers=headers or {}, timeout=seconds)
    return BrowserResponse(response)


class BrowserTLSUnavailable(RuntimeError):
    """curl_cffi is not installed, so a browser_tls source cannot be fetched."""


def _seconds(timeout: Any) -> float | None:
    """httpx.Timeout is useless to curl; reduce it to plain seconds."""
    if timeout is None or isinstance(timeout, (int, float)):
        return None if timeout is None else float(timeout)
    return float(getattr(timeout, "read", None) or getattr(timeout, "connect", None) or 20.0)


def register(cls: type["BaseCollector"]) -> type["BaseCollector"]:
    _registry[cls.type] = cls
    return cls


def known_types() -> list[str]:
    return sorted(_registry)


class BaseCollector:
    """fetch() yields raw items, collect() turns them into Article dicts."""

    type = "base"

    def __init__(self, source: dict[str, Any], config: AppConfig | None = None,
                 client: httpx.AsyncClient | None = None) -> None:
        self.source = source
        self.config = config or get_config()
        self._client = client
        self.name: str = clean_text(source.get("name") or self.type)

    # -- identity -------------------------------------------------------
    @property
    def source_name(self) -> str:
        return self.name

    @property
    def quality(self) -> str:
        return str(self.source.get("quality", "C"))

    async def http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = await get_client()
        return self._client

    # -- to implement ---------------------------------------------------
    async def fetch(self) -> list[dict[str, Any]]:
        """Return raw items: {title,url,author,content,published_at,meta}."""
        raise NotImplementedError

    def normalize(self, item: dict[str, Any]) -> dict[str, Any]:
        """Turn one raw item into the standard Article dict (section 9)."""
        return build_article(
            title=item.get("title"),
            url=item.get("url"),
            source_name=item.get("source_name") or self.name,
            source_type=self.type,
            author=item.get("author"),
            content=item.get("content"),
            published_at=item.get("published_at"),
            quality=self.quality,
            meta=item.get("meta") or {},
            community_heat=item.get("community_heat") or 0,
        )

    # -- shared plumbing ------------------------------------------------
    async def collect(self) -> list[dict[str, Any]]:
        items = await self.fetch()
        out: list[dict[str, Any]] = []
        for item in items or []:
            try:
                data = self.normalize(item)
            except Exception as exc:  # one malformed entry must not lose the feed
                log.warning("%s: cannot normalise item: %s", self.name, exc)
                continue
            if not data.get("url") or not data.get("title"):
                continue
            out.append(data)
        log.debug("%s produced %d article(s)", self.name, len(out))
        return out

    async def get(self, url: str, *, params: dict[str, Any] | None = None,
                  headers: dict[str, str] | None = None, attempts: int = 3,
                  timeout: float | None = None) -> httpx.Response:
        """GET with timeout, redirect following and exponential backoff.

        Transport failures become CollectorError carrying the URL, the exception
        class and its text, so a log line is always actionable even when httpx
        raises an empty-message error.
        """
        client = await self.http()
        merged = {"User-Agent": DEFAULT_USER_AGENT, **(self.source.get("headers") or {}), **(headers or {})}
        wants_browser = bool(self.source.get("browser_tls"))
        waiting = cooling(url)
        if waiting:
            # Raised outside the retry loop on purpose: this is not a failure to
            # retry, it is a scheduled skip, and the round should spend nothing.
            raise CollectorError(
                f"{url} -> 源服务器要求降速，还剩 {max(1, int(waiting / 60))} 分钟再试")
        last: Exception | None = None
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(max(1, attempts)),
                wait=wait_exponential(multiplier=0.8, min=0.5, max=8),
                retry=retry_if_exception(lambda exc: isinstance(exc, httpx.HTTPError)
                                         or (isinstance(exc, CollectorError)
                                             and not isinstance(exc, RateLimited))),
                reraise=True,
            ):
                with attempt:
                    try:
                        if wants_browser and browser_tls_available():
                            response = await browser_get(
                                url, params=params, headers=merged, timeout=_seconds(timeout),
                                impersonate=str(self.source.get("impersonate") or "chrome"))
                        else:
                            response = await client.get(url, params=params, headers=merged,
                                                        timeout=timeout or CLIENT_TIMEOUT)
                    except (httpx.HTTPError, BrowserTLSUnavailable) as exc:
                        last = exc
                        raise
                    except Exception as exc:  # curl_cffi raises its own hierarchy
                        last = CollectorError(describe_error(exc, url))
                        raise last
                    if response.status_code == 429:
                        wait = backoff_seconds(getattr(response, "headers", None),
                                               default=self.backoff())
                        cool_down(url, wait)
                        raise RateLimited(url, wait)
                    if response.status_code >= 500:
                        last = CollectorError(f"{url} -> HTTP {response.status_code}")
                        raise last
                    if response.status_code >= 400:
                        raise CollectorError(f"{url} -> HTTP {response.status_code}")
                    # A 200 that says "you have 0 left, resets in 31s" is a 429
                    # that has not happened yet; park the host before it does.
                    if out_of_budget(getattr(response, "headers", None)):
                        cool_down(url, backoff_seconds(response.headers, default=self.backoff()))
                    return response
        except (httpx.HTTPError, BrowserTLSUnavailable) as exc:
            raise CollectorError(describe_error(exc, url)) from exc
        raise CollectorError(describe_error(last or RuntimeError("request failed"), url))

    def backoff(self) -> float:
        """Per-source override for how long a 429 parks the host."""
        seconds = as_float(self.source.get("backoff_minutes"), DEFAULT_COOLDOWN / 60)
        return max(MIN_COOLDOWN, seconds * 60)

    async def get_text(self, url: str, **kwargs: Any) -> str:
        response = await self.get(url, **kwargs)
        return response.text

    # -- helpers subclasses use -----------------------------------------
    def wants_keyword(self, text: str, *, extra: Sequence[str] = ()) -> bool:
        """arXiv-style firehose sources must pass the AI keyword gate."""
        keywords = list(self.config.filter_keywords) + [k.lower() for k in extra]
        return bool(keyword_hits(text, keywords))

    def limit(self) -> int:
        return int(self.source.get("max_items", 50))

    def since(self, hours: int) -> datetime:
        from datetime import timedelta

        return datetime.utcnow() - timedelta(hours=hours)


def build_collectors(
    config: AppConfig | None = None,
    *,
    types: Iterable[str] | None = None,
    only_enabled: bool = True,
    names: Iterable[str] | None = None,
) -> list[BaseCollector]:
    """Instantiate every configured source whose type has a collector."""
    config = config or get_config()
    wanted_types = {t.lower() for t in types} if types else None
    wanted_names = {n.lower() for n in names} if names else None
    collectors: list[BaseCollector] = []
    for entry in config.sources:
        if only_enabled and not entry.get("enabled", True):
            continue
        source_type = str(entry.get("type", "rss")).lower()
        if wanted_types and source_type not in wanted_types:
            continue
        if wanted_names and str(entry.get("name", "")).lower() not in wanted_names:
            continue
        cls = _registry.get(source_type)
        if cls is None:
            log.warning("no collector implemented for type %r (source %r skipped)", source_type, entry.get("name"))
            continue
        collectors.append(cls(entry, config))
    return collectors
