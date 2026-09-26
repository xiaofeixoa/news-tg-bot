"""发送层：Telegram 说"不"的时候，简报到底算不算发出去了。

`app/bot/sender.py` 此前只覆盖三条 happy-ish 路径（HTML、markup 降级、其它 API 错误
不重试）。没测到的正是最容易在凌晨悄悄发生的那几种：被屏蔽、限流、网络抖动、
以及"两页简报只送到第一页"。最后一条会决定 `push_logs` 里记不记"今天已发"——
记了就等于当天不再重试，他收到的是一份缺尾巴的早报而没人知道。
"""

from __future__ import annotations

import pytest

from app.bot.sender import MAX_MESSAGE, TelegramSender
from app.config import get_config
from app.database.database import session_scope
from app.services.digest import Digest


class Bot:
    """按脚本逐条失败/成功的假 bot；记录每次调用。"""

    def __init__(self, errors=None):
        self.calls: list[dict] = []
        self.errors = list(errors or [])

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        error = self.errors.pop(0) if self.errors else None
        if error is not None:
            raise error

    class _Session:
        async def close(self):
            return None

    session = _Session()


def bad_request(text: str):
    from aiogram.exceptions import TelegramBadRequest
    return TelegramBadRequest(method="sendMessage", message=text)


def forbidden():
    from aiogram.exceptions import TelegramForbiddenError
    return TelegramForbiddenError(method="sendMessage", message="bot was blocked by the user")


def network():
    from aiogram.exceptions import TelegramNetworkError
    return TelegramNetworkError(method="sendMessage", message="connection reset")


def retry_after(seconds: int = 7):
    from aiogram.exceptions import TelegramRetryAfter
    return TelegramRetryAfter(method="sendMessage", message="too many requests",
                              retry_after=seconds)


async def test_a_blocked_chat_is_skipped_once_and_not_retried():
    bot = Bot([forbidden(), forbidden(), forbidden()])
    sender = TelegramSender(bot)          # type: ignore[arg-type]
    assert await sender.send(1, "早报") is False
    assert len(bot.calls) == 1, "被屏蔽是永久错误，重试三次只是白等"


async def test_flood_wait_pauses_for_what_telegram_asks_then_succeeds(monkeypatch):
    from app.bot import sender as sender_mod

    waits: list[float] = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(sender_mod.asyncio, "sleep", fake_sleep)
    bot = Bot([retry_after(7)])
    assert await TelegramSender(bot).send(1, "早报") is True   # type: ignore[arg-type]
    assert waits == [7.0] and len(bot.calls) == 2


async def test_flood_wait_is_capped_so_a_round_cannot_hang_forever(monkeypatch):
    """Telegram 要等 10 分钟时我们最多等 30 秒，剩下的交给下一轮。"""
    from app.bot import sender as sender_mod

    waits: list[float] = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(sender_mod.asyncio, "sleep", fake_sleep)
    bot = Bot([retry_after(600)])
    assert await TelegramSender(bot).send(1, "早报") is True      # type: ignore[arg-type]
    assert waits[0] == 30 and len(bot.calls) == 2


async def test_network_flaps_are_retried_and_a_recovery_is_reported_as_sent():
    bot = Bot([network(), network()])
    assert await TelegramSender(bot).send(1, "早报") is True      # type: ignore[arg-type]
    assert len(bot.calls) == 3


async def test_giving_up_after_the_last_attempt_leaves_a_log_line():
    """失败必须留下痕迹：过去"0 条推送"看起来像新闻少，其实是发送端静默失败。"""
    import app.bot.sender as sender_mod

    lines: list[str] = []
    original = sender_mod.log

    class Capture:
        def __getattr__(self, name):
            def fn(msg, *args):
                lines.append(str(msg) % args if args else str(msg))
            return fn

    sender_mod.log = Capture()                          # type: ignore[misc]
    try:
        bot = Bot([network(), network(), network()])
        assert await TelegramSender(bot).send(1, "早报") is False    # type: ignore[arg-type]
    finally:
        sender_mod.log = original
    assert any("giving up" in line for line in lines), lines


async def test_empty_text_never_reaches_the_api():
    bot = Bot()
    assert await TelegramSender(bot).send(1, "   ") is False        # type: ignore[arg-type]
    assert bot.calls == []


async def test_an_over_long_message_is_cut_with_a_mark_not_mid_sentence():
    bot = Bot()
    await TelegramSender(bot).send(1, "字" * (MAX_MESSAGE + 50))    # type: ignore[arg-type]
    sent = bot.calls[0]["text"]
    assert len(sent) == MAX_MESSAGE and sent.endswith("…"), "尾部要看得出来被截过"


# ------------------------------------------------------- 一份简报可能分几条消息
def digest_with(*messages: str) -> Digest:
    return Digest(kind="morning", messages=list(messages), date_label="2026-09-27",
                  article_ids=[1])


async def test_send_digest_counts_every_page_that_landed():
    bot = Bot()
    assert await TelegramSender(bot).send_digest(                 # type: ignore[arg-type]
        1, digest_with("第一页", "第二页")) == 2


async def test_a_half_delivered_digest_is_not_reported_as_delivered():
    """这就是那条会吃掉早报尾巴的 bug：sent=1 在 `if sent:` 里算成功。"""
    bot = Bot([None, bad_request("message is too long")])
    sender = TelegramSender(bot)                                  # type: ignore[arg-type]
    assert await sender.send_digest(1, digest_with("第一页", "第二页")) == 0, \
        "第二页没送到就不能说今天发过了"
    assert len(bot.calls) == 2, "第一页确实发出去了，只是不算成功"


async def test_the_watcher_records_delivery_only_for_a_whole_digest(session, monkeypatch):
    """闭环检查：send_digest 的返回值就是 `if sent:` 的依据，两层得一起测。"""
    from datetime import datetime, timedelta, timezone

    from app.database import repository as repo
    from app.scheduler.jobs import NewsJobs

    chat = 111111111
    with session_scope() as s:
        repo.get_or_create_user(s, chat, timezone="Asia/Shanghai")
        s.commit()

    jobs = NewsJobs(get_config())

    def payload_for(kind: str) -> Digest:
        # record_delivery 用的是 digest.kind，所以两栏各要一份自己的
        return Digest(kind=kind, messages=["第一页", "第二页"], date_label="2026-09-27",
                      article_ids=[1])

    async def fake_generate(kind, *, chat_id=None, llm=None):
        return payload_for(kind)

    monkeypatch.setattr(jobs.digest, "generate", fake_generate)
    monkeypatch.setattr(NewsJobs, "_digest_due", lambda *args: (True, "due"))

    def ledger_count() -> int:
        with session_scope() as s:
            user = repo.get_user(s, chat)
            return repo.pushes_since(s, user=user, kind="morning",
                                     since=datetime(2000, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None))

    class HalfSender:
        async def send_digest(self, chat_id, digest):
            return 0                      # 第二页没送到

    jobs._sender = HalfSender()
    await jobs.run_digests()
    assert ledger_count() == 0, "没送全就不该记『今天已发』，否则当天再也不会重试"

    class WholeSender:
        async def send_digest(self, chat_id, digest):
            return 2

    jobs._sender = WholeSender()
    await jobs.run_digests()
    assert ledger_count() == 1
