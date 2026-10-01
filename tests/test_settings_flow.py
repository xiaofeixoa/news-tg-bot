"""/设置 的按钮、/pause /resume /setinterest —— 他控制这个 bot 的全部入口。

`app/bot/handlers/settings.py` 之前 55%：没测的正好是按钮的处理函数本体
（`cb_settings`）。这类代码坏掉的形态不是报错，而是"我点了，面板没变"和
"我把早报关了，第二天早上它还是来了"——所以断言全部落在**库里那一行**和
**他眼睛看到的那一行文字**上。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest

from app.bot.handlers import settings as st
from app.bot.handlers.settings import (cb_settings, cmd_pause, cmd_resume,
                                       cmd_settings, cmd_setinterest)
from app.bot.keyboards import inline as K
from app.config import get_config
from app.database import repository as repo
from app.database.database import session_scope
from app.processing.normalize import build_article
from app.services.llm import LLMService
from app.services.news import NewsService

CHAT = 111111111


class Chat:
    def __init__(self, cid: int) -> None:
        self.id = cid


class Msg:
    """分开记"又发了一条"和"就地改写"：把面板叠成三份就是这个文件的第一号坏法。"""

    def __init__(self, cid: int = CHAT, text: str | None = None) -> None:
        self.chat = Chat(cid)
        self.text = text
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.edited: list[tuple[str, dict[str, Any]]] = []
        self.edit_raises: Exception | None = None

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.sent.append((text, kwargs))

    async def edit_text(self, text: str, **kwargs: Any) -> None:
        if self.edit_raises is not None:
            raise self.edit_raises
        self.edited.append((text, kwargs))

    @property
    def panel(self) -> str:
        return self.edited[-1][0] if self.edited else ""

    def last_text(self) -> str:
        return self.sent[-1][0] if self.sent else ""

    def button(self, data: str) -> str:
        """面板上某个按钮此刻的文字（最近一次渲染的）。"""
        renders = (self.edited or self.sent)[::-1]
        for _, kwargs in renders:
            markup = kwargs.get("reply_markup")
            for row in getattr(markup, "inline_keyboard", []):
                for button in row:
                    if button.callback_data == data:
                        return button.text or ""
        return ""


class Cb:
    def __init__(self, action: str, cid: int = CHAT) -> None:
        self.data = K.cb(K.ACT, action)
        self.message = Msg(cid)
        self.answers: list[tuple[str, dict[str, Any]]] = []

    async def answer(self, text: str | None = None, **kwargs: Any) -> None:
        self.answers.append((text or "", kwargs))

    @property
    def note(self) -> str:
        return self.answers[-1][0] if self.answers else ""


def news_service() -> NewsService:
    return NewsService(get_config())


def stored() -> dict[str, Any]:
    with session_scope() as s:
        user = repo.get_user(s, CHAT)
        assert user is not None, "按钮点下去必须落到具体的订阅者行上"
        return {"chat_id": user.telegram_chat_id, "daily_time": user.daily_time,
                "evening_time": user.evening_time, "paused": user.paused,
                "daily_on": user.daily_enabled, "evening_on": user.evening_enabled,
                "breaking": user.breaking_enabled, "min_score": user.min_score,
                "interests": [(i.type, i.value) for i in repo.interests_of(s, user)]}


def seed_scored(scores: list[float]) -> None:
    """入库并"处理完"若干条，只为了控制 final_score：门槛提示要数得准。"""
    with session_scope() as s:
        for index, score in enumerate(scores):
            article = repo.save_article(s, build_article(
                title=f"Model {index} sets a new record on the reasoning benchmark",
                url=f"https://example.org/{index}",
                source_name="OpenAI", source_type="rss",
                content=f"Model {index} details. " + ("The release improves AI agents. " * 6),
                published_at=datetime.now(timezone.utc).replace(tzinfo=None),
                quality="A",
            ))
            assert article is not None
            article.is_processed = True
            article.final_score = score
        s.commit()


# ------------------------------------------------------------------- buttons
@pytest.mark.asyncio
async def test_every_settings_button_moves_the_setting_it_claims_to():
    news = news_service()
    news.user_for(CHAT)
    before = stored()
    cases = {
        "daily": "daily_time", "evening": "evening_time",
        "daily_on": "daily_on", "evening_on": "evening_on",
        "breaking": "breaking", "score+": "min_score", "score-": "min_score",
        "pause": "paused",
    }
    for action, field in cases.items():
        cb = Cb(action)
        await cb_settings(cb, news, get_config())
        after = stored()
        assert after[field] != before[field], f"点 {action} 之后 {field} 没变"
        assert cb.message.edited, f"{action} 点完没有更新面板"
        assert cb.note, f"{action} 点完没有任何回话"
        before = after


@pytest.mark.asyncio
async def test_tapping_buttons_edits_one_panel_instead_of_stacking_stale_ones():
    """旧面板留着 = 留着一排写着假状态的开关，再点一下就把刚设好的改回去了。"""
    news = news_service()
    cb = Cb("daily")
    for _ in range(4):
        await cb_settings(cb, news, get_config())
    assert len(cb.message.edited) == 4
    assert cb.message.sent == [], "同一条面板应该就地改写，而不是又发一条"


@pytest.mark.asyncio
async def test_the_button_labels_track_the_state_they_toggle():
    news = news_service()
    cb = Cb("daily_on")
    await cb_settings(cb, news, get_config())
    assert stored()["daily_on"] is False
    assert "关" in cb.message.button("x:daily_on"), "按钮文字要说清现在是什么状态"
    assert "☀️ 早报：关" in cb.message.panel
    await cb_settings(cb, news, get_config())
    assert stored()["daily_on"] is True
    assert "🔔 早报提醒 开" == cb.message.button("x:daily_on")


@pytest.mark.asyncio
async def test_a_callback_with_no_message_writes_nothing_for_a_phantom_chat():
    news = news_service()
    news.user_for(CHAT)
    before = stored()
    cb = Cb("daily")
    cb.message = None  # Telegram 对匿名/过期回调会给 message=None
    await cb_settings(cb, news, get_config())
    assert "/settings" in cb.note
    assert stored() == before, "没有可写回的面板时，一条设置都不该改"
    with session_scope() as s:
        assert repo.get_user(s, 0) is None, "chat_id 缺省成 0 会凭空造出一个订阅者"


@pytest.mark.asyncio
async def test_a_rejected_panel_edit_still_saves_the_setting_and_answers():
    news = news_service()
    cb = Cb("pause")
    cb.message.edit_raises = TelegramBadRequest(method="editMessageText",
                                               message="message is not modified")
    await cb_settings(cb, news, get_config())
    assert stored()["paused"] is True
    assert "暂停" in cb.note


@pytest.mark.asyncio
async def test_daily_button_cycles_through_the_offered_times_only():
    news = news_service()
    seen = set()
    for _ in range(len(st.DAILY_SLOTS) + 2):
        await cb_settings(Cb("daily"), news, get_config())
        seen.add(stored()["daily_time"])
    assert seen == set(st.DAILY_SLOTS), "转一圈要把每个可选时间都走一遍，且不跑出档位"


def test_an_off_list_time_advances_to_the_next_slot_instead_of_jumping_back():
    assert st._next("07:15", st.DAILY_SLOTS) == "07:30"
    assert st._next("07:59", st.DAILY_SLOTS) == "08:00"
    assert st._next("07:00", st.DAILY_SLOTS) == "07:30"
    assert st._next("12:00", st.DAILY_SLOTS) == st.DAILY_SLOTS[0]
    assert st._next("23:30", st.EVENING_SLOTS) == st.EVENING_SLOTS[0]


# ----------------------------------------------------------------- 门槛提示
@pytest.mark.asyncio
async def test_raising_the_floor_says_how_many_items_are_left_in_the_window():
    seed_scored([50.0, 60.0, 70.0])
    news = news_service()
    news.user_for(CHAT)  # 默认 45
    cb = Cb("score+")
    await cb_settings(cb, news, get_config())
    assert stored()["min_score"] == 50.0
    assert "门槛 50" in cb.note and "3 条达标" in cb.note, cb.note


@pytest.mark.asyncio
async def test_the_floor_that_lets_nothing_in_warns_instead_of_going_quiet():
    """评分上限远高于 90，一路点 🔼 的代价要到早上才知道——所以点的时候就告诉他。"""
    seed_scored([50.0, 60.0, 70.0])
    news = news_service()
    for _ in range(9):
        cb = Cb("score+")
        await cb_settings(cb, news, get_config())
    assert stored()["min_score"] == 90.0
    assert "0 条达标" in cb.note and "收不到简报" in cb.note, cb.note
    for _ in range(6):
        cb = Cb("score-")
        await cb_settings(cb, news, get_config())
    assert stored()["min_score"] == 60.0
    assert "2 条达标" in cb.note and "⚠" not in cb.note, cb.note


# ------------------------------------------------------------------- 面板
@pytest.mark.asyncio
async def test_the_panel_reads_back_the_state_the_user_just_chose():
    news = news_service()
    await cb_settings(Cb("score+"), news, get_config())
    await cb_settings(Cb("evening"), news, get_config())
    msg = Msg()
    await cmd_settings(msg, news)
    user = stored()
    assert f"📊 最低评分：{user['min_score']:.0f}" in msg.last_text()
    assert user["evening_time"] in msg.last_text()
    assert "🌙 晚报：开" in msg.last_text()


@pytest.mark.asyncio
async def test_interest_words_are_escaped_or_the_whole_panel_gets_rejected():
    """兴趣词来自他打的那句话：一个 < 就能让 Telegram 拒掉整块面板。"""
    news = news_service()
    news.set_interests(CHAT, [{"type": "topic", "value": "GPT<b>", "weight": 1.0}])
    msg = Msg()
    await cmd_settings(msg, news)
    assert "GPT&lt;b&gt;" in msg.last_text()
    assert "<b>GPT" not in msg.last_text() and "GPT<b>" not in msg.last_text()


@pytest.mark.asyncio
async def test_the_panel_names_the_clock_in_chinese_not_with_an_iana_key():
    """/设置 上那句"（Asia/Shanghai）"是他设置页唯一说明按哪个钟的地方。"""
    from app.services import format as fmt

    news = news_service()
    msg = Msg()
    await cmd_settings(msg, news)
    assert "上海 UTC+8" in msg.last_text()
    assert "Asia/Shanghai" not in msg.last_text(), "内部标识符不该出现在他读的页面上"
    assert fmt.timezone_label("UTC") == "协调世界时 UTC+0"


def test_the_utc_offset_is_computed_from_the_calendar_not_hardcoded():
    from app.services import format as fmt

    # 半小时的区：只写整数小时会把加尔各答说成 UTC+5
    assert fmt.timezone_label("Asia/Kolkata") == "加尔各答 UTC+5:30"
    # 会调表的区：一年里两个偏移都可能出现，写死任何一个都会在半年里说谎
    assert fmt.timezone_label("America/Los_Angeles") in ("洛杉矶 UTC-8", "洛杉矶 UTC-7")
    assert fmt.timezone_label("Mars/Olympus") == "Mars/Olympus", "认不出的区原样回显，别编偏移"
    # 空名字不是"未知"：调度器与卡片的时间都按 UTC 渲染它，标签必须说同一件事
    assert fmt.timezone_label("") == "UTC+0"
    assert fmt.timezone_label(None) == "UTC+0"


# -------------------------------------------------------------- pause/兴趣
@pytest.mark.asyncio
async def test_pause_and_resume_survive_a_new_service_instance():
    news = news_service()
    await cmd_pause(Msg(), news)
    assert stored()["paused"] is True
    assert news_service().user_for(CHAT)["paused"] is True
    await cmd_resume(Msg(), news)
    assert stored()["paused"] is False


@pytest.mark.asyncio
async def test_setinterest_without_arguments_shows_usage_and_keeps_the_rest():
    news = news_service()
    news.set_interests(CHAT, [{"type": "topic", "value": "GPU", "weight": 1.0}])
    before = stored()
    msg = Msg()
    await cmd_setinterest(msg, st.CommandObject(command="setinterest", args=""), news,
                          LLMService(get_config()), get_config())
    assert "/setinterest" in msg.last_text()
    assert stored() == before, "只是问了个用法，不该顺手清空兴趣"


@pytest.mark.asyncio
async def test_setinterest_in_rule_mode_falls_back_to_keywords_and_replaces():
    """没有 LLM key 时这条路径就是唯一路径：它必须在规则下也能落库。"""
    news = news_service()
    news.set_interests(CHAT, [{"type": "topic", "value": "旧词", "weight": 1.0}])
    msg = Msg()
    await cmd_setinterest(msg, st.CommandObject(
        command="setinterest", args="我主要关注 AI Agent、开源模型、GPU 和 Claude"),
        news, LLMService(get_config()), get_config())
    values = {v for _, v in stored()["interests"]}
    assert values, "规则解析出的兴趣要写进库"
    assert "旧词" not in values, "/setinterest 是覆盖式的"
    assert "已记录" in msg.last_text()
    assert "按关键词" in msg.last_text(), "规则模式要承认自己是规则解析的"


@pytest.mark.asyncio
async def test_setinterest_admits_when_it_cannot_understand_the_sentence():
    news = news_service()
    news.user_for(CHAT)
    msg = Msg()
    await cmd_setinterest(msg, st.CommandObject(command="setinterest", args="嗯"), news,
                          LLMService(get_config()), get_config())
    assert "换个说法" in msg.last_text()
    assert stored()["interests"] == [], "识别不出来就不该留下半条兴趣"


def _push_breakings(count: int, chat: int = CHAT, *, days_ago: int = 0) -> None:
    """`days_ago=0` 落在读者今天的名额里；`=3` 是"昨天的 5 条不该继续挡今天"那一支。"""
    from datetime import timedelta

    with session_scope() as s:
        user = repo.get_or_create_user(s, chat)
        for _ in range(count):
            repo.record_push(s, user=user, kind="breaking")
        if days_ago:
            for p in s.query(repo.PushLog).filter(repo.PushLog.kind == "breaking").all():
                p.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_ago)
        s.commit()


@pytest.mark.asyncio
async def test_the_panel_shows_the_quota_that_is_actually_binding(session, monkeypatch):
    """2026-10-01 那天 `/设置` 只写"突发新闻：开"，而真实状态是 5/5 用光、当天不会再有突发。"""
    from datetime import timedelta

    news = news_service()
    with session_scope() as s:
        user = repo.get_or_create_user(s, CHAT)
        for _ in range(5):
            repo.record_push(s, user=user, kind="breaking")
        s.commit()
    msg = Msg()
    await cmd_settings(msg, news)
    text = msg.last_text()
    assert "突发新闻：开" in text, text
    assert "已用完 5/5" in text, text
    assert "当地 00:00" in text and "还可推送" not in text, text
    # 节奏也要说出来：他会问"为什么早上只来了一条"，答案是他自己配的 60 分钟
    assert "最快每 60 分钟一条" in text, text

    # 昨天的 5 条不该继续挡住今天：名额按读者当地日恢复
    with session_scope() as s:
        for p in s.query(repo.PushLog).filter(repo.PushLog.kind == "breaking").all():
            p.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        s.commit()
    later = Msg()
    await cmd_settings(later, news)
    assert "还可推送 5/5" in later.last_text(), later.last_text()


@pytest.mark.asyncio
async def test_a_reader_with_room_sees_the_remaining_count(session):
    news = news_service()
    _push_breakings(1)
    msg = Msg()
    await cmd_settings(msg, news)
    assert "还可推送 4/5" in msg.last_text(), msg.last_text()


@pytest.mark.asyncio
async def test_the_quota_follows_the_readers_own_day_and_the_configured_limit(session, monkeypatch):
    """两个方向：名额按**读者自己的**当地日恢复，而那个上限只能有一个读取处。

    同一个瞬间，上海读者的那条推送属于"今天"，UTC 读者的同一条属于"昨天"。
    闸门那边已经按当地日算（v1.65），面板若退化成 UTC 日界就会对一半的人说谎——
    两处数字来自两个"今天"，正是本项目反复犯的那一类。
    """
    from app.services import format as fmt

    news = news_service()
    instant = datetime(2026, 10, 1, 1, 0)          # UTC 01:00 = 上海 10-01 09:00
    push_at = datetime(2026, 9, 30, 20, 0)         # 两种"今天"在这条推送上分家
    monkeypatch.setattr("app.config._now_utc", lambda: instant)

    with session_scope() as s:
        sh = repo.get_or_create_user(s, CHAT, timezone="Asia/Shanghai")
        utc = repo.get_or_create_user(s, 222222222, timezone="UTC")
        for who in (sh, utc):
            entry = repo.record_push(s, user=who, kind="breaking")
            entry.created_at = push_at
        s.commit()

    assert news.breaking_quota(CHAT)["used"] == 1, "上海读者：这条属于他今天（当地 09-30 16:00 起）"
    assert news.breaking_quota(222222222)["used"] == 0, "UTC 读者：同一条属于他昨天"

    # 上限是配置里的数字，不是面板上写死的 5
    node = dict((news.config.raw.get("breaking") or {}))
    node["max_per_day"] = 2
    monkeypatch.setitem(news.config.raw, "breaking", node)
    quota = news.breaking_quota(CHAT)
    assert quota["limit"] == 2, quota
    # "还剩 1 / 共 2"两个数字都来自配置与账本，证明上限不是面板上写死的 5
    assert "还可推送 1/2 条" in fmt.quota_line(quota), fmt.quota_line(quota)


@pytest.mark.asyncio
async def test_a_stranger_sees_zero_and_nobody_else_s_ledger(session):
    """`pushes_since(user=None)` 的含义是"数所有人"，面板绝不能把它当成 0 传进去。"""
    news = news_service()
    _push_breakings(5, chat=222222222)
    quota = news.breaking_quota(333333333)
    assert quota["used"] == 0, quota
    assert quota["limit"] == 5, quota
    with session_scope() as s:
        assert repo.get_user(s, 333333333) is None, "看一眼设置面板不该往订阅表里插一行"
