"""Measured precision/recall for the /免费 detector.

Tuned detectors drift: every fix for one false positive quietly kills a real
recall case somewhere. This file is the counterweight - the labels live in
tests/data/free_eval.yaml and were taken from the live corpus.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.config import get_config
from app.processing.free_offers import detect

DATA = Path(__file__).parent / "data" / "free_eval.yaml"

# What the pricing gateway reported on 2026-09-25. `snapshot()` feeds these into
# the detector vocabulary, so a model that went free this week is recognisable
# in news text without anybody editing a YAML file.
GATEWAY_SAMPLE = {"data": [
    {"id": "stealth/space-bunny-alpha", "name": "Space Bunny Alpha", "created": 1758500000,
     "context_length": 131072, "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "z-ai/glm-5.2:free", "name": "Z.ai: GLM 5.2 (free)", "created": 1781631930,
     "context_length": 32768, "pricing": {"prompt": "0", "completion": "0"}},
]}


@pytest.fixture(autouse=True)
def _with_gateway_vocabulary(monkeypatch, tmp_path):
    """Run the real merge path the way a boot-time snapshot does it."""
    import asyncio

    from app.services.free_models import FreeModelWatcher

    async def fake_fetch(self):
        return GATEWAY_SAMPLE

    monkeypatch.setattr(FreeModelWatcher, "_fetch", fake_fetch)
    monkeypatch.setattr(FreeModelWatcher, "state_path",
                        property(lambda self: tmp_path / "free_models.json"))
    watcher = FreeModelWatcher(get_config())
    asyncio.run(watcher.snapshot(force=True))
    yield


def _items():
    return yaml.safe_load(DATA.read_text(encoding="utf-8"))["items"]


def _score():
    config = get_config()
    true_pos = false_pos = missed = 0
    misses, wrong_tool = [], []
    for item in _items():
        offer = detect(item["title"], "", config=config)
        expected = bool(item["offer"])
        if expected and offer is not None:
            true_pos += 1
            want = item.get("tool")
            if want and offer.tool != want:
                wrong_tool.append((item["title"], offer.tool, want))
        elif expected:
            missed += 1
            misses.append(item["title"])
        elif offer is not None:
            false_pos += 1
    total_pos = true_pos + missed
    precision = true_pos / (true_pos + false_pos) if (true_pos + false_pos) else 1.0
    recall = true_pos / total_pos if total_pos else 1.0
    return {"tp": true_pos, "fp": false_pos, "missed": missed,
            "precision": precision, "recall": recall,
            "misses": misses, "wrong_tool": wrong_tool}


def test_labels_are_a_usable_fixture():
    items = _items()
    assert sum(1 for i in items if i["offer"]) >= 10
    assert sum(1 for i in items if not i["offer"]) >= 10


def test_detector_precision_is_perfect_on_the_labelled_set():
    """A false promo in a push notification costs more than a missed one."""
    score = _score()
    assert score["fp"] == 0, f"false positives: {score['misses']}"
    assert score["precision"] == 1.0


def test_detector_recall_meets_the_agreed_floor():
    score = _score()
    assert score["recall"] >= 0.85, (
        f"recall {score['recall']:.2f}; missed: {score['misses']}")


def test_recall_is_measured_per_language():
    """An English-only regression must not hide behind a strong Chinese set."""
    def is_chinese(item):
        return any("一" <= ch <= "鿿" for ch in item["title"])

    for label, subset in (("chinese", [i for i in _items() if is_chinese(i)]),
                          ("english", [i for i in _items() if not is_chinese(i)])):
        config = get_config()
        pos = [i for i in subset if i["offer"]]
        neg = [i for i in subset if not i["offer"]]
        assert len(pos) >= 8 and len(neg) >= 8, f"{label} set is too small to mean anything"
        hits = [i for i in pos if detect(i["title"], "", config=config) is not None]
        false = [i for i in neg if detect(i["title"], "", config=config) is not None]
        assert not false, f"{label} false positives: {[i['title'] for i in false]}"
        assert len(hits) / len(pos) >= 0.85, (
            f"{label} recall {len(hits)}/{len(pos)}; missed {[i['title'] for i in pos if i not in hits]}")


@pytest.mark.parametrize("item", _items(), ids=lambda i: i["title"][:34])
def test_each_labelled_title(item):
    """Per-case view so a failure names the headline instead of a ratio."""
    offer = detect(item["title"], "", config=get_config())
    if item["offer"]:
        assert offer is not None, "labelled as a free offer but not detected"
        if item.get("tool"):
            assert offer.tool == item["tool"]
    else:
        assert offer is None, "labelled as not-an-offer but detected"
