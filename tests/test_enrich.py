"""正文提取: feeds that ship a stub must not decide the briefing.

Measured before this existed: Google DeepMind and Hugging Face delivered
36-288 characters per item, so 100 first-party announcements in seven days
produced zero digest-eligible rows while 3.7 KB Reddit threads cleared the bar.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_config
from app.database import repository as repo
from app.database.models import Article
from app.processing import enrich
from app.processing.normalize import build_article

STUB = "Gemini 2.5 is available today."
FULL = "\n".join(
    f"Gemini 2.5 improves reasoning, tool calling and long context handling on "
    f"every plan, and the model now scores higher on coding and agent benchmarks."
    for _ in range(6))

PAGE = f"""<html><head><script>var tracking = "AI";</script></head><body>
  <nav class="menu">AI News Subscribe Sign in Cookie Policy</nav>
  <article>
    <h2>Gemini 2.5</h2>
    <p>{FULL.splitlines()[0]}</p>
    <p>Related posts: another story about GPUs that is definitely not the article.</p>
    <p>{FULL.splitlines()[1]}</p>
    <footer>All rights reserved.</footer>
  </article>
  <aside>Subscribe to our newsletter for more AI coverage every week!</aside>
</body></html>"""


def test_extract_takes_the_article_and_drops_the_chrome():
    text = enrich.extract(PAGE)

    assert "Gemini 2.5 improves reasoning" in text
    assert "Cookie Policy" not in text and "tracking" not in text
    assert "All rights reserved" not in text and "Subscribe to our newsletter" not in text


def test_extract_survives_junk_input():
    assert enrich.extract("") == ""
    assert enrich.extract("not html at all") == ""


def test_budget_is_a_hard_cap():
    budget = enrich.Budget(2)
    assert budget.spend() and budget.spend()
    assert not budget.spend()
    assert budget.used == 2 and budget.left == 0
    assert not enrich.Budget(0).spend()
    assert not enrich.Budget(-3).spend()


def _article(content: str, url: str = "https://blog.example/post") -> dict:
    return {"title": "Gemini 2.5 released", "url": url, "content": content,
            "source_name": "Google DeepMind", "meta": {}}


class Row:
    """Just the fields `maybe_enrich` touches on the ORM object."""

    def __init__(self) -> None:
        self.content = STUB
        self.meta = {}


async def test_a_stub_is_replaced_with_the_page(monkeypatch):
    async def fake_fetch(url, **kwargs):
        return FULL

    monkeypatch.setattr(enrich, "fetch", fake_fetch)
    data, row = _article(STUB), Row()
    budget = enrich.Budget(5)

    assert await enrich.maybe_enrich(data, row, get_config(), budget=budget) is True
    assert data["content"] == FULL and row.content == FULL
    assert row.meta["enriched"] is True and budget.used == 1


async def test_real_content_and_spent_budgets_are_left_alone(monkeypatch):
    calls: list[str] = []

    async def fake_fetch(url, **kwargs):
        calls.append(url)
        return FULL

    monkeypatch.setattr(enrich, "fetch", fake_fetch)

    assert await enrich.maybe_enrich(_article(FULL), Row(), get_config(),
                                     budget=enrich.Budget(5)) is False
    assert await enrich.maybe_enrich(_article(STUB), Row(), get_config(),
                                     budget=enrich.Budget(0)) is False
    assert calls == [], "neither case may touch the network"


async def test_a_page_shorter_than_the_stub_is_not_an_upgrade(monkeypatch):
    async def fake_fetch(url, **kwargs):
        return "too short"

    monkeypatch.setattr(enrich, "fetch", fake_fetch)
    data, row = _article(STUB), Row()

    assert await enrich.maybe_enrich(data, row, get_config(), budget=enrich.Budget(3)) is False
    assert data["content"] == STUB


async def test_enrichment_keeps_markers_that_came_from_elsewhere(monkeypatch):
    """`article.meta` 里不只有采集器的键：突发热度观察、zh_misses、限免标注住在同一格。

    这里曾经是 `article.meta = data["meta"]` 整体覆盖，于是这一行只要被正文提取碰过
    一次，本轮之外写进去的记号就全没了——重试队列再也读不到它，突发也就永远不会补发。
    """
    async def fake_fetch(url, **kwargs):
        return FULL

    monkeypatch.setattr(enrich, "fetch", fake_fetch)
    data = _article(STUB)
    data["meta"] = {"points": 12}
    row = Row()
    row.meta = {"breaking_defer": {"tries": 2, "reason": "等待全站热度"}, "zh_misses": {"title": 1}}

    assert await enrich.maybe_enrich(data, row, get_config(), budget=enrich.Budget(5)) is True
    assert row.meta["breaking_defer"]["tries"] == 2 and row.meta["zh_misses"]["title"] == 1
    assert row.meta["points"] == 12 and row.meta["enriched"] is True

    async def refuse(url, **kwargs):
        return "too short"

    monkeypatch.setattr(enrich, "fetch", refuse)
    data2 = _article(STUB, url="https://blog.example/refusing-host")
    data2["meta"] = {"hn_id": "99"}
    row2 = Row()
    row2.meta = {"breaking_defer": {"tries": 1, "reason": "等待全站热度"}}

    assert await enrich.maybe_enrich(data2, row2, get_config(), budget=enrich.Budget(5)) is False
    assert row2.meta["breaking_defer"] and row2.meta["hn_id"] == "99"
    assert row2.meta["enriched"] is False, "被拒绝也要记号，requeue_stubs 靠它别再敲门"
    assert data2["meta"] is not row2.meta, "两处不该共用同一个 dict：改一处就是改两处"


async def test_a_refusing_host_is_parked_not_asked_again(monkeypatch):
    """One 403 per hour, not one per article - see collectors.base backoff."""
    from app.collectors import base

    base._cooldowns.clear()
    requests: list[str] = []

    class Client:
        async def get(self, url, **kwargs):
            requests.append(url)
            raise RuntimeError("403 Forbidden")

    async def client():
        return Client()

    monkeypatch.setattr(base, "get_client", client)
    url = "https://blocked.example/post"
    first = await enrich.fetch(url)
    second = await enrich.fetch(url)

    assert first == "" and second == ""
    assert len(requests) == 1, "the host must be parked after a failure"
    assert base.cooling(url) > 3000
    base._cooldowns.clear()


# ------------------------------------------------------- pipeline behaviour
def _feed(title: str, url: str, content: str) -> dict:
    return build_article(title=title, url=url, source_name="Google DeepMind",
                         source_type="rss", content=content,
                         published_at=datetime.now(timezone.utc).replace(tzinfo=None))


async def _no_fetch(url, **kwargs):
    return ""


class _NoAI:
    """Production runs the rule path (no LLM key), so this is the mode that matters."""

    enabled = False


@pytest.mark.asyncio
async def test_enrichment_never_unfilters_a_story_the_stub_passed(session, monkeypatch):
    """Measured: one HF page extracted 6.8 KB with zero keyword hits.

    Dropping news because the page we fetched was a docs view would trade a
    scoring bug for a recall bug, so extraction only ever adds text.
    """
    from app.processing.pipeline import process_pending

    dry = ("This page describes the layout of the archive and how to browse its "
           "sections in a plain readable order. " * 4)

    async def fake_fetch(url, **kwargs):
        return dry

    monkeypatch.setattr(enrich, "fetch", fake_fetch)
    saved = repo.save_article(session, _feed("OpenAI releases a new model today",
                                             "https://blog.example/dry", STUB))
    session.commit()
    await process_pending(session, config=get_config(), llm=_NoAI(), limit=5)
    session.commit()
    row = session.get(Article, saved.id)

    assert row.is_processed and not row.filtered_out, "extraction must not lose the story"
    assert row.content == dry


@pytest.mark.asyncio
async def test_full_text_lets_an_official_announcement_clear_the_bar(session, monkeypatch):
    """Same source, same title shape: only the extra text may change the rank."""
    from app.processing.pipeline import process_pending

    cfg = get_config()
    bar = float(cfg.get("digest.morning.min_score", 55))
    monkeypatch.setattr(enrich, "fetch", _no_fetch)
    blind = repo.save_article(session, _feed("Gemini 2.5 released today",
                                             "https://blog.example/blind", STUB))
    session.commit()
    await process_pending(session, config=cfg, llm=_NoAI(), limit=5)
    session.commit()
    blind_score = session.get(Article, blind.id).final_score

    async def fake_fetch(url, **kwargs):
        return FULL

    monkeypatch.setattr(enrich, "fetch", fake_fetch)
    seen = repo.save_article(session, _feed("Gemini 2.5 released now",
                                            "https://blog.example/seen", STUB))
    session.commit()
    await process_pending(session, config=cfg, llm=_NoAI(), limit=5)
    session.commit()
    row = session.get(Article, seen.id)

    assert row.content == FULL and row.meta.get("enriched") is True
    assert row.final_score - blind_score >= 5.0, \
        f"only the text differs: enriched={row.final_score} stub={blind_score}"
    assert row.final_score >= bar, f"enriched={row.final_score} bar={bar}"
    # In production the stubs are shorter and hit fewer keywords than this
    # fixture, which is why every DeepMind/HF row capped out at 54.2/50.9
    # against a bar of 55 - measured on the live database, not modelled here.


async def test_a_refused_page_is_recorded_as_tried(monkeypatch):
    async def none(url, **kwargs):
        return ""

    monkeypatch.setattr(enrich, "fetch", none)
    data, row = _article(STUB), Row()

    assert await enrich.maybe_enrich(data, row, get_config(), budget=enrich.Budget(3)) is False
    assert data["content"] == STUB and row.meta["enriched"] is False


@pytest.mark.asyncio
async def test_requeue_spends_its_budget_on_the_rows_nearest_the_bar(session):
    """328 stub rows were sitting in the database; only the near-misses matter."""
    rows = []
    for index, (score, body) in enumerate(((41.0, STUB), (62.0, STUB),
                                           (53.0, STUB), (70.0, FULL))):
        saved = repo.save_article(session, _feed(f"Hugging Face model of the week {index}",
                                                 f"https://hf.example/{index}", body))
        row = session.get(Article, saved.id)
        row.final_score, row.is_processed = score, True
        rows.append(row)
    session.commit()

    assert enrich.requeue_stubs(session, config=get_config(), within_hours=36, limit=2) == 2
    session.commit()
    assert sorted(row.final_score for row in rows if not row.is_processed) == [53.0, 62.0], \
        "the 70-pointer already has full text and must not be requeued"


@pytest.mark.asyncio
async def test_a_row_whose_page_refused_is_not_knocked_on_again(session):
    saved = repo.save_article(session, _feed("Gemini 2.5 preview", "https://dm.example/1", STUB))
    row = session.get(Article, saved.id)
    row.is_processed, row.meta, row.final_score = True, {"enriched": False}, 54.0
    session.commit()

    assert enrich.requeue_stubs(session, config=get_config()) == 0


@pytest.mark.asyncio
async def test_a_translation_of_a_teaser_does_not_survive_the_full_text(session):
    """Measured live: enriched row kept "Foundry 托管计算上的拥抱脸部模型"."""
    body = repo.save_article(session, _feed("Hugging Face Models on Foundry",
                                            "https://hf.example/stale", STUB))
    row = session.get(Article, body.id)
    row.summary = "Hugging Face Models on Foundry Managed Compute"
    row.summary_zh = "Foundry 托管计算上的拥抱脸部模型"
    row.translated_by = "mymemory"
    row.is_processed = True
    session.commit()

    assert enrich.reset_stale_translations(session, config=get_config()) == 0, \
        "an un-enriched row is not the repair's business"
    row.meta = {"enriched": True}
    row.content = FULL                      # what an enriched row looks like on disk
    session.commit()

    assert enrich.reset_stale_translations(session, config=get_config()) == 1
    session.commit()
    assert row.summary_zh is None and row.translated_by is None
    assert enrich.reset_stale_translations(session, config=get_config()) == 0, \
        "the next boot must not re-clear the line the translator just made"


@pytest.mark.asyncio
async def test_rewriting_a_summary_invalidates_its_translation(session, fake_llm, monkeypatch):
    """The general net: any re-pass that changes the text must drop the old Chinese."""
    from app.processing.pipeline import _apply, process_pending

    saved = repo.save_article(session, _feed("OpenAI ships a cheaper reasoning model",
                                              "https://openai.com/stale-zh", FULL))
    row = session.get(Article, saved.id)
    session.commit()
    await process_pending(session, config=get_config(), llm=fake_llm, limit=5)
    session.commit()
    row.summary_zh = "旧译文"
    row.translated_by = "mymemory"
    session.commit()

    _apply(session, row, category="AI Models", subcategory=None,
                    scores={"final_score": 70.0}, summary={"summary": "a different summary"},
                    tags=[], method="rule", config=get_config())
    session.commit()
    assert row.summary_zh is None and row.translated_by is None
