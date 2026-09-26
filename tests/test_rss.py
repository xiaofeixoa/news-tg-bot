"""Collector tests: normal parse, empty feed, timeout, HTTP 500, broken XML.

design doc section 36.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.collectors.base import build_collectors
from app.collectors.rss import RSSCollector
from app.processing.pipeline import collect

GOOD_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>OpenAI News</title>
  <item>
    <title>GPT-5 released with a larger context window</title>
    <link>https://openai.com/index/gpt-5/?utm_source=twitter&amp;utm_campaign=launch</link>
    <guid>https://openai.com/index/gpt-5/</guid>
    <pubDate>Mon, 21 Sep 2026 08:00:00 GMT</pubDate>
    <author>research@openai.com</author>
    <description>&lt;p&gt;OpenAI released GPT-5 today. It supports 400k tokens and costs 30% less.&lt;/p&gt;</description>
  </item>
  <item>
    <title>Our new safety evaluation framework</title>
    <link>https://openai.com/index/safety-eval/</link>
    <pubDate>Mon, 21 Sep 2026 06:00:00 GMT</pubDate>
    <description>A post about AI safety evaluation for large language models.</description>
  </item>
</channel></rss>"""

ATOM_FEED = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>DeepMind Blog</title>
  <entry>
    <title>Gemini reasoning model tops the MRCR benchmark</title>
    <link href="https://deepmind.google/blog/gemini-mrcr/"/>
    <id>https://deepmind.google/blog/gemini-mrcr/</id>
    <updated>2026-09-20T10:00:00Z</updated>
    <summary>The new LLM reasoning model sets a state of the art score.</summary>
  </entry>
</feed>"""

EMPTY_FEED = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Quiet</title></channel></rss>"""
BROKEN_FEED = b"""<?xml version="1.0"?><rss><channel><title>broken</title>"""


def response(content: bytes, status: int = 200) -> SimpleNamespace:
    return SimpleNamespace(content=content, status_code=status, text=content.decode("utf-8", "ignore"))


async def stub_get(payload=None, error: Exception | None = None):
    async def _get(url, **kwargs):
        if error is not None:
            raise error
        return payload
    return _get


def make_collector(url: str = "https://openai.com/news/rss.xml") -> RSSCollector:
    from app.config import get_config

    return RSSCollector({"name": "OpenAI", "type": "rss", "url": url, "quality": "A"}, get_config())


def test_release_markdown_does_not_leak_into_extracted_text():
    """GitHub releases arrive as Markdown; "## New Features" must not become
    the first line of a Telegram summary."""
    from app.processing.normalize import build_article, strip_html

    raw = "## New Features\n\n- Added **Bedrock** support\nSee [docs](https://x.example/d) for details.\n"
    cleaned = strip_html(raw)
    assert "#" not in cleaned and "**" not in cleaned and "](" not in cleaned
    assert "New Features" in cleaned and "Bedrock" in cleaned and "docs" in cleaned
    data = build_article(title="codex 0.157.0", url="https://github.com/openai/codex/releases/tag/x",
                         source_name="GitHub Releases", content=raw)
    assert not data["content"].lstrip().startswith("#")


@pytest.mark.asyncio
async def test_rss_parses_items_and_normalises_them():
    collector = make_collector()
    collector.get = lambda url, **kwargs: _await(response(GOOD_FEED))
    items = await collector.collect()
    assert len(items) == 2
    first = items[0]
    assert first["title"].startswith("GPT-5")
    assert first["source_name"] == "OpenAI"
    assert first["source_type"] == "rss"
    # utm_* tracking parameters must be stripped from the identity URL
    assert first["normalized_url"] == "https://openai.com/index/gpt-5"
    assert "utm" not in first["url"]
    assert "400k tokens" in (first["content"] or "")
    assert first["language"] == "en"
    assert isinstance(first["published_at"], datetime)
    assert first["hash"] and first["url_hash"]


async def _await(value):
    return value


@pytest.mark.asyncio
async def test_rss_supports_atom():
    collector = make_collector("https://deepmind.google/blog/rss.xml")
    collector.get = lambda url, **kwargs: _await(response(ATOM_FEED))
    items = await collector.collect()
    assert len(items) == 1
    assert "Gemini" in items[0]["title"]
    assert items[0]["url"] == "https://deepmind.google/blog/gemini-mrcr"


@pytest.mark.asyncio
async def test_rss_empty_feed_yields_nothing_and_does_not_raise():
    collector = make_collector()
    collector.get = lambda url, **kwargs: _await(response(EMPTY_FEED))
    assert await collector.collect() == []


@pytest.mark.asyncio
async def test_rss_broken_xml_raises_collector_error():
    from app.collectors.base import CollectorError

    collector = make_collector()
    collector.get = lambda url, **kwargs: _await(response(BROKEN_FEED))
    with pytest.raises(CollectorError):
        await collector.fetch()


@pytest.mark.asyncio
async def test_rss_timeout_is_reported_not_crash():
    import httpx

    collector = make_collector()

    async def boom(url, **kwargs):
        raise httpx.ReadTimeout("timed out")

    collector.get = boom
    from app.collectors.base import CollectorError

    with pytest.raises(CollectorError):
        await collector.fetch()


@pytest.mark.asyncio
async def test_rss_http_500_raises():
    import httpx

    collector = make_collector()

    request = httpx.Request("GET", "https://openai.com/feed")
    resp = httpx.Response(500, request=request)

    async def boom(url, **kwargs):
        raise httpx.HTTPStatusError("server error", request=request, response=resp)

    collector.get = boom
    with pytest.raises(Exception) as excinfo:
        await collector.fetch()
    assert excinfo.type.__name__ in {"CollectorError", "HTTPStatusError"}


@pytest.mark.asyncio
async def test_one_dead_source_does_not_stop_the_others(session, article_factory):
    """A failing collector is recorded, the healthy one still stores news (§21, §25)."""
    from app.config import get_config

    class Broken:
        source_name = "Broken Source"

        async def collect(self):
            raise RuntimeError("503 from the feed")

    class Healthy:
        source_name = "OpenAI"

        async def collect(self):
            return [article_factory("Claude 4.6 announced by Anthropic",
                                    "https://anthropic.com/news/claude-4-6")]

    stats = await collect(session, [Broken(), Healthy()], config=get_config(), llm=None)
    session.commit()
    assert stats.errors and "Broken Source" in stats.errors[0]
    assert stats.stored == 1


@pytest.mark.asyncio
async def test_registry_builds_only_enabled_sources():
    from app.config import get_config

    collectors = build_collectors(get_config(), types={"rss"})
    names = {c.source_name for c in collectors}
    assert "OpenAI" in names
    assert "Meta AI" not in names          # enabled: false in sources.yaml
    assert "Reddit LocalLLaMA" not in names  # different type + disabled


def test_all_documented_collector_types_are_registered():
    from app.collectors.base import known_types

    assert {"rss", "hackernews", "github", "reddit", "arxiv", "youtube"} <= set(known_types())


def test_double_escaped_entities_never_reach_the_reader():
    from app.processing.normalize import clean_text, unescape_entities

    assert unescape_entities("Now &amp;#128064; then") == "Now 👀 then"   # decodes, not deleted
    assert unescape_entities("尾部 &amp;#1").strip() == "尾部"  # truncated code is dropped
    assert unescape_entities("Tom &amp; Jerry") == "Tom & Jerry"
    assert unescape_entities("a & b") == "a & b"          # not an entity at all
    # clean_text deliberately stays URL-safe; decoding happens on the text paths
    assert clean_text("OpenAI & Anthropic") == "OpenAI & Anthropic"
    assert unescape_entities("OpenAI &amp; Anthropic") == "OpenAI & Anthropic"
    assert unescape_entities("尾部的残缺实体 &amp").strip() == "尾部的残缺实体"


def test_build_article_decodes_titles_but_leaves_urls_alone():
    from app.processing.normalize import build_article

    article = build_article(title="OpenAI &amp; Anthropic 发布 &#128064; 了",
                            url="https://x.com/p?a=1&amp;b=2&utm_source=rss",
                            source_name="TechCrunch AI", content="正文 &amp; 结尾")
    assert article["title"] == "OpenAI & Anthropic 发布 👀 了"
    assert "b=2" in article["url"]              # query strings are not entity soup
    assert "&amp;" not in article["title"]
    assert "正文 & 结尾" in article["content"]
