"""Scoring tests: configurable weights, source tiers, heat, novelty, interests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_config
from app.processing import scorer


def make(title: str = "OpenAI releases a new reasoning model", *, meta=None, hours: int = 2) -> dict:
    return {
        "title": title,
        "content": "The model improves benchmarks and cuts inference price.",
        "source_name": "OpenAI",
        "meta": meta or {},
        "published_at": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours),
    }


def test_weights_sum_to_one_and_are_read_from_yaml():
    weights = get_config().scoring_weights
    assert set(weights) >= {"importance", "relevance", "novelty", "source_quality", "community_heat"}
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-6)
    assert weights["importance"] == pytest.approx(0.35)


def test_weighted_formula_matches_the_document():
    config = get_config()
    article = make(meta={"points": 300})
    scores = scorer.compute_scores(article, importance=80, relevance=70, novelty_value=60,
                                   quality="A", interests=[], config=config)
    w = config.scoring_weights
    expected = (80 * w["importance"] + 70 * w["relevance"] + 60 * w["novelty"]
                + scores["source_quality"] * w["source_quality"]
                + scores["community_heat"] * w["community_heat"])
    assert scores["community_heat"] > 0
    assert scores["final_score"] == pytest.approx(expected, abs=1.0)


def test_missing_community_signal_renormalises_the_other_weights():
    """Official blogs have no upvote count; losing 10% for that is wrong."""
    config = get_config()
    scores = scorer.compute_scores(make(meta={}), importance=100, relevance=100, novelty_value=100,
                                   quality="A", config=config)
    w = config.scoring_weights
    raw = 100 * (w["importance"] + w["relevance"] + w["novelty"]) + 95 * w["source_quality"]
    renormalised = raw / (1 - w["community_heat"])
    assert scores["final_score"] == pytest.approx(renormalised, abs=1.0)
    assert scores["weights_renormalised"] == 1.0
    assert scores["final_score"] >= config.get("breaking.threshold", 90) - 1


def test_changing_weights_changes_the_result_without_touching_code(monkeypatch):
    config = get_config()
    before = scorer.compute_scores(make(), importance=90, relevance=10, novelty_value=10,
                                   quality="C", config=config)["final_score"]
    monkeypatch.setitem(config.raw["scoring"]["weights"], "importance", 0.0)
    monkeypatch.setitem(config.raw["scoring"]["weights"], "relevance", 0.90)
    after = scorer.compute_scores(make(), importance=90, relevance=10, novelty_value=10,
                                  quality="C", config=config)["final_score"]
    assert after < before


@pytest.mark.parametrize("tier,expected", [("A", 95), ("B", 80), ("C", 65), ("D", 45)])
def test_source_credibility_tiers(tier, expected):
    assert scorer.source_quality(quality=tier) == expected


@pytest.mark.parametrize("meta,minimum", [
    ({"points": 0}, 0),
    ({"points": 100}, 55),
    ({"points": 5000}, 95),
    ({"stars": 2000}, 60),
    ({"upvotes": 800, "comments": 200}, 85),
])
def test_community_heat_is_squashed_into_range(meta, minimum):
    heat = scorer.community_heat({"meta": meta})
    assert minimum <= heat <= 100


def test_novelty_decays_with_age():
    assert scorer.novelty(1) > scorer.novelty(24) > scorer.novelty(72) > scorer.novelty(24 * 30)


def test_interests_boost_and_exclusions_penalise():
    article = make("New MCP server standard for AI agents")
    liked = scorer.compute_scores(article, importance=60, relevance=60, novelty_value=60,
                                  quality="B", interests=[{"type": "topic", "value": "mcp", "weight": 1.0}])
    ignored = scorer.compute_scores(article, importance=60, relevance=60, novelty_value=60, quality="B")
    assert liked["final_score"] > ignored["final_score"]

    excluded = scorer.compute_scores(article, importance=60, relevance=60, novelty_value=60, quality="B",
                                     interests=[{"type": "exclude", "value": "mcp", "weight": 1.0}])
    assert excluded["final_score"] < ignored["final_score"]


def test_scores_never_leave_zero_to_hundred():
    scores = scorer.compute_scores(make(), importance=1000, relevance=1000, novelty_value=1000,
                                   quality="A", interests=[{"type": "topic", "value": "model", "weight": 1.0}])
    assert 0 <= scores["final_score"] <= 100


def test_breaking_threshold_is_a_gate_not_a_coin_flip():
    assert scorer.is_breaking({"final_score": 93}, 90)
    assert not scorer.is_breaking({"final_score": 71}, 90)


def test_importance_falls_back_to_relevance_when_the_model_gives_nothing():
    """A story with no importance signal must not ride on source quality alone."""
    scores = scorer.compute_scores(make(), importance=0, relevance=90, novelty_value=90, quality="A")
    assert scores["importance_score"] > 0
    low = scorer.compute_scores(make(), importance=0, relevance=5, novelty_value=5, quality="A")
    assert low["final_score"] < scores["final_score"]


def test_low_credibility_sources_are_capped_below_breaking():
    """A trending repo is a lead, not an announcement (design doc section 40)."""
    threshold = float(get_config().get("breaking.threshold", 90))
    scores = {
        tier: scorer.compute_scores(make(), importance=100, relevance=100, novelty_value=100,
                                    quality=tier)["final_score"]
        for tier in ("A", "B", "C", "D")
    }
    assert scores["A"] >= scores["B"] > scores["C"] > scores["D"]
    assert scores["C"] < threshold and scores["D"] < threshold, "community noise cannot be breaking"
    assert not scorer.is_breaking({"final_score": scores["C"]}, threshold)


def test_tier_cap_is_configurable():
    config = get_config()
    assert scorer.tier_cap("C", config) == config.get("sources_quality.tier_caps.C")
    assert scorer.tier_cap(None, config) <= 100


# --------------------------------------------------- 突发：热度破例（v1.22）
def hn_row(**over):
    """线上真实形状：OpenAI agent 黑进政府那条，heat 645、评分 76、来源质量 65。"""
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    base = {
        "source_name": "Hacker News",
        "source_quality": 65,
        "final_score": 76,
        "community_heat": 645,
        "title": "Revealing the details of how OpenAI agents hacked Hugging Face",
        "published_at": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=30),
    }
    base.update(over)
    return SimpleNamespace(**base)


def test_community_heat_rescues_a_real_event_from_a_community_surface():
    from app.processing import breaking

    ok, why = breaking.gate(hn_row(), config=get_config(), ai_enabled=False)
    assert ok, f"heat 645 的大事件不该只因为来源是 HN 就被挡：{why}"
    assert "热度" in why


def test_heat_does_not_buy_in_without_an_event():
    from app.processing import breaking

    ok, why = breaking.gate(hn_row(title="Show HN: an agent framework I built"),
                            config=get_config(), ai_enabled=False)
    assert not ok and "no event" in why


def test_heat_does_not_buy_in_when_the_news_is_old():
    from datetime import datetime, timedelta, timezone

    from app.processing import breaking

    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=48)
    ok, why = breaking.gate(hn_row(published_at=old), config=get_config(), ai_enabled=False)
    assert not ok and "older than" in why


def test_a_blocklisted_source_stays_blocklisted_however_hot():
    from app.processing import breaking

    ok, why = breaking.gate(hn_row(source_name="Reddit LocalLLaMA RSS", community_heat=9000),
                            config=get_config(), ai_enabled=False)
    assert not ok and "not a publisher" in why


def test_heat_below_the_bar_still_needs_a_first_hand_source():
    from app.processing import breaking

    bar = float(get_config().get("breaking.rule.min_community_heat"))
    ok, why = breaking.gate(hn_row(community_heat=bar - 1), config=get_config(), ai_enabled=False)
    assert not ok and "source quality" in why


def test_the_settings_text_describes_the_gate_that_is_actually_in_force():
    from app.processing import breaking

    text = breaking.describe(get_config(), ai_enabled=False)
    assert "热度" in text and "250" in text, f"/设置 说的门必须就是代码里的门：{text}"
