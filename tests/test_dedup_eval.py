"""跨来源去重评测：tests/data/dedup_eval.yaml 里的 25 对线上真实标题。

判分方式与栏目评测一样：`same: true` 的对必须全部合并，`same: false` 的一对都不能合并。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.config import get_config
from app.processing import deduplicate as dd

PAIRS = yaml.safe_load(Path("tests/data/dedup_eval.yaml").read_text(encoding="utf-8"))


def merged_pairs() -> tuple[list, list, list]:
    cfg = get_config()
    yes, no, wrong = [], [], []
    for pair in PAIRS:
        same, score = dd.is_same_headline(pair["a"], pair["b"], cfg)
        (yes if same else no).append((score, pair))
        if same != pair["same"]:
            wrong.append((pair["same"], round(score, 3),
                          dd.shared_content_terms(pair["a"], pair["b"]), pair["a"][:44], pair["b"][:44]))
    return yes, no, wrong


def test_every_real_duplicate_pair_merges():
    _yes, _no, wrong = merged_pairs()
    missed = [w for w in wrong if w[0] is True]
    assert not missed, "同一件事没被合并：\n" + "\n".join(str(m) for m in missed)


def test_no_different_story_pair_merges():
    """合并错的代价比漏合并大：它会把一条真新闻从简报里彻底删掉。"""
    _yes, _no, wrong = merged_pairs()
    merged_wrong = [w for w in wrong if w[0] is False]
    assert not merged_wrong, "不同的事被错并：\n" + "\n".join(str(m) for m in merged_wrong)


def test_codex_lookalike_stays_separate():
    """最高分的假阳性：只共享 "codex" 一个词，相似度却打到 0.70。"""
    same, score = dd.is_same_headline("0.157.1 released in openai/codex", "两个codex邀请码自取", get_config())
    assert not same and 0.6 <= score < 0.79
    assert len(dd.shared_content_terms("0.157.1 released in openai/codex", "两个codex邀请码自取")) < 3


def test_rule_can_be_switched_off_without_breaking_the_confident_path():
    cfg = get_config()
    original = cfg.raw.setdefault("dedup", {}).get("rule_merge_shared_terms")
    cfg.raw["dedup"]["rule_merge_shared_terms"] = 0
    try:
        same, _score = dd.is_same_headline(
            "Australia to investigate if OpenAI hack of government health website broke the law",
            "Australia says OpenAI agent hacked into government website", cfg)
        assert not same, "关掉规则后灰区不再自动合并（回到只靠 AI 的行为）"
        again, _ = dd.is_same_headline("Gemini 3.8 text-to-speech says hello", "Gemini 3.8 text-to-speech", cfg)
        assert again, "高分路径不受开关影响"
    finally:
        cfg.raw["dedup"]["rule_merge_shared_terms"] = original
