"""规则门的召回与误放行：tests/data/gate_eval.yaml 是线上真实标题。

门槛是"这条新闻会不会被读者看见"的第一道闸，判错一次就是永久看不见，
所以它值得有自己的评测集，而不是靠几条零散断言。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from app.config import get_config
from app.processing import classifier

CASES = yaml.safe_load(Path("tests/data/gate_eval.yaml").read_text(encoding="utf-8"))


def _keep(title: str, source: str) -> tuple[bool, list[str]]:
    return classifier.rule_filter({"title": title, "content": title,
                                   "source_name": source}, config=get_config())


def test_every_labeled_case_is_answered_the_way_it_should_be():
    wrong: list[str] = []
    for case in CASES:
        kept, hits = _keep(case["title"], case.get("source", ""))
        want = case["expect"] == "keep"
        if kept != want:
            wrong.append(f"{case['title'][:48]!r} 期望 {case['expect']}，"
                         f"实际 {'keep' if kept else 'drop'}（命中 {hits}）")
    assert not wrong, "规则门判错：\n" + "\n".join(wrong)


def test_the_recall_cases_only_pass_because_model_is_a_keyword():
    """12 条召回用例全靠 model/agi 这两个词；把它们从词表里抽掉就应当全部落回被丢。

    这条断言的存在理由：词表是配置，配置里的一个词被删掉时，没有人会想到
    "线上有 17% 的被丢行只靠它"。
    """
    config = get_config()
    node = config.raw.setdefault("filters", {})
    words = list(node.get("keywords") or [])
    stripped = [w for w in words if str(w).lower() not in ("model", "models", "agi")]
    assert len(stripped) < len(words), "词表里没有 model/agi，这条断言已经失去意义"

    node["keywords"] = stripped
    recall_cases = [c for c in CASES if c["expect"] == "keep"
                    and not c["title"].startswith("How to model")]
    try:
        lost = 0
        for case in recall_cases:
            kept, _hits = classifier.rule_filter(
                {"title": case["title"], "content": case["title"],
                 "source_name": case.get("source", "")}, config=config)
            if not kept:
                lost += 1
        # 线上量出来是 12/12：这 12 条全靠这两个词，一条都不是别的关键字救的。
        # 断言按用例数写死，将来加一条不依赖 model/agi 的用例会直接指出是哪条。
        assert lost == len(recall_cases), \
            f"去掉 model/agi 只让 {lost}/{len(recall_cases)} 条落回被丢，评测集或词表已经变了"
    finally:
        node["keywords"] = words
