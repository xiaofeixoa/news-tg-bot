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


async def test_an_over_long_message_is_cut_on_a_line_break_not_inside_a_tag():
    """以前这一刀是 `text[:4095] + "…"`：正好能切进 `<b>…</b>` 中间。

    标签被切一半时 Telegram 回的是 can't parse，整条消息都发不出去——
    用户那边就是"我问了，没有回答"。旧用例的名字写着 not mid-sentence，
    断言却把 `len(sent) == MAX_MESSAGE` 钉死，等于替这刀背书。
    """
    from app.services import format as fmt

    bot = Bot()
    line = "<b>这一行讲的是一个很长的标题，里面有 NVIDIA 与 GPU 两个关键词</b>\n"
    await TelegramSender(bot).send(1, line * 100)                 # type: ignore[arg-type]
    sent = bot.calls[0]["text"]
    assert sent.endswith("…（内容过长已截断）"), sent[-40:]
    assert sent.count("<b>") == sent.count("</b>"), "不能在标签中间下刀"
    assert fmt.utf16_len(sent) <= MAX_MESSAGE, fmt.utf16_len(sent)
    assert sent.endswith(line.strip() + "\n…（内容过长已截断）") or "行" in sent


async def test_emoji_cannot_smuggle_a_message_past_the_limit():
    """Telegram 数的是 UTF-16 码元：🟢 一个字符占两个。"""
    from app.services import format as fmt

    bot = Bot()
    await TelegramSender(bot).send(1, "🟢" * 3000)                # type: ignore[arg-type]
    sent = bot.calls[0]["text"]
    assert fmt.utf16_len(sent) <= MAX_MESSAGE, fmt.utf16_len(sent)
    assert "内容过长已截断" in sent, sent[-30:]
    assert len(sent) < 3000, "字符数也该真的降下来，不是只把尾巴换成 …"


async def test_the_clipping_is_reported_in_the_log(monkeypatch):
    from app.bot import sender as sender_mod

    notes: list[str] = []
    monkeypatch.setattr(sender_mod.log, "warning",
                        lambda *a, **k: notes.append(str(a[0]) % a[1:] if a else str(a[0])))
    bot = Bot()
    await TelegramSender(bot).send(1, "行\n" * 4000)              # type: ignore[arg-type]
    assert any("exceeded the limit; clipping" in m for m in notes), notes


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


async def test_a_successful_send_leaves_a_line_the_audit_can_read(monkeypatch):
    """账本说"我们决定发"，日志说"Telegram 收了"：少一只眼睛就只剩一条腿的证据。

    `scripts/delivery_report.py` 是 grep `chat_id=` 的，成功路径不打日志的话，
    它对任何一次正常投递都只能回答"账上有"，答不出"对面收了"。
    """
    from app.bot import sender as module

    class Recorder:
        def __init__(self) -> None:
            self.records: list[tuple[str, tuple]] = []

        def _any(self, level):
            def emit(msg, *args):
                self.records.append((str(msg), args))
            return emit

        def __getattr__(self, level: str):
            return self._any(level)

    log = Recorder()
    monkeypatch.setattr(module, "log", log)
    sender = TelegramSender(Bot(), get_config())

    assert await sender.send(111111111, "早报正文") is True
    delivered = [(m, a) for m, a in log.records if "delivered" in m]
    assert len(delivered) == 1, delivered
    message, args = delivered[0]
    assert "chat_id=%s" in message and args[1] == 111111111
    assert args[0] == len("早报正文") and args[2] == "HTML"

    log.records.clear()
    bot = Bot(errors=[forbidden()])
    assert await TelegramSender(bot, get_config()).send(111111111, "发不出去") is False
    assert not [m for m, _ in log.records if "delivered" in m], "没送成不能留下送成的行"
