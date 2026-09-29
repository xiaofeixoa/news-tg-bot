"""中文不变量：他的屏幕上不该出现内部键名。

这些用例是从 2026-09-29 一次人工审计固化来的：把每个渲染面都喂一遍"内容已是中文"的
数据，检查输出的 chrome（栏目名、来源类型、标签）里有没有英文内部键。人工看过一次就
会忘，所以做成断言。
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest

from app.config import get_config
from app.services import format as fmt
from app.services.news import ArticleView

UNKNOWN_CATEGORY = "ai-research-internal"
UNKNOWN_SOURCE_TYPE = "newsletter"


def make_view(**kw) -> ArticleView:
    base = dict(id=1, title="An English headline", title_zh="一条中文标题",
                url="https://example.com/a", source_name="TechCrunch AI",
                source_type="rss", category="AI Models", subcategory="GPT",
                summary="English summary.", summary_zh="中文摘要一句。",
                key_points=["English bullet."],
                meta={"key_points_zh": ["第一条中文要点。"]},
                final_score=72.5, published_at=datetime(2026, 9, 29, 6, 0))
    base.update(kw)
    return ArticleView(**base)


def rendered_surfaces(item: ArticleView) -> dict[str, str]:
    cfg = get_config()
    return {
        "news_list": fmt.news_list([item], config=cfg, title="最新新闻", show_scores=True),
        "section_blocks": "\n".join(fmt.section_blocks([item], config=cfg)),
        "article_card": fmt.article_card(item, config=cfg),
        "breaking_card": fmt.breaking_card(item, config=cfg),
        "free_offer_list": fmt.free_offer_list([item], config=cfg),
    }


def test_an_unknown_category_never_prints_its_internal_key():
    """未登记的分类键是 chrome，不该印出来——他现在看到的每个栏目名都必须是中文。"""
    item = make_view(category=UNKNOWN_CATEGORY)
    surfaces = rendered_surfaces(item)
    for name, text in surfaces.items():
        assert UNKNOWN_CATEGORY not in text, f"{name} 把内部键直接印给了他：{text[:160]!r}"
    cfg = get_config()
    assert cfg.category_label(UNKNOWN_CATEGORY) == "其他"
    assert cfg.category_label(None) == "其他"


def test_known_categories_keep_their_chinese_labels():
    """8 个线上在用的分类各自的中文名，防止有人改 categories.yaml 时改错。"""
    cfg = get_config()
    expect = {"Open Source": "开源生态", "Other": "其他", "AI Models": "模型发布",
              "Research": "论文与方法", "AI Agent": "智能体", "AI Infrastructure": "算力与推理",
              "AI Applications": "产品与应用", "Companies": "公司动态"}
    got = {key: cfg.category_label(key) for key in expect}
    assert got == expect, f"栏目中文名变了：{got}"


def test_an_unknown_source_type_is_not_printed_raw():
    """/sources 里 `newsletter` 这种内部取值不该原样给他看。"""
    cfg = get_config()
    assert cfg.source_type_label(UNKNOWN_SOURCE_TYPE) == "其它来源"
    assert cfg.source_type_label(None) == "其它来源"
    # 已登记的取值仍然按配置显示（品牌名本来就是拉丁文）
    assert cfg.source_type_label("hackernews") == "Hacker News"


def test_a_missing_label_is_reported_to_the_operator_once(monkeypatch):
    """落回「其他」要安静地告诉他，但要**告诉运维一次**：这是配置缺口。"""
    import app.config as config_mod

    config_mod._MISSING_LABEL_WARNED.clear()
    messages: list[str] = []
    monkeypatch.setattr(config_mod, "_config_log",
                        lambda message, *args: messages.append(message % args))
    cfg = get_config()
    for _ in range(3):
        cfg.category_label("Robotics")
        cfg.source_type_label("mastodon")
    assert len(messages) == 2, f"每个缺失标签该恰好提醒一次：{messages}"
    assert "Robotics" in messages[0] and "categories" in messages[0]
    # 提醒里写的兜底文案必须是代码真正返回的那个：来源类型落回「其它来源」，
    # 日志却说「其他」就是把人往错的文件上引。
    assert "按「其他」显示" in messages[0], messages[0]
    assert "mastodon" in messages[1] and "source_types" in messages[1]
    assert "按「其它来源」显示" in messages[1], messages[1]
    config_mod._MISSING_LABEL_WARNED.clear()


def test_rendered_surfaces_stay_chinese_when_content_is_chinese():
    """内容都是中文时，输出里不该有成句的英文——只有品牌名/URL/HTML 属性允许是拉丁文。"""
    allowed = {"openai", "anthropic", "techcrunch", "nvidia", "github", "reddit",
               "claude", "gemini", "qwen", "example", "com", "href", "https", "rss",
               "gpt", "ai"}
    latin = re.compile(r"[A-Za-z][A-Za-z0-9.\-_]{2,}")
    item = make_view()
    for name, text in rendered_surfaces(item).items():
        plain = re.sub(r"<[^>]+>", " ", text)
        plain = re.sub(r"https?://\S+", " ", plain)
        rogue = sorted({w for w in latin.findall(plain) if w.lower() not in allowed})
        assert not rogue, f"{name} 出现非品牌的英文词 {rogue}：{plain[:160]!r}"
