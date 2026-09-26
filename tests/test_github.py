"""GitHub collector: releases mode ordering, and sources.yaml hygiene."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import yaml

from app.collectors.base import CollectorError
from app.collectors.github import GitHubCollector


class Resp:
    def __init__(self, payload, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.headers = FakeHeaders(headers or {})

    def json(self):
        return self._payload


def release_payload(tag: str, when: datetime) -> Resp:
    return Resp({"html_url": f"https://github.com/o/r/releases/tag/{tag}",
                 "tag_name": tag, "name": tag, "published_at": when.isoformat(),
                 "body": f"notes for {tag}", "author": {"login": "someone"}})


def make_collector(repos, *, cap=None, by_repo=None):
    source = {"name": "Coding Agent Releases", "type": "github", "mode": "releases",
              "category": "official", "quality": "A", "fresh_hours": 168,
              "repositories": repos}
    if cap is not None:
        source["max_items"] = cap
    collector = GitHubCollector(source)
    now = datetime.utcnow()
    table = by_repo or {name: release_payload(f"v-{i}", now - timedelta(hours=i))
                        for i, name in enumerate(repos)}

    async def fake_get(url, **kwargs):
        name = url.split("/repos/")[1].rsplit("/releases", 1)[0]
        got = table.get(name)
        if got is None:
            raise RuntimeError("404")
        return got

    if by_repo is not None or cap is not None:
        collector.get = fake_get     # only for the pure-parsing tests
    else:
        collector._stub_get = fake_get
    return collector


async def test_releases_are_capped_newest_first_not_in_config_order():
    """The cap must drop the oldest releases, not whichever repo is listed last."""
    repos = [f"org/repo-{i}" for i in range(12)]
    now = datetime.utcnow()
    # repo-0 is listed first but is the oldest; repo-11 is the newest
    table = {name: release_payload(f"v{i}", now - timedelta(hours=12 - i))
             for i, name in enumerate(repos)}
    collector = make_collector(repos, cap=3, by_repo=table)
    items = await collector._releases()
    assert [i["meta"]["repo"] for i in items] == ["org/repo-11", "org/repo-10", "org/repo-9"]


async def test_repos_without_releases_are_skipped_not_fatal():
    repos = ["org/empty", "org/good"]
    table = {"org/good": release_payload("v1", datetime.utcnow() - timedelta(hours=1))}
    collector = make_collector(repos, by_repo=table)
    items = await collector._releases()
    assert [i["meta"]["repo"] for i in items] == ["org/good"]


async def test_stale_releases_outside_fresh_hours_are_dropped():
    repos = ["org/old"]
    table = {"org/old": release_payload("v1", datetime.utcnow() - timedelta(days=30))}
    collector = make_collector(repos, by_repo=table)
    assert await collector._releases() == []


def test_sources_yaml_watches_each_repo_once():
    """A repo in two sources costs two API calls per round and stores nothing new."""
    cfg = yaml.safe_load(open("config/sources.yaml", encoding="utf-8"))
    seen: dict[str, list[str]] = {}
    for source in cfg["sources"]:
        if source.get("mode") == "releases":
            for repo in source.get("repositories") or []:
                seen.setdefault(str(repo).lower(), []).append(source["name"])
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    assert not dupes, f"watched twice: {dupes}"


def test_the_agents_the_user_cares_about_are_watched():
    """Qoder and Zcode must come from their own changelog, not from forum luck."""
    cfg = yaml.safe_load(open("config/sources.yaml", encoding="utf-8"))
    watched = {str(r).lower() for s in cfg["sources"] if s.get("mode") == "releases"
               for r in (s.get("repositories") or [])}
    assert "zai-org/zcode" in watched
    assert "qoderai/better-harness" in watched
    assert "sst/opencode" in watched


class FakeHeaders(dict):
    """httpx headers are case-insensitive; mimic that."""

    def __init__(self, source=None):
        super().__init__({str(k).lower(): v for k, v in (source or {}).items()})


@pytest.fixture(autouse=True)
def _reset_rate_state(monkeypatch, tmp_path):
    from app.collectors import github
    github._rate.update({"remaining": None, "reset": 0.0})
    github._stats.update({"metered": 0, "not_modified": 0, "empty_skipped": 0})
    # In-memory so no test ever reads or writes the real data dir.
    github._etag_cache = {}
    monkeypatch.setattr(github, "_etag_path", lambda config: tmp_path / "github_etags.json")
    yield


def test_exhausted_anonymous_quota_is_reported_as_a_block():
    import time
    from app.collectors import github

    github.note_rate(FakeHeaders({"X-RateLimit-Remaining": "0",
                                  "X-RateLimit-Reset": str(time.time() + 900)}))
    reason = github.rate_block_reason()
    assert reason and "GITHUB_TOKEN" in reason and "15 分钟" in reason


def test_the_block_lifts_once_the_window_rolls_over():
    import time
    from app.collectors import github

    github.note_rate(FakeHeaders({"x-ratelimit-remaining": "0",
                                  "x-ratelimit-reset": str(time.time() - 5)}))
    assert github.rate_block_reason() is None      # window passed: probe again
    assert github._rate["remaining"] is None


async def test_no_http_calls_are_made_while_blocked(monkeypatch):
    """28 repos x a dead quota used to mean 28 pointless 403s per round."""
    from app.collectors import github

    import time as _time
    github._rate.update({"remaining": 0, "reset": _time.time() + 900})
    calls = []

    async def spy(self, url, **kwargs):
        calls.append(url)
        raise AssertionError("must not reach the network while blocked")

    monkeypatch.setattr("app.collectors.base.BaseCollector.get", spy)
    collector = GitHubCollector({"name": "Coding Agent Releases", "type": "github",
                                 "mode": "releases", "repositories": ["a/b", "c/d"]})
    with pytest.raises(CollectorError) as exc:
        await collector._releases()
    assert calls == []
    assert "GITHUB_TOKEN" in str(exc.value)


async def test_a_403_with_zero_remaining_surfaces_instead_of_looking_empty(monkeypatch):
    import time as _time

    from app.collectors import github

    async def rate_limited(self, url, **kwargs):
        # GitHub sends the quota headers even on the 403
        return Resp({"message": "API rate limit exceeded"}, status=403,
                    headers={"x-ratelimit-remaining": "0",
                             "x-ratelimit-reset": str(_time.time() + 900)})

    monkeypatch.setattr("app.collectors.base.BaseCollector.get", rate_limited)
    collector = GitHubCollector({"name": "Coding Agent Releases", "type": "github",
                                 "mode": "releases", "repositories": ["zai-org/ZCode"]})
    with pytest.raises(CollectorError) as exc:
        await collector._releases()
    assert "配额" in str(exc.value)
    assert github._rate["remaining"] == 0


async def test_a_raised_403_still_teaches_the_guard(monkeypatch):
    """base.get() throws away the headers, so the quota must be re-checked."""
    import time as _time

    from app.collectors import github

    calls = []

    async def explode(self, url, **kwargs):
        calls.append(url)
        if url.endswith("/rate_limit"):
            return Resp({"resources": {"core": {"remaining": 0,
                                                 "reset": _time.time() + 600}}}, status=200,
                        headers={"x-ratelimit-remaining": "0"})
        raise CollectorError(f"{url} -> HTTP 403")

    monkeypatch.setattr("app.collectors.base.BaseCollector.get", explode)
    github._rate.update({"remaining": None, "reset": 0.0})
    github.GitHubCollector._spent_checked_at = 0.0
    collector = GitHubCollector({"name": "Coding Agent Releases", "type": "github",
                                 "mode": "releases", "repositories": ["zai-org/ZCode"]})
    with pytest.raises(CollectorError) as exc:
        await collector._releases()
    assert "GITHUB_TOKEN" in str(exc.value)
    assert any(url.endswith("/rate_limit") for url in calls)
    assert github.rate_block_reason()          # remembered for the rest of the window

    before = len(calls)
    with pytest.raises(CollectorError):
        await collector._releases()
    assert len(calls) == before, "the second round must not probe GitHub again"


class JsonResp(Resp):
    """httpx hands out both a parsed body and `.text`; the cache needs the text."""

    def __init__(self, payload, status=200, headers=None):
        super().__init__(payload, status=status, headers=headers)
        import json

        self.text = json.dumps(payload)


def _release(when: datetime | None = None) -> dict:
    return {"html_url": "https://github.com/zai-org/ZCode/releases/tag/v1.2",
            "tag_name": "v1.2", "name": "v1.2",
            "published_at": (when or datetime.utcnow()).isoformat(), "body": "notes"}


def _releases_collector():
    return GitHubCollector({"name": "Coding Agent Releases", "type": "github",
                            "mode": "releases", "repositories": ["zai-org/ZCode"]})


async def test_an_unchanged_repo_costs_nothing_after_the_first_round(monkeypatch):
    """27 watched repos burned the whole 60/hour anonymous budget twice an hour.

    A 304 is free, so round two must send the validator back and still deliver
    the release - the data cannot depend on the budget being there.
    """
    from app.collectors import github

    seen: list[str | None] = []

    async def conditional(self, url, **kwargs):
        validator = (kwargs.get("headers") or {}).get("If-None-Match")
        seen.append(validator)
        if validator == '"e1"':
            return Resp(None, status=304, headers={"x-ratelimit-remaining": "59"})
        return JsonResp(_release(), headers={"etag": '"e1"', "x-ratelimit-remaining": "30"})

    monkeypatch.setattr("app.collectors.base.BaseCollector.get", conditional)
    collector = _releases_collector()
    first = await collector.fetch()
    second = await collector.fetch()

    assert seen == [None, '"e1"']
    assert [i["meta"]["tag"] for i in (first[0], second[0])] == ["v1.2", "v1.2"]
    assert github._rate["remaining"] == 59, "a 304 still carries the live budget"
    assert (github._stats["metered"], github._stats["not_modified"]) == (1, 1), \
        "two rounds must cost exactly one credit"


async def test_the_validator_survives_a_restart(monkeypatch, tmp_path):
    """The cache file is the point: a restart used to spend 27 credits again."""
    from app.collectors import github

    seen: list[str | None] = []

    async def conditional(self, url, **kwargs):
        validator = (kwargs.get("headers") or {}).get("If-None-Match")
        seen.append(validator)
        return JsonResp(_release(), headers={"etag": '"e1"'})

    monkeypatch.setattr("app.collectors.base.BaseCollector.get", conditional)
    await _releases_collector().fetch()
    assert (tmp_path / "github_etags.json").is_file()

    github._etag_cache = None            # cold process, warm disk
    await _releases_collector().fetch()
    assert seen == [None, '"e1"']


async def test_trending_never_reports_an_unwatched_repo(monkeypatch):
    """The production DB filled up with "someone/new-repo (0 stars)" headlines."""
    asked: list[tuple[str, str]] = []

    async def spy(self, url, **kwargs):
        params = kwargs.get("params") or {}
        asked.append((str(params.get("q")), str(params.get("sort"))))
        return JsonResp({"items": []})

    monkeypatch.setattr("app.collectors.base.BaseCollector.get", spy)
    collector = GitHubCollector({"name": "GitHub Trending", "type": "github",
                                 "searches": [{"topic": "ai"}]})
    await collector._search()

    created = [q for q, _ in asked if "created:>" in q]
    pushed = [(q, s) for q, s in asked if "pushed:>" in q]
    assert created and "stars:>=" in created[0]
    assert pushed and pushed[0][1] == "updated", "sort=stars returns the same 25 forever"


async def test_each_topic_is_its_own_query_not_an_intersection(monkeypatch):
    """`topic:ai topic:llm` means repos with both tags, which is almost nobody."""
    asked: list[str] = []

    async def spy(self, url, **kwargs):
        params = kwargs.get("params") or {}
        asked.append(str(params.get("q")))
        return JsonResp({"items": []})

    monkeypatch.setattr("app.collectors.base.BaseCollector.get", spy)
    collector = GitHubCollector({"name": "GitHub Trending", "type": "github",
                                 "searches": [{"topic": "ai"}, {"topic": "llm"},
                                              {"topic": "agents"}]})
    await collector._search()

    assert asked[0].startswith("topic:ai created:")
    assert all("topic:llm" not in q for q in asked), "topics must not be AND-ed"
    assert len(asked) == 2, "the anonymous budget caps how many queries run"


async def test_only_bodyless_requests_get_a_validator(monkeypatch):
    """Search queries embed a date that changes every round, so caching them
    would grow the file without ever producing a 304."""
    from app.collectors import github

    async def ok(self, url, **kwargs):
        return JsonResp({"items": []}, headers={"etag": '"e2"'})

    monkeypatch.setattr("app.collectors.base.BaseCollector.get", ok)
    collector = GitHubCollector({"name": "Trending", "type": "github",
                                 "searches": [{"topic": "ai"}]})
    await collector.fetch()
    assert github.load_etags(collector.config) == {}


async def test_a_repo_with_no_releases_stops_costing_budget(monkeypatch):
    """Repos that publish nothing answered 404 in every past round already.

    Each one cost a credit every 30 minutes forever, on a question whose answer
    does not change - see logs/collector.log's "known-empty" count for how many.
    """
    from app.collectors import github

    asked: list[str] = []

    async def not_found(self, url, **kwargs):
        asked.append(url)
        raise CollectorError(f"{url} -> HTTP 404")

    monkeypatch.setattr("app.collectors.base.BaseCollector.get", not_found)
    collector = _releases_collector()
    await collector.fetch()
    await collector.fetch()
    await collector.fetch()

    assert len(asked) == 1, "the 404 must be remembered, not re-asked"
    assert github._stats["empty_skipped"] == 2


async def test_a_timeout_is_never_recorded_as_an_answer(monkeypatch):
    """One grey round must not hide an agent's first ever release for hours."""
    from app.collectors import github

    asked: list[str] = []

    async def flaky(self, url, **kwargs):
        asked.append(url)
        raise CollectorError(f"{url} ConnectTimeout: ")

    monkeypatch.setattr("app.collectors.base.BaseCollector.get", flaky)
    collector = _releases_collector()
    await collector.fetch()
    await collector.fetch()

    assert len(asked) == 2
    assert github._stats["empty_skipped"] == 0


async def test_the_released_nothing_answer_expires(monkeypatch):
    """`empty_hours` is the difference between caching and forgetting."""
    from datetime import datetime, timedelta

    from app.collectors import github

    asked: list[str] = []

    async def not_found(self, url, **kwargs):
        asked.append(url)
        raise CollectorError(f"{url} -> HTTP 404")

    monkeypatch.setattr("app.collectors.base.BaseCollector.get", not_found)
    collector = _releases_collector()
    await collector.fetch()
    await collector.fetch()
    assert len(asked) == 1

    url = asked[0]
    entry = github.load_etags(collector.config)[url]
    entry["missing_until"] = (datetime.utcnow() - timedelta(seconds=1)).isoformat(
        timespec="seconds")
    await collector.fetch()
    assert len(asked) == 2, "an expired 404 has to be asked again"
