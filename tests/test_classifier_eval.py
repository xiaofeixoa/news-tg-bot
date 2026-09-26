"""Rule-mode 分类准确率：tests/data/category_eval.yaml 是线上真实标题。

这里刻意只用标题打分（不带正文），因为栏目归属最该由标题决定；
线上那 37% 的分错，一半是正文里几个词把分数带跑偏了。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.config import get_config
from app.processing import classifier

CASES = yaml.safe_load(Path("tests/data/category_eval.yaml").read_text(encoding="utf-8"))


def score_cases(cases) -> tuple[int, int, list[str]]:
    config = get_config()
    wrong: list[str] = []
    for case in cases:
        predicted, _sub, _confidence = classifier.rule_classify(
            {"title": case["title"], "content": "", "source_name": case.get("source", "")},
            config,
        )
        expected = case["expect"] if isinstance(case["expect"], list) else [case["expect"]]
        if predicted not in expected:
            wrong.append(f"{case['title'][:52]!r} -> {predicted!r} 期望 {expected}")
    return len(cases) - len(wrong), len(cases), wrong


def test_rule_classification_hits_the_live_headlines():
    correct, total, wrong = score_cases(CASES)
    # 2026-09-26 实测：改动前 18/49（36.7%），改动后 49/53（92.5%）。
    # 门槛定 0.88 而非 0.92，留出余量，也免得剩下几条真分错的标题被无声忽略。
    assert correct / total >= 0.88, f"{correct}/{total} 命中，错因：\n" + "\n".join(wrong[:12])


@pytest.mark.parametrize("source", ["GitHub Trending", "arXiv AI", "NVIDIA Developer", "AWS ML"])
def test_no_source_family_is_entirely_misfiled(source):
    """The 2026-09-26 symptom: every GitHub Trending repo landed in some other bucket.

    A family being *totally* wrong means the source signal is not reaching the
    decision at all - a different bug from one headline being arguable.
    """
    picked = [c for c in CASES if c.get("source") == source]
    assert len(picked) >= 3, f"评测集里 {source} 只有 {len(picked)} 条"
    correct, total, wrong = score_cases(picked)
    assert correct > 0, f"{source} 全部 {total} 条分错：{wrong[:3]}"
