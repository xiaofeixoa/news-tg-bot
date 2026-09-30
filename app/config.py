"""Configuration: env vars via pydantic-settings, tunables via YAML.

Secrets only ever come from the environment (.env), never from YAML or code.
"""

from __future__ import annotations

import os
import string
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env_files() -> tuple[str, ...]:
    """Config files loaded for direct (non-systemd) runs.

    Under systemd the unit's EnvironmentFile supplies everything, but an
    operator running `scripts/telegram_smoke.py` by hand would otherwise get an
    empty allowlist and a confusing "ALLOWED_CHAT_IDS 为空" message.

    Only paths this process may actually read are listed: the deployment keeps
    /etc/ai-news-radar/env at mode 600 owned by root, and PID 1 can read it as
    EnvironmentFile while the unprivileged service user cannot - asking
    pydantic-settings to open it anyway would turn every service start into a
    permission error.
    """
    explicit = os.getenv("ENV_FILE")
    if explicit:
        return (explicit,)
    candidates = (PROJECT_ROOT / ".env", Path("/etc/ai-news-radar/env"))
    return tuple(str(path) for path in candidates if path.is_file() and os.access(path, os.R_OK))


class Settings(BaseSettings):
    """Environment-driven settings (design doc section 33)."""

    model_config = SettingsConfigDict(
        env_file=_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "development"
    log_level: str = "INFO"

    telegram_bot_token: str = ""
    allowed_chat_ids: str = ""
    # Telegram's API is blocked from some networks; aiogram accepts an http(s)
    # proxy here (socks5:// needs the optional aiohttp_socks dependency).
    telegram_proxy: str = ""

    database_url: str = "sqlite:///data/news.db"
    data_dir: str = "data"
    config_dir: str = "config"
    log_dir: str = "logs"

    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_light_model: str = ""
    llm_strong_model: str = ""
    llm_timeout: float = 60.0
    llm_max_retries: int = 2

    timezone: str = "Asia/Shanghai"
    rss_fetch_interval: int = 600
    hn_fetch_interval: int = 600
    github_fetch_interval: int = 1800
    reddit_fetch_interval: int = 1800
    arxiv_fetch_interval: int = 10800
    youtube_fetch_interval: int = 1800
    process_interval: int = 600

    daily_digest_time: str = "08:00"
    evening_digest_time: str = "20:00"
    min_article_score: float = 45

    breaking_news_enabled: bool = True
    breaking_news_threshold: float = 90
    max_breaking_news_per_day: int = 5
    breaking_cooldown_minutes: int = 60

    github_token: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def chat_id_whitelist(self) -> set[int]:
        return {int(x) for x in self.allowed_chat_ids.replace(";", ",").split(",") if x.strip().lstrip("-").isdigit()}

    def is_allowed_chat(self, chat_id: int | None) -> bool:
        """Fail closed: an unset allowlist means nobody may use the bot."""
        return chat_id is not None and chat_id in self.chat_id_whitelist

    @property
    def project_root(self) -> Path:
        return PROJECT_ROOT

    @property
    def config_path(self) -> Path:
        return self._resolve(self.config_dir)

    @property
    def data_path(self) -> Path:
        return self._resolve(self.data_dir)

    @property
    def log_path(self) -> Path:
        return self._resolve(self.log_dir)

    def _resolve(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else PROJECT_ROOT / path

    @property
    def sqlalchemy_url(self) -> str:
        """Normalise sqlite:///relative.db into an absolute file URL.

        Relative paths resolve against the project root, so `sqlite:///data/news.db`
        means <project>/data/news.db and not <project>/data/data/news.db.
        """
        url = self.database_url
        if url.startswith("sqlite:///") and ":memory:" not in url:
            raw = url[len("sqlite:///") :]
            if raw and not Path(raw).is_absolute():
                url = "sqlite:///" + str((PROJECT_ROOT / raw).resolve()).replace("\\", "/")
        return url

    def llm_model_for(self, tier: str) -> str:
        if tier == "strong":
            return self.llm_strong_model or self.llm_model
        if tier == "light":
            return self.llm_light_model or self.llm_model
        return self.llm_model

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)


@dataclass
class AppConfig:
    """Everything the app needs: env settings + parsed YAML files."""

    settings: Settings
    raw: dict[str, Any]
    sources: list[dict[str, Any]] = field(default_factory=list)
    categories: dict[str, Any] = field(default_factory=dict)
    prompts: dict[str, str] = field(default_factory=dict)
    free: dict[str, Any] = field(default_factory=dict)

    @property
    def free_terms(self) -> dict[str, Any]:
        """Vocabulary for the /免费 detector (config/free_offers.yaml)."""
        return self.free or {}

    def register_model_names(self, names: Iterable[str]) -> int:
        """Merge runtime-known model names into the /免费 vocabulary.

        The pricing gateway's list changes weekly; asking anybody to keep
        config/free_offers.yaml in step with it would never actually happen, so
        the live snapshot feeds the detector instead.
        """
        models = self.free.setdefault("models", [])
        if not isinstance(models, list):
            return 0
        added = 0
        for name in names:
            token = str(name).strip().lower()
            if len(token) >= 4 and token not in models:
                models.append(token)
                added += 1
        return added

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.raw
        for key in path.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    # ---- convenience views -------------------------------------------
    @property
    def ai_enabled(self) -> bool:
        """Can this run actually ask a model? Costs nothing to answer.

        Several features (breaking news, briefing summaries) have to behave
        differently when there is no key, and `LLMService.enabled` is the same
        two conditions - so the check lives here rather than requiring callers
        to build a client.
        """
        return bool(self.get("llm.enabled", True)) and self.settings.llm_configured

    @property
    def enabled_sources(self) -> list[dict[str, Any]]:
        return [s for s in self.sources if s.get("enabled", True)]

    def sources_of_type(self, type_name: str) -> list[dict[str, Any]]:
        return [s for s in self.enabled_sources if s.get("type", "rss") == type_name]

    def source_by_name(self, name: str) -> dict[str, Any] | None:
        return next((s for s in self.sources if s.get("name") == name), None)

    # ---- taxonomy views (config/categories.yaml) ----------------------
    @property
    def category_names(self) -> list[str]:
        return list(self.categories.get("top_categories") or [])

    @property
    def fallback_category(self) -> str:
        return self.categories.get("fallback_category", "Other")

    def category_meta(self, name: str) -> dict[str, Any]:
        return (self.categories.get("categories") or {}).get(name, {}) or {}

    def category_label(self, name: str | None) -> str:
        """Chinese column heading for output.

        The taxonomy keys stay English because they are also the prompt vocabulary
        and the DB column, but a briefing section titled "🤖 AI Models" is English
        chrome we control - and he reads Chinese.

        The last line used to be `return name`, so any key missing from
        categories.yaml printed its internal identifier straight into a Chinese
        briefing (measured reachable: the echo is still latent on live data - all 8
        stored categories have labels - but the invariant "his screen never shows an
        internal key" is what he asked for). An unlabelled key now renders as the
        fallback label and warns the operator once, because the fix belongs in
        config/categories.yaml and a silent English word is how nobody notices it.
        """
        if not name:
            return self.fallback_label
        label = self.category_meta(name).get("label")
        if label:
            return str(label)
        if name == self.fallback_category:
            return self.fallback_label
        _warn_missing_label("分类", name, "config/categories.yaml 的 categories",
                            self.fallback_label)
        return self.fallback_label

    @property
    def fallback_label(self) -> str:
        return str(self.categories.get("fallback_label") or "其他")

    def source_type_label(self, type_name: str | None) -> str:
        """/sources 与 --self-check 里的采集器类型：内部取值不该直接印给他看。"""
        label = self.get(f"labels.source_types.{(type_name or '').lower()}")
        if label:
            return str(label)
        if type_name:
            _warn_missing_label("来源类型", type_name, "settings.yaml 的 labels.source_types",
                                SOURCE_TYPE_FALLBACK)
        return SOURCE_TYPE_FALLBACK

    def quality_label(self, tier: str | None) -> str:
        return str(self.get(f"labels.quality.{(tier or 'C').upper()}") or f"{(tier or 'C').upper()} 级")

    def subcategories(self, name: str) -> list[str]:
        return list((self.category_meta(name).get("subcategories") or {}).keys())

    @property
    def all_subcategories(self) -> list[str]:
        out: list[str] = []
        for cat in self.category_names:
            out.extend(self.subcategories(cat))
        return out

    def keywords(self, name: str) -> list[str]:
        return [k.lower() for k in self.category_meta(name).get("keywords", [])]

    @property
    def filter_keywords(self) -> list[str]:
        return [k.lower() for k in self.get("filters.keywords", [])]

    @property
    def scoring_weights(self) -> dict[str, float]:
        weights = self.get("scoring.weights", {}) or {}
        total = sum(float(v) for v in weights.values()) or 1.0
        return {k: float(v) / total for k, v in weights.items()}

    def prompt(self, key: str) -> str:
        try:
            return self.prompts[key]
        except KeyError:  # pragma: no cover - config file is missing a key
            raise KeyError(f"prompt '{key}' not found in config/prompts.yaml") from None

    def render(self, key: str, **variables: Any) -> str:
        """Render a prompt with string.Template so JSON braces stay literal."""
        return string.Template(self.prompt(key)).safe_substitute(
            {k: ("null" if v is None else v) for k, v in variables.items()}
        )


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"missing config file: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"config file must contain a mapping: {path}")
    return data


def load_config(settings: Settings | None = None) -> AppConfig:
    settings = settings or Settings()
    cfg_dir = settings.config_path
    for folder in (settings.data_path, settings.log_path):
        folder.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        settings=settings,
        raw=_load_yaml(cfg_dir / "settings.yaml"),
        sources=_load_yaml(cfg_dir / "sources.yaml").get("sources", []) or [],
        categories=_load_yaml(cfg_dir / "categories.yaml"),
        prompts=_load_yaml(cfg_dir / "prompts.yaml"),
        free=_load_yaml(cfg_dir / "free_offers.yaml") if (cfg_dir / "free_offers.yaml").exists() else {},
    )


def _config_log(message: str, *args: Any) -> None:
    """Log a config problem without importing the logger at module scope (circular)."""
    try:
        from app.logging_setup import get_logger

        get_logger("app").warning(message, *args)
    except Exception:  # pragma: no cover - config must load even before logging
        pass


_MISSING_LABEL_WARNED: set[str] = set()
# One constant so the label the reader gets and the warning about it cannot disagree.
SOURCE_TYPE_FALLBACK = "其它来源"


def _warn_missing_label(kind: str, value: str, where: str, shown: str) -> None:
    """Say it once per value: the reader only ever sees Chinese, the operator fixes YAML.

    The message names the label the reader actually got, because a warning that
    says 「其他」 while the code returned 「其它来源」 is the kind of small lie that
    sends somebody to the wrong file.
    """
    key = f"{kind}={value}"
    if key in _MISSING_LABEL_WARNED:
        return
    _MISSING_LABEL_WARNED.add(key)
    _config_log("界面没有 %s %r 的中文名，按「%s」显示；请在 %s 补一条", kind, value, shown, where)


def as_int(value: Any, default: int) -> int:
    """int() that respects a configured 0.

    `int(cfg.get("x", 5) or 5)` silently turns "off" back into the default, which
    is how a cooldown of 0 minutes kept blocking alerts on a live server.
    """
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        _config_log("config value %r is not an integer, using %s", value, default)
        return default


def as_float(value: Any, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        _config_log("config value %r is not a number, using %s", value, default)
        return default


# The token `digest.deferral_worthwhile` matches, so it is exported rather than private.
QUIET_TOKEN = "静默时段"
_QUIET_OFF = {"", "off", "none", "false", "0"}


def _minutes_of_day(text: Any) -> int | None:
    parts = str(text or "").strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hours, minutes = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        return None
    return hours * 60 + minutes


def _quiet_bounds(raw: str) -> tuple[int, int, str] | None:
    """(start, end, label) in minutes-of-day, or None when quiet hours are off/broken.

    A window that ends *before* it starts wraps past midnight ("23:00-07:00"); an
    equal pair or a malformed string is not a window at all, and saying so once in
    the log beats quietly delivering at the hours he tried to switch off.
    """
    if str(raw or "").strip().lower() in _QUIET_OFF:
        return None
    start_text, _, end_text = str(raw).strip().partition("-")
    start, end = _minutes_of_day(start_text), _minutes_of_day(end_text)
    if start is None or end is None or start == end:
        _config_log("ignoring malformed breaking.quiet_hours=%r (expected e.g. \"23:00-07:00\")",
                    raw)
        return None
    return start, end, f"{start_text.strip()}-{end_text.strip()}"


def quiet_hours_text(config: "AppConfig | None" = None) -> str:
    """The configured window as "23:00-07:00", or "" when it is off or unusable."""
    bounds = _quiet_bounds((config or get_config()).get("breaking.quiet_hours"))
    return bounds[2] if bounds else ""


def _now_utc() -> "datetime":
    """One indirection so a wall-clock policy can be tested at a fixed instant.

    Without it the suite would pass at 14:00 UTC and fail at 16:00 UTC, because the
    quiet window is judged against the reader's local clock.
    """
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)


def local_now(zone_name: str | None = None, *, config: "AppConfig | None" = None,
              at: "datetime | None" = None):
    """What time it is for this reader, as an aware datetime on their wall clock.

    Single source for "now" across the delivery rules (the briefing's own clock,
    the daily caps, the quiet window), all of which read `_now_utc()` so a test can
    pin one instant instead of hoping the suite runs at a friendly hour.
    """
    from datetime import timezone as _tz
    from zoneinfo import ZoneInfo

    config = config or get_config()
    when = at or _now_utc()
    stamp = when if when.tzinfo is not None else when.replace(tzinfo=_tz.utc)
    return stamp.astimezone(ZoneInfo(zone_name or config.settings.timezone))


def local_day_start(zone_name: str | None = None, *, config: "AppConfig | None" = None,
                    at: "datetime | None" = None) -> "datetime":
    """Midnight of the reader's calendar day, as the naive-UTC stamp the ledger uses.

    Every `push_logs.created_at` is naive UTC, so a "how many today" question has to
    pick a midnight. The briefings already pick the *reader's* (`jobs._digest_due`),
    and the 突发/限免 daily caps picked UTC - i.e. 08:00 Beijing. Measured 2026-09-30:
    #1497 was refused 16 times between 05:22 and 07:52 Beijing with only 4 pushes in
    his own day, then went out at 08:02:40 the second the UTC bucket flipped; all 26
    cap refusals in the logs fall in the 05:00-08:00 Beijing band where the two
    definitions disagree. One definition, shared by all three callers.
    """
    from datetime import timezone as _tz

    return local_now(zone_name, config=config, at=at).replace(
        hour=0, minute=0, second=0, microsecond=0).astimezone(_tz.utc).replace(tzinfo=None)


def quiet_window(config: "AppConfig | None" = None, timezone_name: str | None = None, *,
                 at: "datetime | None" = None) -> str:
    """Why this reader must not be pinged right now, or "" when they may.

    Measured on the live box over 2026-09-16..30 (42 pushes in `push_logs`): 6 of 13
    突发 pushes and 5 of 17 限免 pushes landed between 23:00 and 07:00 Beijing time,
    two of them after 03:00 -
    while the briefing times *he* configured are 08:00 and 20:00. A quiet window drops
    nothing: the offer and the gateway ledger are only written after a delivery, and the
    突发 retry queue re-asks, so a 03:11 event arrives at 07:00 instead of waking anybody.

    The clock that matters is the reader's: every timestamp in the database is naive
    UTC, and reading "23:00" off UTC would silence the wrong eight hours for a +08:00
    reader - and the wrong reader entirely for another zone.
    """
    config = config or get_config()
    bounds = _quiet_bounds(config.get("breaking.quiet_hours"))
    if bounds is None:
        return ""
    start, end, label = bounds
    local = local_now(timezone_name, config=config, at=at)
    minutes = local.hour * 60 + local.minute
    inside = (minutes >= start or minutes < end) if start > end else (start <= minutes < end)
    if not inside:
        return ""
    return f"{QUIET_TOKEN} {label}（{local.tzinfo} 当地 {label.partition('-')[2]} 之后自动补发）"

def quiet_opens_in(config: "AppConfig | None" = None, timezone_name: str | None = None, *,
                   at: "datetime | None" = None) -> "float | None":
    """静默窗口还有多少小时放行；不在窗口内（含窗口关掉/写坏）时 None。

    与 `quiet_window()` 共用同一次 `_quiet_bounds` 解析和同一个读者钟——"窗口"这件事
    不能有两个定义，否则放行时刻与承诺时刻会各自漂移。
    """
    config = config or get_config()
    bounds = _quiet_bounds(config.get("breaking.quiet_hours"))
    if bounds is None:
        return None
    start, end, _label = bounds
    local = local_now(timezone_name, config=config, at=at)
    minutes = local.hour * 60 + local.minute
    inside = (minutes >= start or minutes < end) if start > end else (start <= minutes < end)
    if not inside:
        return None
    until = (end - minutes) % (24 * 60)
    return (until or 24 * 60) / 60.0


@lru_cache
def get_config() -> AppConfig:
    return load_config()


def reload_config() -> AppConfig:
    get_config.cache_clear()
    return get_config()
