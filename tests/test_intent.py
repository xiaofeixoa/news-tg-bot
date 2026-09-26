"""规则模式下的意图判定：没有 LLM 时，这些说法必须由本机自己认出来。

`rule_intent` 是键-less 部署里唯一的理解层，判错一个意思就等于把用户推到死路上
（"最近 AI Agent 有什么值得关注的？" 曾经被判成设置类问题，只回一句"请用 /settings"）。
"""

from __future__ import annotations

import pytest

from app.services.search import rule_intent

CASES = [
    # 他真实会问的新闻问题 -> 必须是 search/latest
    ("最近 AI Agent 有什么值得关注的？", {"search", "latest"}),
    ("最近有什么大模型发布", {"search", "latest"}),
    ("今天 OpenAI 有什么新闻", {"search", "latest"}),
    ("本周有哪些融资", {"search", "latest"}),
    ("NVIDIA 的新显卡怎么样", {"search", "latest"}),
    ("有没有关于 Claude 的消息", {"search", "latest"}),
    ("开源模型哪些值得跑", {"search", "latest"}),
    # 明确在改设置 -> settings
    ("我想关注 NVIDIA 和大模型", {"settings"}),
    ("把推送时间改成早上七点", {"settings"}),
    ("暂停推送", {"settings"}),
    ("我的兴趣设置一下", {"settings"}),
    # 其它意图
    ("我现在都订阅了哪些来源", {"sources"}),
    ("数据源状态如何", {"sources"}),
    ("你能做什么", {"help"}),
    ("帮我看看第二条的详细分析", {"summarize"}),
]


@pytest.mark.parametrize("text,expected", CASES)
def test_rule_intent_reads_the_question_as_the_user_means_it(text, expected):
    got = rule_intent(text, []).get("intent")
    assert got in expected, f"{text!r} -> {got!r}，期望其中之一 {sorted(expected)}"


def test_question_wins_over_the_settings_keyword():
    """同一个词在问句和陈述里意思相反，问句必须优先。"""
    assert rule_intent("有什么值得关注的？", [])["intent"] in {"search", "latest"}
    assert rule_intent("关注具身智能", [])["intent"] == "settings"
