"""Rate-limit backoff: a source that says "come back later" is believed.

Regression cover for VentureBeat AI, which logged 69 failures in six hours
(~200 requests, three per round) for zero articles because every 429 was
retried immediately and then re-attempted in full on the next round.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from app.collectors.base import (CollectorError, MAX_COOLDOWN, RateLimited, BaseCollector,
                                 backoff_seconds, cooling, out_of_budget)

URL = "https://venturebeat.com/category/ai/feed/"


class PlainHeaders(dict):
    """A plain dict keeps the server's casing, like BrowserResponse.headers."""


class Resp:
    def __init__(self, status: int = 200, headers: dict | None = None, text: str = "<rss/>") -> None:
        self.status_code = status
        self.headers = PlainHeaders(headers or {})
        self.text = text
        self.content = text.encode()


class StubClient:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    async def get(self, url, **kwargs):
        self.calls.append(url)
        got = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(got, Exception):
            raise got
        return got


@pytest.fixture(autouse=True)
def _clear_cooldowns():
    from app.collectors import base
    base._cooldowns.clear()
    yield
    base._cooldowns.clear()


def collector(*responses, **source) -> tuple[BaseCollector, StubClient]:
    client = StubClient(*responses)
    return BaseCollector({"name": "VentureBeat AI", "type": "rss", **source}, client=client), client


async def test_a_429_is_asked_once_instead_of_three_times():
    collector_, client = collector(Resp(429, {"Retry-After": "120"}))
    with pytest.raises(RateLimited):
        await collector_.get(URL, attempts=3)
    assert len(client.calls) == 1, "backing off cannot mean hitting them again"
    assert cooling(URL) == pytest.approx(120, abs=2)


async def test_the_whole_host_is_skipped_until_the_wait_is_over():
    collector_, client = collector(Resp(429, {"Retry-After": "120"}), Resp(200))
    with pytest.raises(RateLimited):
        await collector_.get(URL, attempts=2)
    with pytest.raises(CollectorError) as exc:
        await collector_.get(URL, attempts=2)
    assert len(client.calls) == 1, "the second round must not spend a request"
    assert "分钟" in str(exc.value)

    second = BaseCollector({"name": "other", "type": "rss"}, client=client)
    assert await second.get("https://www.v2ex.com/index.xml")      # other hosts unaffected
    assert len(client.calls) == 2


async def test_zero_budget_on_a_200_parks_the_host_before_the_429_lands():
    """Reddit answers 200 with `x-ratelimit-remaining: 0.0` and a 31s reset."""
    collector_, client = collector(Resp(200, {"X-RateLimit-Remaining": "0.0",
                                              "X-RateLimit-Reset": "31"}))
    response = await collector_.get("https://www.reddit.com/r/LocalLLaMA/hot/.rss")
    assert response.status_code == 200, "the data we did get must still be used"
    assert out_of_budget(response.headers)
    assert cooling("https://www.reddit.com/r/LocalLLaMA/hot/.rss") == pytest.approx(31, abs=2)


def test_retry_after_accepts_seconds_and_an_http_date():
    assert backoff_seconds({"retry-after": "120"}) == 120
    when = (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime(
        "%a, %d %b %Y %H:%M:%S GMT")
    assert math.isclose(backoff_seconds(PlainHeaders({"Retry-After": when})), 600, abs_tol=10)
    assert backoff_seconds({}, default=900) == 900
    assert backoff_seconds(None, default=900) == 900


def test_a_silly_retry_after_cannot_park_a_source_for_a_week():
    assert backoff_seconds({"retry-after": "99999999"}) == MAX_COOLDOWN
    assert backoff_seconds({"retry-after": "1"}) == 30, "sub-minute waits are just noise"


async def test_server_errors_are_still_retried_within_the_round():
    """5xx is transient in a way a 429 is not; do not merge the two paths."""
    collector_, client = collector(Resp(503), Resp(503), Resp(200))
    response = await collector_.get(URL, attempts=3)
    assert response.status_code == 200
    assert len(client.calls) == 3
    assert cooling(URL) == 0


async def test_a_source_can_ask_for_its_own_wait():
    collector_, client = collector(Resp(429), backoff_minutes=1)
    with pytest.raises(RateLimited):
        await collector_.get(URL, attempts=2)
    assert cooling(URL) == pytest.approx(60, abs=3)
