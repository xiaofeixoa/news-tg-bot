"""采集器解析层：把线上真实响应形状钉进测试（覆盖率最低、出事最安静的一层）。

Hacker News 7 天 58 行、arXiv 42 行、Reddit 采集器目前全部 disabled（Reddit 走 RSS）。
这些模块此前 19-24% 覆盖：上游改形状不会报错，只会让某一源变成"今天没什么新闻"。
fixture 的形状来自 2026-09-26 从部署机上抓的真实响应（字段名、空值、链接结构）。
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from app.collectors.arxiv import ArxivCollector
from app.collectors.hackernews import HackerNewsCollector
from app.collectors.reddit import RedditCollector, _accept, _to_item
from app.processing.normalize import build_article
from datetime import timezone


class JsonResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class BytesResp:
    def __init__(self, body: bytes):
        self.content = body


HN_FRONT_PAGE = {
    "hits": [
        {"objectID": "49849985", "story_id": 49849985,
         "title": "Revealing the details of how OpenAI agents hacked Hugging Face",
         "url": "https://swarmtraces.org/", "author": "specked-citrus",
         "points": 683, "num_comments": 433, "created_at": "2026-09-25T21:09:27Z",
         "created_at_i": 1790344167, "story_text": None, "updated_at": "2026-09-26T01:00:00Z",
         "_tags": ["story", "author_specked-citrus", "story_49849985", "front_page"]},
        {"objectID": "49850001", "title": "Show HN: I built a coffee drip timer",
         "url": "https://example.com/drip", "author": "someone", "points": 41,
         "num_comments": 3, "created_at": "2026-09-26T01:00:00Z", "story_text": None,
         "_tags": ["story", "show_hn", "front_page"]},
    ]
}

HN_SEARCH = {
    "hits": [
        {"objectID": "49849985", "title": "Revealing the details of how OpenAI agents hacked Hugging Face",
         "url": "https://swarmtraces.org/", "points": 683, "num_comments": 433,
         "created_at": "2026-09-25T21:09:27Z", "author": "specked-citrus",
         "_tags": ["story"]},                      # 与 front_page 同一条 → 只能出现一次
        {"objectID": "49851000", "title": "Ask HN: where should I run my LLM agents?",
         "url": None, "points": 120, "num_comments": 88,
         "created_at": "2026-09-26T03:00:00Z", "author": "asker",
         "story_text": "I have a few agents and no GPU.", "_tags": ["ask_hn", "story"]},
    ]
}

ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <title>ArXiv Query</title>
  <entry>
    <id>http://arxiv.org/abs/2609.11111v1</id>
    <updated>2026-09-26T04:00:00Z</updated>
    <published>2026-09-25T04:00:00Z</published>
    <title>AgentGym: a benchmark for tool-calling in large language models</title>
    <summary>We evaluate LLM agents on tool calling across 40 tasks with a sandbox.</summary>
    <author><name>Zhang, Wei</name></author>
    <author><name>Lang, Ada</name></author>
    <link href="https://arxiv.org/abs/2609.11111v1" rel="alternate" type="text/html"/>
    <link href="https://arxiv.org/pdf/2609.11111v1" rel="related" type="application/pdf" title="pdf"/>
    <arxiv:primary_category term="cs.LG"/>
    <category term="cs.LG"/><category term="cs.AI"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2012.12104v1</id>
    <updated>2020-12-09T05:08:41Z</updated>
    <published>2020-12-08T05:08:41Z</published>
    <title>A Deep Approach for Ramp Metering Based on Traffic Video Data</title>
    <summary>We use camera footage at highway on-ramps to control traffic lights.</summary>
    <author><name>Driver, Ann</name></author>
    <link href="https://arxiv.org/abs/2012.12104v1" rel="alternate" type="text/html"/>
    <arxiv:primary_category term="eess.IV"/>
    <category term="eess.IV"/>
  </entry>
</feed>
"""


def hn_collector(**source):
    base = {"name": "Hacker News", "type": "hackernews", "queries": ["AI"], "max_items": 25}
    base.update(source)
    return HackerNewsCollector(base)


# ------------------------------------------------------------------ Hacker News
async def test_hn_front_page_item_keeps_points_and_external_url(monkeypatch):
    async def spy(self, url, **kwargs):
        return JsonResp(HN_FRONT_PAGE if (kwargs.get("params") or {}).get("tags") == "front_page"
                        else {"hits": []})

    monkeypatch.setattr("app.collectors.base.BaseCollector.get", spy)
    items = await hn_collector().fetch()

    assert [i["title"] for i in items] == [
        "Revealing the details of how OpenAI agents hacked Hugging Face"], \
        "咖啡滴滤器那条没有 AI 关键词，front page 也不该放宽这道门"
    item = items[0]
    assert item["url"] == "https://swarmtraces.org/", "有外链的帖子要用外链，去重才和 RSS 对得上"
    assert item["community_heat"] == 683 and item["meta"]["points"] == 683
    assert item["published_at"] == datetime(2026, 9, 25, 21, 9, 27, tzinfo=timezone.utc)
    stored = build_article(**item, source_name="Hacker News", source_type="hackernews")
    assert stored["published_at"] == datetime(2026, 9, 25, 21, 9, 27), (
        "采集器交的是带时区的 UTC，入库那一层必须抹成 naive——窗口比较用的是 naive")
    assert item["meta"]["hn_discussion"].endswith("item?id=49849985")
    assert item["meta"]["external_url"] == "https://swarmtraces.org/"


async def test_hn_ask_post_without_url_falls_back_to_the_discussion(monkeypatch):
    async def spy(self, url, **kwargs):
        return JsonResp({"hits": []} if (kwargs.get("params") or {}).get("tags") == "front_page"
                        else HN_SEARCH)

    monkeypatch.setattr("app.collectors.base.BaseCollector.get", spy)
    items = await hn_collector().fetch()

    by_id = {i["meta"]["hn_id"]: i for i in items}
    assert set(by_id) == {"49849985", "49851000"}, "同一条在两个查询里出现只能有一条"
    ask = by_id["49851000"]
    assert ask["url"] == "https://news.ycombinator.com/item?id=49851000", "Ask HN 没有外链"
    assert ask["content"].startswith("I have a few agents"), "正文用 story_text，不是标题"


async def test_hn_asks_for_the_points_and_age_it_relies_on(monkeypatch):
    asked = []

    async def spy(self, url, **kwargs):
        params = kwargs.get("params") or {}
        if params.get("tags") != "front_page":
            asked.append(params)
        return JsonResp({"hits": []})

    monkeypatch.setattr("app.collectors.base.BaseCollector.get", spy)
    await hn_collector(min_points=200).fetch()

    assert asked and "points>=200" in asked[0]["numericFilters"], asked
    assert "created_at_i>" in asked[0]["numericFilters"], "48 小时窗口是服务端过滤，不是本地筛"


# ----------------------------------------------------------------------- arXiv
async def test_arxiv_parses_the_atom_feed_and_gates_on_keywords(monkeypatch):
    asked = {}

    async def spy(self, url, **kwargs):
        asked.update(kwargs.get("params") or {})
        return BytesResp(ARXIV_ATOM.encode())

    monkeypatch.setattr("app.collectors.base.BaseCollector.get", spy)
    items = await ArxivCollector({"name": "arXiv AI", "type": "arxiv",
                                  "categories": ["cs.AI", "cs.LG"], "max_items": 20}).fetch()

    assert [i["title"] for i in items] == [
        "AgentGym: a benchmark for tool-calling in large language models"], "高速公路匝道那条不含 AI 关键词"
    assert asked["sortBy"] == "submittedDate" and asked["sortOrder"] == "descending", \
        "不按提交时间排，API 会把 2020 年的论文当新论文发回来"
    assert asked["search_query"] == "cat:cs.AI OR cat:cs.LG"
    item = items[0]
    assert item["url"] == "https://arxiv.org/abs/2609.11111v1"
    assert item["meta"]["arxiv_id"] == "2609.11111v1"
    assert item["meta"]["pdf_url"] == "https://arxiv.org/pdf/2609.11111v1"
    assert item["meta"]["primary_category"] == "cs.LG"
    assert item["author"] == "Zhang, Wei, Lang, Ada"
    assert item["published_at"] == datetime(2026, 9, 25, 4, 0, tzinfo=timezone.utc)
    assert build_article(**item, source_name="arXiv AI", source_type="arxiv")[
        "published_at"] == datetime(2026, 9, 25, 4, 0)


async def test_arxiv_survives_a_response_with_no_entries(monkeypatch):
    async def spy(self, url, **kwargs):
        return BytesResp(b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>')

    monkeypatch.setattr("app.collectors.base.BaseCollector.get", spy)
    assert await ArxivCollector({"name": "arXiv AI", "type": "arxiv"}).fetch() == []


# ---------------------------------------------------------------------- Reddit
def reddit_post(**over):
    base = {"id": "abc", "permalink": "/r/LocalLLaMA/comments/abc/t/", "title": "My new agent runs locally",
            "author": "someone", "subreddit": "LocalLLaMA", "stickied": False, "promoted": False,
            "is_created_from_ads_ui": False, "ups": 300, "num_comments": 40, "score": 300,
            "selftext": "It runs on one GPU.", "is_self": True, "url": "https://reddit.com/r/LocalLLaMA",
            "created_utc": 1790000000, "link_flair_text": "Discussion"}
    base.update(over)
    return base


SOURCE = {"subreddit": "LocalLLaMA", "min_upvotes": 50, "min_comments": 0}


def test_reddit_filters_the_noise_it_always_carries():
    assert _accept(reddit_post(), SOURCE, "LocalLLaMA")
    assert not _accept(reddit_post(stickied=True), SOURCE, "LocalLLaMA"), "置顶公告不是新闻"
    assert not _accept(reddit_post(promoted=True), SOURCE, "LocalLLaMA"), "广告帖"
    assert not _accept(reddit_post(ups=9), SOURCE, "LocalLLaMA"), "低于门槛"
    assert not _accept(reddit_post(author="AutoModerator"), SOURCE, "LocalLLaMA")
    assert not _accept(reddit_post(subreddit="MachineLearning"), SOURCE, "LocalLLaMA"), \
        "跨版推荐帖的 subreddit 字段是原版的"
    assert not _accept(reddit_post(title="   "), SOURCE, "LocalLLaMA"), "没标题就没法上简报"


def test_reddit_self_post_and_link_post_are_keyed_differently_on_purpose():
    self_post = _to_item(reddit_post(), "LocalLLaMA")
    assert self_post["url"].startswith("https://www.reddit.com/r/LocalLLaMA/comments/abc")
    assert self_post["community_heat"] == 300
    assert self_post["meta"]["external_url"] is None

    link = _to_item(reddit_post(is_self=False, url="https://example.com/paper",
                                selftext=""), "LocalLLaMA")
    assert link["url"] == "https://example.com/paper", "同一篇文章被别的源也覆盖时要去重"
    assert link["meta"]["discussion"].startswith("https://www.reddit.com")
    assert link["content"].startswith("Shared link: https://example.com/paper")


async def test_reddit_needs_a_subreddit_and_says_so():
    """没配 subreddit 时要在发请求之前就报错，而不是拿到一版空数据。"""
    from app.collectors.base import CollectorError

    with pytest.raises(CollectorError):
        await RedditCollector({"name": "Reddit", "type": "reddit"}).fetch()


def test_every_wired_source_type_has_a_collector():
    from app.collectors.base import known_types
    assert {"reddit", "hackernews", "arxiv", "github", "rss"} <= set(known_types())
