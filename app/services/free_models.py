"""Live "what is free right now" check (the /免费 command's primary answer).

News collection only tells us that somebody *announced* a promo, and announcements
lag reality in both directions: a free tier can appear without any press, and a
press release can outlive the offer. So /免费 also asks a pricing API directly -
that is the only part of the answer that is verifiably true at the moment the
user asks.

Kept separate from the article pipeline on purpose: these are *states*, not
events. Stuffing them into `articles` would pollute the digests and make every
refresh look like breaking news.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.config import AppConfig, as_int, get_config
from app.logging_setup import get_logger

log = get_logger("app")

OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"


@dataclass
class FreeModel:
    """One model that costs 0 right now on a public gateway."""

    id: str
    name: str
    provider: str
    url: str
    context_length: int = 0
    listed_at: datetime | None = None
    free_since: datetime | None = None
    vendors: list[str] = field(default_factory=list)

    @property
    def since(self) -> datetime | None:
        return self.free_since or self.listed_at

    def days_free(self, now: float | None = None) -> int:
        start = self.since
        if start is None:
            return 0
        return max(0, int(((now or time.time()) - start.timestamp()) // 86400))


class FreeModelWatcher:
    """Cached snapshot of the free tier of a public model gateway."""

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()
        self._cache: list[FreeModel] = []
        self._cached_at = 0.0
        self._ever_failed = False
        # filled by _track_trend on every real snapshot
        self.newly_free: list[FreeModel] = []
        self.ended: list[str] = []

    # ------------------------------------------------------------------ cfg
    @property
    def cfg(self) -> dict[str, Any]:
        return dict(self.config.get("free.models", {}) or {})

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True))

    @property
    def state_path(self) -> Path:
        raw = str(self.cfg.get("state_file", "free_models.json"))
        path = Path(raw)
        return path if path.is_absolute() else self.config.settings.data_path / raw

    @property
    def source_name(self) -> str:
        """Label for the footer; `name:` in config, else derived from the host."""
        named = str(self.cfg.get("name") or "").strip()
        if named:
            return named
        from urllib.parse import urlparse

        host = urlparse(str(self.cfg.get("url") or OPENROUTER_MODELS)).hostname or ""
        parts = [p for p in host.split(".") if p and p not in {"com", "ai", "www"}]
        return "-".join(parts[:1]).title() or "网关"

    # ------------------------------------------------------------- fetching
    async def snapshot(self, *, force: bool = False) -> list[FreeModel]:
        """Free models, newest first. Empty list when disabled or unreachable."""
        if not self.enabled:
            return []
        ttl = float(self.cfg.get("ttl_minutes", 360)) * 60
        fresh = (time.time() - self._cached_at) < ttl
        if fresh and not force:
            return self._cache
        try:
            raw = await self._fetch()
        except Exception as exc:  # /免费 must still answer with the news list
            from app.collectors.base import describe_error

            log.warning("live free-model check failed: %s", describe_error(exc))
            self._ever_failed = True
            return self._cache if fresh else []
        models = self._parse(raw)
        seen = self._record_seen([m.id for m in models])
        for model in models:
            first = seen.get(model.id)
            if first:
                model.free_since = first
            model.vendors = _vendors(self.config, model)
        known = self.config.register_model_names(_vocabulary_names(models))
        self._cache, self._cached_at = models, time.time()
        self._track_trend(models)
        if known:
            log.info("free-offer vocabulary learned %d model name(s) from the gateway", known)
        log.info("live free-model check: %d free model(s) at source", len(models))
        return models

    async def _fetch(self) -> Any:
        from app.collectors.base import get_client

        url = str(self.cfg.get("url") or OPENROUTER_MODELS)
        client = await get_client()
        response = await client.get(url, headers={"Accept": "application/json"})
        response.raise_for_status()
        return response.json()

    # --------------------------------------------------------------- parsing
    def _parse(self, payload: Any) -> list[FreeModel]:
        if isinstance(payload, dict):
            payload = payload.get("data") or payload.get("models") or []
        min_ctx = as_int(self.cfg.get("min_context"), 0)
        # Keep the whole free tier cached: a filtered /免费 deepseek query must be
        # able to reach models that are not among the newest few.
        limit = as_int(self.cfg.get("cache_max"), 80)
        blocked = [str(t).lower() for t in (self.cfg.get("exclude") or [])]

        out: list[FreeModel] = []
        for entry in payload or []:
            if not isinstance(entry, dict):
                continue
            mid = str(entry.get("id") or "").strip()
            if not mid:
                continue
            pricing = entry.get("pricing") or {}
            if not _all_zero(pricing):
                continue  # not actually free: any token class costs money
            if _is_excluded(mid, entry, blocked):
                continue
            context = int(entry.get("context_length") or 0)
            if context and context < min_ctx:
                continue
            out.append(FreeModel(
                id=mid,
                name=str(entry.get("name") or mid),
                provider=mid.split("/")[0].replace("-", "."),
                url=f"https://openrouter.ai/{mid}",
                context_length=context,
                listed_at=_to_dt(entry.get("created")),
            ))
        out.sort(key=lambda m: (m.listed_at or datetime.min), reverse=True)
        return out[:limit]

    # ------------------------------------------------------------- bookkeeping
    def _read_state(self) -> dict[str, Any]:
        try:
            if self.state_path.is_file():
                return dict(json.loads(self.state_path.read_text(encoding="utf-8")))
        except Exception as exc:
            log.warning("free-model state unreadable (%s): %s", self.state_path, exc)
        return {}

    def _write_state(self, state: dict[str, Any]) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
        except Exception as exc:  # a read-only data dir must not break /免费
            log.warning("free-model state unwritable (%s): %s", self.state_path, exc)

    def _track_trend(self, models: Sequence[FreeModel]) -> None:
        """Remember this snapshot's free set so the next one can be diffed.

        "近期免费" is a question about change, and change needs a previous state:
        which models just became free, and which quietly stopped being free.
        """
        state = self._read_state()
        previous = set(state.get("last_free") or [])
        current = {model.id: model for model in models}
        if state.get("last_free_seeded"):
            self.newly_free = [m for mid, m in current.items() if mid not in previous]
            names = dict(state.get("names") or {})
            self.ended = [names.get(mid, mid) for mid in sorted(previous - set(current))]
        else:
            # First run: everything would look "new", which is not information.
            self.newly_free, self.ended = [], []
        names = dict(state.get("names") or {})
        for mid, model in current.items():
            names[mid] = model.name or mid
        state["names"] = dict(list(names.items())[-400:])
        state["last_free"] = sorted(current)
        state["last_free_seeded"] = True
        self._write_state(state)

    def trend(self) -> tuple[list[FreeModel], list[str]]:
        """(newly free since the last snapshot, names that stopped being free)."""
        return list(self.newly_free), list(self.ended)

    def unannounced(self, models: Sequence[FreeModel]) -> list[FreeModel]:
        """Free models this install has never been told about.

        `first_seen` alone is not enough: the very first snapshot would call all
        sixteen of them "new", so the first pass only seeds the ledger.
        """
        state = self._read_state()
        if not state.get("announced_seeded"):
            return []
        ledger = set(state.get("announced") or [])
        return [model for model in models if model.id not in ledger]

    def mark_announced(self, ids: Sequence[str]) -> None:
        state = self._read_state()
        ledger = set(state.get("announced") or [])
        ledger.update(ids)
        state["announced"] = sorted(ledger)[-200:]
        state["announced_seeded"] = True
        self._write_state(state)

    def _record_seen(self, ids: Iterable[str]) -> dict[str, datetime]:
        """Remember when we first saw each free model, so "已免费 N 天" is real."""
        state = self._read_state()
        seen: dict[str, str] = dict(state.get("seen") or {})
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        for mid in ids:
            seen.setdefault(mid, now)
        # Keep the file bounded: offers that disappear are not worth tracking forever.
        if len(seen) > 400:
            seen = dict(sorted(seen.items(), key=lambda kv: kv[1])[-400:])
        state.update({"seen": seen, "last_checked": now, "source": str(self.cfg.get("url") or OPENROUTER_MODELS)})
        if not state.get("announced_seeded"):
            # Seed here so a fresh install does not announce the whole free tier
            # as "new" on its first alert run.
            state["announced"] = sorted(set(state.get("announced") or []) | set(seen))
            state["announced_seeded"] = True
        self._write_state(state)
        return {k: _to_iso(v) for k, v in seen.items()}

    @property
    def failed(self) -> bool:
        return self._ever_failed



def _vocabulary_names(models: Sequence[FreeModel]) -> list[str]:
    """Names the /免费 detector should recognise as model subjects."""
    names: list[str] = []
    for model in models:
        family = model.id.split("/")[-1].replace(":free", "").strip().lower()
        if family:
            names.append(family)
        provider = model.id.split("/")[0].strip().lower()
        if len(provider) >= 5:
            names.append(provider)
    return names


def _all_zero(pricing: dict[str, Any]) -> bool:
    """Free only when every priced token class is 0 - `prompt: 0, image: 0.004` is not."""
    if not pricing:
        return False
    priced = {k: v for k, v in pricing.items() if k != "pricing"}
    if not priced:
        return False
    try:
        return all(float(v or 0) == 0.0 for v in priced.values())
    except (TypeError, ValueError):
        return False


def _is_excluded(model_id: str, entry: dict[str, Any], blocked: list[str]) -> bool:
    haystack = f"{model_id} {entry.get('name') or ''}".lower()
    return any(needle and needle in haystack for needle in blocked)


def _to_dt(value: Any) -> datetime | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, timezone.utc).replace(tzinfo=None)


def _to_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _vendors(config: AppConfig, model: FreeModel) -> list[str]:
    """Which known tool/model families this free entry belongs to.

    Straight substring test against the /免费 vocabulary, so it stays honest:
    it reports that the model *is* e.g. a DeepSeek model, not that some agent
    officially supports it.
    """
    tools = (config.free_terms or {}).get("tools") or {}
    lowered = f"{model.id} {model.name}".lower()
    hits = [name for needle, name in ((t.lower(), t) for t in tools) if needle in lowered]
    return sorted(set(hits))[:4]


_watcher: FreeModelWatcher | None = None


def get_free_model_watcher(config: AppConfig | None = None) -> FreeModelWatcher:
    global _watcher
    if _watcher is None or (config is not None and config is not _watcher.config):
        _watcher = FreeModelWatcher(config)
    return _watcher
