"""GitHub collector: trending-by-search, releases, organisations, topics (§4.3).

GitHub has no official "trending" API, so trending is reproduced with the
search API (newest high-star / fast-moving repos by topic). All modes share one
class and are selected per source in config/sources.yaml.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

from app.collectors.base import BaseCollector, CollectorError, SourceBudget, register
from app.config import as_float, as_int
from app.logging_setup import get_logger
from app.processing.normalize import clean_text, to_utc_naive

log = get_logger("collector")

API = "https://api.github.com"

# How long a repo that answers "I have no releases" is left alone.
DEFAULT_EMPTY_HOURS = 6.0

# One anonymous budget per IP, shared by every GitHub source on this box.
_rate = {"remaining": None, "reset": 0.0}

# Responses this process has seen, so a round can report what it cost.
_stats = {"metered": 0, "not_modified": 0, "empty_skipped": 0}
RATE_HINT = ("GitHub API 匿名配额只有 60 次/小时，已用完；"
             "在 /etc/ai-news-radar/env 里设 GITHUB_TOKEN=<PAT> 可放宽到 5000 次/小时")


def note_rate(headers: Any) -> None:
    remaining = headers.get("x-ratelimit-remaining") if headers else None
    reset = headers.get("x-ratelimit-reset") if headers else None
    try:
        if remaining is not None:
            _rate["remaining"] = int(remaining)
        if reset is not None:
            _rate["reset"] = float(reset)
    except (TypeError, ValueError):  # pragma: no cover - odd proxies
        pass


def _rate_path(config: Any):
    return config.settings.data_path / "github_rate.json"


_rate_restored = False


_saved_rate: dict[str, Any] = {"remaining": None, "reset": 0.0}


def save_rate(config: Any) -> None:
    """Remember the window across restarts, so a deploy does not re-spend it."""
    import json

    if _rate["remaining"] is None:
        return
    if (_saved_rate["remaining"], _saved_rate["reset"]) == (_rate["remaining"], _rate["reset"]):
        return                                  # nothing moved since the last write
    try:
        from app.config import atomic_write_json

        atomic_write_json(_rate_path(config), dict(_rate))
    except Exception as exc:  # pragma: no cover - read-only data dir
        log.debug("github rate state unwritable (%s): %s", config, exc)
        return
    _saved_rate.update({"remaining": _rate["remaining"], "reset": _rate["reset"]})


def restore_rate(config: Any) -> None:
    """Adopt the quota state the previous process learned.

    `_rate` used to live only in memory, so every restart began certain that
    GitHub would answer - and spent the remaining hour's credits discovering
    otherwise. Eight deploys in one evening is eight fresh rounds of 27 repos,
    which is how both GitHub sources ended each run reporting "配额已用完".
    """
    global _rate_restored
    import json
    import time

    if _rate_restored or _rate["remaining"] is not None:
        return
    _rate_restored = True
    try:
        stored = json.loads(_rate_path(config).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except Exception as exc:  # a broken file must not stop collection
        log.debug("github rate state unreadable: %s", exc)
        return
    try:
        remaining = None if stored.get("remaining") is None else int(stored["remaining"])
        reset = float(stored.get("reset") or 0.0)
    except (TypeError, ValueError):  # pragma: no cover
        return
    if remaining == 0 and reset > time.time():
        _rate["remaining"], _rate["reset"] = 0, reset
        _saved_rate.update({"remaining": 0, "reset": reset})


def rate_block_reason(config: Any = None) -> str | None:
    """Why GitHub calls cannot work right now, or None if they can."""
    import time as _time

    if config is not None and _rate["remaining"] is None:
        restore_rate(config)
    if _rate["remaining"] != 0:
        return None
    if _rate["reset"] and _time.time() < _rate["reset"]:
        minutes = int((_rate["reset"] - _time.time()) / 60) + 1
        return f"{RATE_HINT}（约 {minutes} 分钟后恢复）"
    _rate["remaining"] = None      # window rolled over; let the next call probe again
    return None


class CachedResponse:
    """Stands in for an httpx.Response when GitHub answered 304 Not Modified."""

    status_code = 200

    def __init__(self, payload: Any, headers: Any = None) -> None:
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> Any:
        if isinstance(self._payload, str):
            import json

            self._payload = json.loads(self._payload or "null")
        return self._payload


_etag_cache: dict[str, Any] | None = None

# A search response is ~200KB and its query string changes daily, so caching
# bodies would grow the file without ever getting a hit.
MAX_CACHED_BODY = 400_000
# 但单条有上限不够：400 条 × 400KB 是 160MB，而每轮脏了就要整份 `json.dumps` 重写。
# 在 475MB、aiogram 自己就吃 ~106MB 的那台机器上，光这一个字符串就能把进程顶到 OOM，
# 磁盘也一起交代。真机 2026-10-01：27 条 = 982,888 字节，最大一条 299,837（中位数 9,680）。
MAX_CACHE_BYTES = 8_000_000


def _etag_path(config: Any):
    return config.settings.data_path / "github_etags.json"


def load_etags(config: Any) -> dict[str, Any]:
    """Conditional-request validators, kept across restarts.

    GitHub does not count a 304 against the rate limit, so this is what makes
    the anonymous 60/hour budget usable at all: 27 watched repos that did not
    change cost nothing after the first round.
    """
    global _etag_cache
    if _etag_cache is not None:
        return _etag_cache
    import json

    path = _etag_path(config)
    try:
        _etag_cache = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except Exception as exc:  # a broken cache must never stop collection
        log.warning("github etag cache unreadable (%s): %s", path, exc)
        _etag_cache = {}
    if not isinstance(_etag_cache, dict):
        _etag_cache = {}
    return _etag_cache


def save_etags(config: Any, cache: dict[str, Any]) -> None:
    """落盘前把总量压回天花板，而且**要丢就丢整条记录**。

    只删 body、留着 etag 是最危险的折中：下一轮照样带 `If-None-Match` 去问，
    GitHub 免费回一个没有正文的 304，采集器就把那个仓库读成"这一轮没发布"。
    整条丢掉只让下一轮多花一次配额；读成"没新闻"是谎报，花配额是成本。
    """
    import json

    from app.config import atomic_write_json

    trimmed = dict(list(cache.items())[-400:])       # newest entries only
    while len(trimmed) > 1 and len(json.dumps(trimmed, ensure_ascii=False)) > MAX_CACHE_BYTES:
        coldest = min(trimmed, key=lambda k: str((trimmed[k] or {}).get("at") or ""))
        trimmed.pop(coldest)
    try:
        atomic_write_json(_etag_path(config), trimmed)
    except Exception as exc:  # pragma: no cover - read-only data dir
        log.warning("github etag cache unwritable: %s", exc)


@register
class GitHubCollector(BaseCollector):
    type = "github"

    # class-level: one budget is shared by every GitHub source in the process
    _spent_checked_at: float = 0.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._etag_dirty = False

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        token = self.config.settings.github_token or self.source.get("token")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def get(self, url: str, **kwargs: Any):
        """Guard the shared anonymous budget, and ask for 304s instead of bodies.

        Without the guard, an exhausted quota looked like "nothing new": every repo
        403'd, each 403 was logged at debug and skipped, and the round reported
        `0 new of 0 fetched` - indistinguishable from a quiet internet.

        Without conditional requests the guard fires constantly instead: 27 watched
        repos cost 27 of 60 anonymous credits per round, so the budget died twice
        an hour. An unchanged repo now costs 0 (GitHub does not count a 304),
        which is what makes the no-token setup actually work.
        """
        blocked = rate_block_reason(self.config)
        if blocked:
            raise SourceBudget(blocked)
        key = self._cache_key(url, kwargs.get("params"))
        entry = load_etags(self.config).get(key) if key else None
        # 只有"回放得起 304"的记录才配发验证器：etag 在、body 不在时，GitHub 会免费回
        # 一个空正文，而 233 行那个回放分支要求 body 存在——于是正文缺失的 304 原样返回，
        # 采集器把这个仓库读成"这一轮没有发布"。花一次配额可以，读成没新闻不行。
        if isinstance(entry, dict) and entry.get("etag") and entry.get("body"):
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault("If-None-Match", str(entry["etag"]))
            kwargs["headers"] = headers
        try:
            response = await super().get(url, **kwargs)
        except Exception as exc:
            # base.get() raises before we can read the headers, and a 403 body is
            # exactly what tells us the quota is gone. /rate_limit is unmetered,
            # so ask it instead of guessing.
            if await self._confirm_spent(str(exc)):
                raise SourceBudget(rate_block_reason(self.config) or RATE_HINT) from exc
            raise
        note_rate(getattr(response, "headers", None))
        status = getattr(response, "status_code", 200)
        if status == 403 and _rate["remaining"] == 0:
            raise SourceBudget(rate_block_reason(self.config) or RATE_HINT)
        if status == 304 and isinstance(entry, dict) and entry.get("body"):
            _stats["not_modified"] += 1
            return CachedResponse(entry["body"], getattr(response, "headers", None))
        if status == 200:
            _stats["metered"] += 1
            if key:
                self._store(key, response)
        return response

    @staticmethod
    def _cache_key(url: str, params: Any) -> str | None:
        """Only plain endpoint calls are worth a validator.

        Query-carrying searches embed a date that changes every round, so their
        keys would never repeat; caching their bodies would only grow the file.
        """
        return None if params else url

    def _store(self, key: str, response: Any) -> None:
        etag = (getattr(response, "headers", None) or {}).get("etag")
        body = getattr(response, "text", None)
        if not etag or not body or len(body) > MAX_CACHED_BODY:
            return
        import json

        try:
            json.loads(body)                     # never store a half-written payload
        except Exception:  # noqa: BLE001 - not JSON: nothing to replay
            return
        cache = load_etags(self.config)
        record = {"etag": str(etag), "body": body, "at": _now_iso()}
        if cache.get(key) == record:
            return
        cache[key] = record
        self._etag_dirty = True

    async def _confirm_spent(self, error_text: str) -> bool:
        """True when GitHub says the anonymous budget is gone."""
        if "403" not in error_text and "429" not in error_text:
            return False
        import time as _time

        if _time.time() < self._spent_checked_at:
            return _rate["remaining"] == 0
        self._spent_checked_at = _time.time() + 60
        try:
            response = await super().get(f"{API}/rate_limit", headers=self._headers(),
                                         attempts=1)
        except Exception:  # noqa: BLE001 - cannot confirm; let the original error stand
            return False
        note_rate(getattr(response, "headers", None))
        if response.status_code == 200:
            try:
                core = (response.json().get("resources") or {}).get("core") or {}
                if core.get("remaining") is not None:
                    _rate["remaining"] = int(core["remaining"])
                if core.get("reset") is not None:
                    _rate["reset"] = float(core["reset"])
            except (ValueError, TypeError, AttributeError):
                pass
        return _rate["remaining"] == 0

    async def fetch(self) -> list[dict[str, Any]]:
        mode = str(self.source.get("mode", "search")).lower()
        started = dict(_stats)
        try:
            if mode in {"release", "releases"}:
                return await self._releases()
            if mode in {"org", "organization"}:
                return await self._org()
            return await self._search()
        finally:
            # One write per round, not per repo: the file holds every body.
            if self._etag_dirty:
                self._etag_dirty = False
                save_etags(self.config, load_etags(self.config))
            # Outside the dirty check on purpose: a round that changed no bodies
            # still learned the quota number, and that is the part a restart needs.
            save_rate(self.config)
            self._report(started)

    def _report(self, started: dict[str, int]) -> None:
        """Say what this round cost, because '0 new' used to be ambiguous.

        A quiet internet and a dead API budget now look different in the journal,
        which is the difference between waiting and editing /etc/ai-news-radar/env.
        """
        metered = _stats["metered"] - started["metered"]
        cached = _stats["not_modified"] - started["not_modified"]
        empty = _stats["empty_skipped"] - started["empty_skipped"]
        if not (metered or cached or empty):
            return
        log.info("github %s: %d metered + %d free 304 + %d known-empty, quota left=%s%s",
                 self.name, metered, cached, empty, _rate["remaining"],
                 "" if self.config.settings.github_token else " (anonymous: set GITHUB_TOKEN for 5000/h)")

    def _known_empty(self, url: str) -> bool:
        entry = load_etags(self.config).get(url)
        until = _parse((entry or {}).get("missing_until")) if isinstance(entry, dict) else None
        return bool(until and until > datetime.utcnow())

    def _mark_empty(self, url: str) -> None:
        """Remember a 404 for a while - but not forever.

        Only a real 404 gets here, so a repo publishing its first release is
        found at most `empty_hours` late; a timeout or a quota 403 is retried on
        the very next round.
        """
        hours = as_float(self.source.get("empty_hours"), DEFAULT_EMPTY_HOURS)
        until = datetime.utcnow() + timedelta(hours=max(0.1, hours))
        cache = load_etags(self.config)
        record = cache.get(url) if isinstance(cache.get(url), dict) else {}
        cache[url] = {**record, "missing_until": until.isoformat(timespec="seconds")}
        self._etag_dirty = True

    # ---------------------------------------------------------- search mode
    def _bases(self) -> list[str]:
        """One query skeleton per `searches:` entry - never their intersection.

        GitHub ANDs qualifiers, so the previous single base built from
        `[{topic: ai}, {topic: llm}, {topic: agents}]` asked for repos carrying
        all three tags: 4 hits in two weeks against 26 for `topic:ai` alone.
        """
        entries = self.source.get("searches") or [{"topic": "ai"}]
        language = clean_text(self.source.get("language") or "")
        bases: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            clauses: list[str] = []
            if entry.get("topic"):
                clauses.append(f"topic:{entry['topic']}")
            if entry.get("query"):
                clauses.append(str(entry["query"]))
            if entry.get("repo"):
                clauses.append(f"repo:{entry['repo']}")
            if language:
                clauses.append(f"language:{language}")
            base = " ".join(clauses)
            if base and base not in bases:
                bases.append(base)
        return bases or ["topic:ai"]

    async def _search(self) -> list[dict[str, Any]]:
        days = int(self.source.get("recent_days", 7))
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        # "created" has a star floor: without one it returns repos that were
        # published minutes ago with nobody watching, and the digest fills up
        # with "someone/first-commit (0 stars)" (seen in the production DB).
        # "pushed" is sorted by recency, not stars - ordered by stars it handed
        # back the same 25 famous repos every round, which dedup then dropped,
        # so the source cost 2 credits and produced "0 new of 25" forever.
        new_floor = int(self.source.get("min_new_stars", 10))
        min_stars = int(self.source.get("min_stars", 300))
        queries: list[tuple[str, str, int]] = []
        for base in self._bases():
            queries.append((f"{base} created:>{since} stars:>={new_floor}", "stars", new_floor))
            queries.append((f"{base} pushed:>{since} stars:>={min_stars}", "updated", min_stars))
        # Each search costs one credit out of the same 60/hour the 27 watched
        # repos need, so the topic list is capped instead of run in full.
        # Set `max_search_calls: 0` to lift the cap once GITHUB_TOKEN is set.
        cap = as_int(self.source.get("max_search_calls"), 2)
        queries = queries[:cap] if cap > 0 else queries
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        below_floor = 0
        for query, sort, floor in queries:
            params = {"q": query, "sort": sort, "order": "desc",
                      "per_page": min(30, self.limit())}
            try:
                response = await self.get(f"{API}/search/repositories", params=params,
                                          headers=self._headers(), attempts=2)
            except Exception as exc:
                raise CollectorError(f"GitHub search failed: {exc}") from exc
            payload = response.json()
            for repo in payload.get("items", []):
                key = str(repo.get("full_name") or repo.get("html_url") or "")
                if not key or key in seen:
                    continue
                seen.add(key)
                # Re-check the floor on the answer. `stars:>=N` is a search-index
                # promise, and the index lags: 30 rows of "(0 stars)" spam repos got
                # through it on 2026-09-25 and one more on 2026-09-25 21:24, and at
                # score 54 a 0-star repo is good enough for a briefing slot.
                if int(repo.get("stargazers_count") or 0) < floor:
                    below_floor += 1
                    continue
                items.append(self._repo_item(repo))
            if len(items) >= self.limit():
                break
        if below_floor:
            log.info("github %s: dropped %d repo(s) under the star floor",
                     self.source.get("name"), below_floor)
        return items[: self.limit()]

    def _repo_item(self, repo: dict[str, Any]) -> dict[str, Any]:
        stars = int(repo.get("stargazers_count") or 0)
        description = clean_text(repo.get("description")) or "(no description)"
        topics = ", ".join(repo.get("topics") or [])
        return {
            "title": f"{repo.get('full_name')} ({stars:,} stars)",
            "url": repo.get("html_url"),
            "author": (repo.get("owner") or {}).get("login"),
            "content": (
                f"{description}\n\n"
                f"Language: {repo.get('language') or 'n/a'}  Stars: {stars:,}  "
                f"Forks: {int(repo.get('forks_count') or 0):,}\n"
                f"Topics: {topics or 'n/a'}\n"
                f"Created: {repo.get('created_at')}  Last push: {repo.get('pushed_at')}\n"
                f"License: {(repo.get('license') or {}).get('spdx_id') or 'n/a'}"
            ),
            "published_at": _parse(repo.get("pushed_at") or repo.get("created_at")),
            "community_heat": stars,
            "meta": {
                "repo": repo.get("full_name"),
                "stars": stars,
                "forks": int(repo.get("forks_count") or 0),
                "language": repo.get("language"),
                "topics": (repo.get("topics") or [])[:10],
                "description": description,
            },
        }

    # --------------------------------------------------------- releases mode
    async def _releases(self) -> list[dict[str, Any]]:
        repos = [str(r) for r in (self.source.get("repositories") or [])]
        items: list[dict[str, Any]] = []
        for name in repos:
            url = f"{API}/repos/{name.strip('/').strip()}/releases/latest"
            if self._known_empty(url):
                # Repos with no releases answered 404 in every past round and will
                # do so again; re-asking them burned budget that has no other use.
                # `empty_hours` below decides how long that answer stays valid.
                _stats["empty_skipped"] += 1
                continue
            try:
                response = await self.get(url, headers=self._headers(), attempts=2)
            except Exception as exc:  # 404 = the repo has no releases: skip it
                if rate_block_reason(self.config):
                    raise SourceBudget(rate_block_reason(self.config)) from exc
                if _looks_like_missing(str(exc)):
                    self._mark_empty(url)
                log.debug("no latest release for %s: %s", name, exc)
                continue
            if response.status_code != 200:
                continue
            release = response.json()
            if not release.get("html_url"):
                continue
            published = _parse(release.get("published_at"))
            if published and published < self.since(int(self.source.get("fresh_hours", 72))):
                continue
            body = clean_text(release.get("body")) or ""
            items.append(
                {
                    "title": f"{release.get('name') or release.get('tag_name')} released in {name}",
                    "url": release["html_url"],
                    "author": ((release.get("author") or {}).get("login")),
                    "content": (f"Release {release.get('tag_name')} of {name}.\n\n{body[:4000]}")[:12000],
                    "published_at": published,
                    "meta": {"repo": name, "tag": release.get("tag_name"), "prerelease": release.get("prerelease")},
                }
            )
        # Newest first before the cap: truncating in config order would starve
        # whichever repos happen to be listed last, forever.
        items.sort(key=lambda item: item.get("published_at") or datetime.min, reverse=True)
        return items[: self.limit()]

    # ------------------------------------------------------------ org mode
    async def _org(self) -> list[dict[str, Any]]:
        org = clean_text(self.source.get("organization") or "")
        if not org:
            raise CollectorError("github org collector needs `organization:`")
        params = {"per_page": min(50, self.limit()), "sort": "updated", "direction": "desc"}
        try:
            response = await self.get(f"{API}/orgs/{org}/repos", params=params, headers=self._headers())
        except Exception as exc:
            raise CollectorError(f"GitHub org fetch failed: {exc}") from exc
        return [self._repo_item(repo) for repo in response.json() if isinstance(repo, dict)]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _looks_like_missing(error_text: str) -> bool:
    """Only the real 404 from `BaseCollector.get` means "no releases here".

    A timeout or a quota 403 must not be remembered as an answer, or one bad
    round would hide a repo's first release for hours.
    """
    return "HTTP 404" in error_text


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    # Every timestamp in this app is naive UTC; comparing naive with aware
    # would raise on the "fresh since" check above.
    return to_utc_naive(parsed)
