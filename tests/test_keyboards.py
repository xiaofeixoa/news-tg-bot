"""键盘层的 callback_data 上限（Telegram 64 字节）此前只写在模块 docstring 里。

`/免费` 的工具名和 `/topics` 的分类都是从库里来的自由文本（检测器会从促销句子里
现编工具名，`free_offer_tool` 是 VARCHAR(64)——64 个汉字就是 192 字节）。一个超长值
的代价不是少一个按钮：Telegram 会拒收整个 reply_markup，那条命令从此不再回答，
直到那行数据被归档。
"""

from __future__ import annotations

import pytest

from app.bot.keyboards import inline as K


def datas(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard
            for button in row if getattr(button, "callback_data", None)]


def test_ordinary_keyboards_stay_well_under_the_cap():
    ids = list(range(1, 11))
    for markup in (K.news_list_keyboard(ids, page=3, total_pages=9),
                   K.article_keyboard(42, "https://example.com/a", back_tag="Open Source"),
                   K.topics_keyboard([{"category": "Open Source", "label": "开源",
                                       "count": 474, "emoji": "🧩"}]),
                   K.free_keyboard([{"tool": "Claude Code", "count": 3}], days=30)):
        for data in datas(markup):
            assert len(data.encode("utf-8")) <= K.CALLBACK_MAX_BYTES, data


def test_a_tool_name_too_long_for_a_button_is_left_out_not_sent():
    long_tool = "免费" * 30                      # 120 字节，远超 64
    markup = K.free_keyboard([{"tool": long_tool, "count": 5},
                              {"tool": "OpenRouter", "count": 1}], days=30)
    values = datas(markup)
    assert all(len(v.encode("utf-8")) <= K.CALLBACK_MAX_BYTES for v in values), values
    assert any(v.startswith("f:d:") for v in values), f"三档时间按钮不能被连坐：{values}"
    assert any(v == "f:t:OpenRouter" for v in values), values
    assert not any(long_tool in v for v in values), values


def test_the_cap_is_measured_in_bytes_not_characters():
    """20 个汉字 = 60 字节，放得下；21 个 = 63 + 前缀 4 = 67，放不下。"""
    fits = "一" * 14                            # 42 字节 + "f:t:" = 46
    too_big = "一" * 21                         # 63 字节 + "f:t:" = 67
    markup = K.free_keyboard([{"tool": fits, "count": 1}, {"tool": too_big, "count": 1}])
    values = datas(markup)
    assert f"f:t:{fits}" in values, values
    assert f"f:t:{too_big}" not in values, values


def test_a_dropped_button_is_reported(monkeypatch):
    notes: list[str] = []
    monkeypatch.setattr(K.log, "warning",
                        lambda *a, **k: notes.append(str(a[0]) % a[1:] if a else str(a[0])))
    K.free_keyboard([{"tool": "免费" * 40, "count": 1}], days=7)
    assert any("dropped: callback_data is" in m for m in notes), notes


def test_an_all_tools_too_long_keyboard_is_still_a_keyboard():
    markup = K.free_keyboard([{"tool": "字" * 40, "count": 1},
                              {"tool": "词" * 40, "count": 2}], days=90)
    assert markup.inline_keyboard, "不能返回空键盘——那会让整条消息没有按钮却看起来正常"
    assert all(row for row in markup.inline_keyboard), \
        f"空的按钮行也会被 Telegram 拒：{markup.inline_keyboard}"
    assert all(len(v.encode("utf-8")) <= K.CALLBACK_MAX_BYTES for v in datas(markup))


def test_a_long_category_does_not_take_the_topics_message_down():
    markup = K.topics_keyboard([{"category": "分类" * 30, "label": "怪东西",
                                 "count": 1, "emoji": "🫥"},
                                {"category": "AI Models", "label": "模型",
                                 "count": 349, "emoji": "🧠"}])
    values = datas(markup)
    assert "t:AI Models" in values, values
    assert any(v == "b:news" for v in values), f"回主页那一行必须还在：{values}"
    assert all(len(v.encode("utf-8")) <= K.CALLBACK_MAX_BYTES for v in values)


def test_the_deep_button_is_not_offered_when_no_llm_is_configured():
    """没配 key 时点它只会重发同一张卡片——那就别摆一个会说谎的按钮。"""
    from app.bot.keyboards import inline as K

    off = K.article_keyboard(7, "https://example.com/a", deep_available=False)
    on = K.article_keyboard(7, "https://example.com/a")
    off_data = [b.callback_data for row in off.inline_keyboard for b in row
                if getattr(b, "callback_data", None)]
    on_data = [b.callback_data for row in on.inline_keyboard for b in row
               if getattr(b, "callback_data", None)]
    assert "d:7" not in off_data, off_data
    assert "d:7" in on_data, on_data
    assert any(v == "b:news" for v in off_data), f"返回按钮不能被连坐：{off_data}"


def test_the_summary_button_is_offered_only_when_there_is_a_deeper_view_to_return_to():
    """屏幕上已经是常规摘要时，「📄 常规摘要」点下去只是重发同一条消息（v1.78）。"""
    off = K.deep_keyboard(7, "https://example.com/a", summary_available=False)
    on = K.deep_keyboard(7, "https://example.com/a")
    off_data = [b.callback_data for row in off.inline_keyboard for b in row
                if getattr(b, "callback_data", None)]
    on_data = [b.callback_data for row in on.inline_keyboard for b in row
               if getattr(b, "callback_data", None)]
    assert "a:7" not in off_data, off_data
    assert "a:7" in on_data, on_data
    off_texts = [b.text for row in off.inline_keyboard for b in row]
    assert not any("常规摘要" in t for t in off_texts), off_texts
    assert any("阅读原文" in t for t in off_texts), f"链接按钮无害，不该被连坐：{off_texts}"
    assert any(v == "b:news" for v in off_data), off_data
