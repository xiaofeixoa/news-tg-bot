"""Chinese output: translate non-Chinese titles/summaries.

The user reads Chinese, but every upstream feed is English. Two routes:

* ``llm``       - best quality; batched through the configured OpenAI-compatible
                  model. Used automatically when LLM_* is set.
* ``mymemory``  - key-free fallback (MyMemory free tier). Works from a datacenter
                  IP, but the anonymous quota is small, so it is budgeted:
                  a per-run cap, a per-day cap, and a length cap per request.

Google's free web endpoint and Edge's anonymous token are tried only as extras
and are deliberately not the default: from cloud IPs Google returns a "Sorry"
page and edge.microsoft.com/translate/auth returns an empty body.

Translation never blocks the news flow: every failure path leaves the article
in its original language and the pipeline carries on.
"""

from __future__ import annotations

import asyncio
import time
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

import httpx

from app.config import AppConfig, get_config
from app.logging_setup import get_logger

log = get_logger("llm")

MYMEMORY = "https://api.mymemory.translated.net/get"
MYMEMORY_MAX_CHARS = 480          # the free tier rejects longer segments
TARGET = "zh-CN"


def wants_chinese(config: AppConfig | None = None) -> bool:
    config = config or get_config()
    return str(config.get("app.language", "zh")).lower().startswith("zh")


def has_cjk(text: str | None) -> bool:
    """Any Chinese character present - the acceptance test for a translation."""
    return any("一" <= ch <= "鿿" for ch in (text or ""))


# Free machines translate company and product names into Chinese words: the live
# library holds "Introducing Gemini Omni" -> "双子座 Omni 简介", "How Hugging Face
# Inference Endpoints..." -> "拥抱人脸推理端点…", "Claude's Load-Bearing Seams" ->
# "克劳德承重接缝", and today's 突发 alert called Anthropic "人类技术". A wrong
# proper noun is not a soft translation, it is a false statement about a company.
#
# `config/settings.yaml -> translate.keep_terms` names the brands that have no
# accepted Chinese form (微软/亚马逊 are correct renderings and must not be
# guarded). Those names travel as circled-digit placeholders, which the engines
# treat as punctuation; `kept_intact` rejects any answer that ate one, so a route
# that mangles names loses the item to the next route instead of shipping junk.
_MAX_KEPT = 35


def _placeholder(index: int) -> str:
    """⑴…⒇ then ㉑…㉟ - circled rather than bracketed digits.

    Live titles already contain "【PM公益站】", and a placeholder that can collide
    with source text would rename somebody's product.
    """
    if 1 <= index <= 20:
        return chr(0x2474 + index - 1)          # ⑴ … ⒇
    return chr(0x3251 + index - 21)             # ㉑ … ㉟


def _boundary(term: str) -> str:
    """Look-arounds for one guarded term.

    A brand is alphanumeric and must not match inside a longer word, so digits
    count as "inside" for it: guarding the "Qwen" in "Qwen3-TTS", or the "GPT-5"
    in "GPT-50", would be a different claim about which product the row is about.
    A slash unit is not a word: "50t/s" has its digit glued to the symbol, and the
    alphanumeric lookbehind used to reject exactly the case worth guarding - which
    is how a live row reached a reader as "50吨/秒".
    """
    if "/" in term:                                 # "t/s", "tokens/s", "KB/token"
        return f"(?<![A-Za-z]){re.escape(term)}(?![A-Za-z])"
    return f"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])"


def term_in(text: str | None, term: str) -> bool:
    """Does this text carry `term` as a standalone token, case-insensitively?"""
    return bool(text) and re.search(_boundary(term), text, re.I) is not None


# 免密钥 MT 的义项错译：英文原文里确实有那个词，中文却给了另一个义项。
# (英文触发词，中文错写，应写作) —— 顺序要紧："模特儿"先于"模特"，
# "代理人工智能"必须先于通用的"代理人"，否则会写出"智能体工智能"。
SENSE_FIXES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("model", "models"), "模特儿", "模型"),
    (("model", "models"), "模特", "模型"),
    # 2026-09-28 从真库量的另两个义项：`车型` 2 行、`机型` 2 行，四行的英文里都确实
    # 有 model/models——"Which Local Models are the least 'Claude' sounding"→"哪些本地
    # 车型"（这条就在 09-28 08:04 发出的早报里）、"tiny models"→"微型机型"。
    (("model", "models"), "车型", "模型"),
    (("model", "models"), "机型", "模型"),
    (("agentic",), "代理人工智能", "自主智能体"),
    # 2026-09-28 量到的三个：`特工` 8 行（逐行查过原文，全是 OpenAI/前沿实验室的 AI
    # agent，没有一条是真特工报道）、`光学` 1 行（`OpenAI Feared "Optics"`→观感/形象）、
    # `黑客新闻` 1 行（Hacker News 是站点名，不该被译成中文）。
    # 特工同样要过英文门槛：真出现 "foreign agents" 时中文该是"外国代理人/特工"，
    # 所以下面那组"指人"的搭配会让开整条规则。
    (("ai agent", "ai agents", "agentic", "agent", "agents", "agent swarm", "agent swarms",
      "coding agent", "coding agents", "llm agent", "llm agents", "multi-agent", "ai safety"),
     "特工", "智能体"),
    (("optics",), "光学", "观感"),
    (("hacker news",), "黑客新闻", "Hacker News"),
    # agent 不能无条件改：真库 #323 是 `Feds Target AI Critics as "Foreign Agents"`
    # → "外国代理人"，那是法律术语、是对的。所以要英文出现 AI 语境搭配才动手
    # （搭配里含空格，`term_in` 的词边界照样管用）。
    (("ai agent", "ai agents", "agentic", "agent swarm", "agent swarms", "coding agent",
      "coding agents", "llm agent", "llm agents", "multi-agent", "ai safety"),
     "代理人", "智能体"),
)

# 这些"代理人/特工"指的是人/机构，不是 AI agent；中文里出现就让开整条规则。
AGENT_HUMAN_ONLY = ("外国代理人", "境外代理人", "保险代理人", "房产代理人", "专利代理人",
                    "货运代理人", "代理人签订", "委托代理人",
                    "外国特工", "双重特工", "特工组织", "特工头子", "间谍特工")


def fix_wrong_sense(text: str | None, english: str | None) -> str | None:
    """改掉"model→模特""agentic AI→代理人工智能"这类义项错译，只在英文确实带那个词时动手。

    2026-09-27 从真库量的：`模特` 2 行全是真错（这份语料没有走秀的模特）；
    含 `代理人` 的 7 行里 5 行是错义（#206/#199/#184 的 agentic AI、#484 的 agent swarms、
    #394 的 AI safety agents），#236 是 coding agent，而 **#323 的"外国代理人"是正确的**
    —— 所以这里按搭配收窄，而不是一见 agent 就改。
    量过之后仍**故意不动**的：`token→令牌`（LLM 语境的标准说法）、`protocol→协议`。
    """
    if not text or not english:
        return text
    out = text
    for keys, wrong, right in SENSE_FIXES:
        if wrong not in out:
            continue
        if wrong in ("代理人", "特工") and any(keep in out for keep in AGENT_HUMAN_ONLY):
            continue
        if any(term_in(english, key) for key in keys):
            out = out.replace(wrong, right)
    return out


def protect_terms(text: str, terms: Sequence[str]) -> tuple[str, dict[str, str]]:
    """Replace known brand names with pass-through placeholders.

    Adjacent hits become one placeholder: "GitHub Copilot" guarded as two tokens
    lets the engine put a word between them, which is how a live row came out as
    "…语音 SageMaker AI" with its "Amazon" left behind.
    """
    if not text or not terms:
        return text, {}
    # Longest first so "GitHub Copilot" wins over "GitHub".
    ordered = sorted({t for t in terms if t}, key=len, reverse=True)
    rx = re.compile("|".join(_boundary(t) for t in ordered), re.I)
    spans: list[tuple[int, int]] = []
    for match in rx.finditer(text):
        if spans and match.start() < spans[-1][1]:
            continue                                  # already inside a longer name
        if spans and not text[spans[-1][1]:match.start()].strip():
            spans[-1] = (spans[-1][0], match.end())    # same name, one gap of space
        else:
            spans.append((match.start(), match.end()))
    # The cap is on placeholders in *this string* (the circled digits run out at
    # 35), not on the vocabulary. Truncating the term list instead silently
    # un-guarded the shortest entries - which is exactly how "50t/s" got through
    # once the unit list pushed the brand list past the limit.
    spans = spans[:_MAX_KEPT]
    mapping: dict[str, str] = {}
    cursor, out = 0, []
    for index, (start, end) in enumerate(spans, start=1):
        token = _placeholder(index)
        mapping[token] = text[start:end]
        out.append(text[cursor:start])
        out.append(token)
        cursor = end
    if not mapping:
        return text, {}
    out.append(text[cursor:])
    guarded = "".join(out)
    return guarded, mapping


def restore_terms(zh: str, mapping: dict[str, str]) -> str:
    """Put the names back. A placeholder the engine swallowed stays untranslated."""
    out = zh or ""
    for token, term in mapping.items():
        out = out.replace(token, term)
    return out


def kept_intact(zh: str, mapping: dict[str, str]) -> bool:
    """Did the engine pass every placeholder through untouched?

    Google does; MyMemory turns "⒆" into "锘洪噾" or drops it and leaves the
    sentence without a subject. Both are worse than no translation: the caller
    falls through to the next route, and if none is left the line stays in
    English - which is the fallback he approved for spent quota.
    """
    return all(token in (zh or "") for token in mapping)


# "owner/repo (123 stars)" - a repository name, not a sentence. Translating it
# only burns free-tier quota and produces junk like "（ 0星）".
# Key-free routes, tried in this order by `provider: auto`.
FREE_ROUTES = ("mymemory", "google")

# The web widget's own endpoint: `translate.googleapis.com/translate_a/single`
# answers with an empty payload from cloud IPs, this one does not.
GOOGLE_WEB = "https://translate.google.com/translate_a/t"


def flatten_google(data: object) -> str:
    """The endpoint answers either ["zh", ...] or [[["zh", "en", null]], ...]."""
    if isinstance(data, str):
        return data
    if not isinstance(data, list) or not data:
        return ""
    first = data[0]
    if isinstance(first, str):
        return "".join(part for part in data if isinstance(part, str))
    if isinstance(first, list):
        return "".join(str(seg[0]) for seg in first if seg and isinstance(seg[0], str))
    return ""


# Machine translation transliterates brand names on repeat ("克劳德" for Claude),
# which reads as noise to anybody following this space. Restoring the Latin
# original costs no API call.
PROPER_NOUNS = {
    "克劳德": "Claude",
    "安索罗皮克": "Anthropic",
    "奥皮恩AI": "OpenAI",
    "开放人工智能": "OpenAI",
    "格明尼": "Gemini",
    "迪普西克": "DeepSeek",
    "米斯塔": "Mistral",
    "格罗克": "Grok",
    "梅塔": "Meta",
    "微软件": "Microsoft",
}


def restore_proper_nouns(text: str) -> str:
    for chinese, latin in PROPER_NOUNS.items():
        text = text.replace(chinese, latin)
    return text


REPO_TITLE_RE = re.compile(r"^[\w.\-]+/[\w.\-]+\s*\(.*\)\s*$")
# 光秃秃的 `owner/repo`：Reddit 的模型发布帖常拿仓库名当标题。免费 MT 对这种串
# **一个字都不返回**（实测 `translate_many` 回 `{}`，不是回显），所以它不可能靠翻译变中文，
# 只会让 `display_title` 回落成英文整行 —— 09-28 早报里就有这么一行
# （`LuffyTheFox/Swift-Qwen3.8-27B-Genesis-GGUF`）。和下面两个模板一样，给它一个不带断言的
# 中文框架：这确实是一个项目，至于是谁的、叫什么，原样保留。
SLUG_TITLE_RE = re.compile(r"^\s*([A-Za-z][\w.\-]*)/([A-Za-z][\w.\-]*)\s*$")

# Feed titles that are templates, not prose. Machine-translating them costs
# free-tier quota and produces wrong word order ("owner/repo中发布的v1.2") or
# nonsense ("（ 0星）"), so we compose the Chinese ourselves.
RELEASE_TITLE_RE = re.compile(
    r"^(?P<ver>.+?)\s+released in\s+(?P<repo>[\w.\-]+/[\w.\-]+)\s*$", re.I)
TRENDING_TITLE_RE = re.compile(
    r"^(?P<repo>[\w.\-]+/[\w.\-]+)\s*\((?P<stars>[\d,.]+)\s+stars?\)\s*$", re.I)


def localize_title(title: str | None) -> str | None:
    """Chinese for templated feed titles, or None if it is real prose."""
    text = (title or "").strip()
    if not text:
        return None
    match = RELEASE_TITLE_RE.match(text)
    if match:
        return f"{match.group('repo')} 发布 {match.group('ver')}"
    match = TRENDING_TITLE_RE.match(text)
    if match:
        return f"{match.group('repo')} 收获 {match.group('stars')} 星"
    match = SLUG_TITLE_RE.match(text)
    if match:
        return f"项目：{match.group(1)}/{match.group(2)}"
    return None


def needs_translation(text: str | None) -> bool:
    """True when the string is mostly latin prose worth translating."""
    if not text:
        return False
    if REPO_TITLE_RE.match(text.strip()) or localize_title(text) is not None:
        return False
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return latin > 8 and cjk < max(3, len(text) * 0.10)


@dataclass
class Budget:
    """Stops a free provider from burning its anonymous quota in one round."""

    per_run: int = 120
    per_day: int = 400
    used_run: int = 0
    used_day: int = 0
    day_of: str = ""

    def available(self) -> bool:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.day_of:
            self.day_of, self.used_day = today, 0
        return self.used_run < self.per_run and self.used_day < self.per_day

    def spend(self, count: int = 1) -> None:
        self.used_run += count
        self.used_day += count

    def reset_run(self) -> None:
        self.used_run = 0


class Translator:
    def __init__(self, config: AppConfig | None = None, *, llm: Any = None) -> None:
        self.config = config or get_config()
        self._llm = llm
        self._client: httpx.AsyncClient | None = None
        self.provider = str(self.config.get("translate.provider", "auto")).lower()
        self.budget = Budget(
            per_run=int(self.config.get("translate.per_run_limit", 120)),
            per_day=int(self.config.get("translate.daily_budget", 400)),
        )
        self.cache: dict[str, str] = {}
        self._down_until: dict[str, float] = {}
        # Names the free engines must not turn into Chinese words.
        self.keep_terms = [str(t) for t in (self.config.get("translate.keep_terms", []) or []) if str(t).strip()]
        # Measurement symbols ride the same guard: "50t/s" came back as "50吨/秒"
        # (tonnes per second) and "0.9 KB/token" as "0.9 KB/令牌" - both reached a
        # briefing. Bare "token" is deliberately NOT here: in "single-use approval
        # token" the machine's 令牌 is correct Chinese, and guarding the word would
        # turn a good sentence into untranslated noise.
        self.keep_terms += [str(t) for t in (self.config.get("translate.keep_units", []) or [])
                            if str(t).strip()]
        # Which route actually served the last batch: `mode()` reports the
        # configured preference, and provenance written to the DB must be true.
        self.last_route: str | None = None

    @property
    def llm(self) -> Any:
        if self._llm is None:
            from app.services.llm import get_llm

            self._llm = get_llm()
        return self._llm

    @property
    def enabled(self) -> bool:
        if not wants_chinese(self.config) or self.provider == "off":
            return False
        return bool(self.config.get("translate.enabled", True))

    def mode(self) -> str:
        if self.provider == "llm":
            return "llm"
        if self.provider in {"mymemory", "google"}:
            return self.provider
        return "llm" if self.llm.enabled else "mymemory"

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=8.0),
                                             follow_redirects=True,
                                             headers={"User-Agent": "AI-News-Radar/1.0"})
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # ------------------------------------------------------------- public API
    async def translate_many(self, texts: Sequence[str], *, hint: str = "title") -> dict[str, str]:
        """Translate a batch; returns {original: chinese}. Missing keys stay English."""
        if not self.enabled:
            return {}
        wanted = [t for t in dict.fromkeys(texts) if t and needs_translation(t)]
        if not wanted:
            return {}
        out: dict[str, str] = {t: self.cache[t] for t in wanted if t in self.cache}
        todo = [t for t in wanted if t not in out]
        if not todo:
            return out

        mode = self.mode()
        if mode == "llm":
            try:
                got = await self._via_llm(todo, hint=hint)
                out.update(got)
                self.budget.spend(1)  # one model call for the whole batch
                self.last_route = "llm"
                return out
            except Exception as exc:  # noqa: BLE001 - fall through to the free tier
                log.info("llm translation unavailable (%s), using %s", exc, mode)
        out.update(await self._via_free_routes(todo))
        return out

    async def _via_free_routes(self, todo: list[str]) -> dict[str, str]:
        """Try the key-free routes in order, handing the leftovers downstream.

        `auto` used to mean "MyMemory only": when its IP quota ran out it
        answered 429 with a warning body, which we correctly refused to store -
        and then quietly left the news in English for the rest of the day
        without ever asking the second provider.
        """
        provider = str(self.provider or "auto").lower()
        routes = list(FREE_ROUTES) if provider == "auto" else [provider] if provider in FREE_ROUTES else []
        remaining = list(todo)
        out: dict[str, str] = {}
        for route in routes:
            if not remaining:
                break
            if self._route_down(route):
                log.debug("translate route %s is in backoff; skipping it", route)
                continue
            try:
                got = await (self._via_mymemory(remaining) if route == "mymemory"
                             else self._via_google(remaining))
            except Exception as exc:  # noqa: BLE001 - the next route may still work
                log.info("translate route %s failed: %s", route, exc)
                continue
            if got:
                self.last_route = route
            out.update(got)
            remaining = [t for t in remaining if t not in got]
        if remaining:
            log.info("%d item(s) stay in English for now (free routes spent)", len(remaining))
        return out

    # ---------------------------------------------------------- route health
    def _route_down(self, route: str) -> bool:
        until = self._down_until.get(route, 0.0)
        return bool(until and time.time() < until)

    def _mark_route_down(self, route: str, seconds: float = 3600.0) -> None:
        # Only the transition is a warning: the digest asked twice per send and a
        # quota that lasts the evening printed the same line all night. What
        # happens next is the caller's business - the text stays English.
        if not self._route_down(route):
            log.warning("translate route %s is out of free quota; retrying in %d min, "
                        "untranslated lines stay in English", route, int(seconds / 60))
        self._down_until[route] = max(self._down_until.get(route, 0.0), time.time() + seconds)

    async def translate(self, text: str, *, hint: str = "title") -> str | None:
        got = await self.translate_many([text], hint=hint)
        return got.get(text)

    # -------------------------------------------------------------- providers
    async def _via_llm(self, texts: list[str], *, hint: str) -> dict[str, str]:
        prompt = self.config.render(
            "translate_prompt",
            items="\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts[:60])),
            kind="标题" if hint == "title" else "摘要",
        )
        data = await self.llm.ask_json(prompt, tier="light")
        rows = data.get("items") or data.get("translations") or []
        out: dict[str, str] = {}
        for row in rows:
            if isinstance(row, dict) and row.get("src") and row.get("zh"):
                out[str(row["src"])] = str(row["zh"]).strip()
        return self._remember(out)

    async def _via_mymemory(self, texts: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        concurrency = max(1, int(self.config.get("translate.concurrency", 3)))
        queue: asyncio.Queue[str] = asyncio.Queue()
        for text in texts:
            queue.put_nowait(text)

        async def worker() -> None:
            while not queue.empty():
                if not self.budget.available():
                    log.info("translation budget exhausted; %d item(s) stay in English",
                             queue.qsize())
                    return
                text = queue.get_nowait()
                guarded, mapping = protect_terms(text, self.keep_terms)
                segment = guarded[:MYMEMORY_MAX_CHARS]
                self.budget.spend()
                try:
                    client = await self._http()
                    response = await client.get(MYMEMORY, params={"q": segment, "langpair": f"en|{TARGET}"})
                    if getattr(response, "status_code", 200) == 429:
                        self._mark_route_down("mymemory")
                        break
                    payload = response.json()
                    translated = str((payload.get("responseData") or {}).get("translatedText") or "").strip()
                    status = payload.get("responseStatus")
                    # Accept only a real translation: same-status, different text,
                    # and it actually contains Chinese. Titles that are pure
                    # proper nouns come back unchanged - leave those alone.
                    if "MYMEMORY WARNING" in translated.upper():
                        # The provider's own per-IP quota, not our budget: there is
                        # no point asking again for the rest of the day.
                        self._mark_route_down("mymemory")
                        break
                    if (status == 200 and translated and has_cjk(translated)
                            and translated.strip().lower() != segment.strip().lower()
                            and "QUERY LENGTH LIMIT" not in translated.upper()
                            and kept_intact(translated, mapping)):
                        out[text] = self._polish(restore_terms(translated, mapping))
                    elif mapping and not kept_intact(translated, mapping):
                        log.debug("mymemory swallowed a brand placeholder for %r; "
                                  "trying the next route", text[:40])
                    elif status != 200:
                        log.debug("mymemory status %s for %r", status, text[:40])
                except Exception as exc:  # noqa: BLE001 - keep the original text
                    log.debug("mymemory failed for %r: %s", text[:40], exc)
                await asyncio.sleep(0.15)

        await asyncio.gather(*(worker() for _ in range(concurrency)))
        return self._remember(out)

    async def _via_google(self, texts: list[str]) -> dict[str, str]:
        """Google's unauthenticated web endpoint, one batched request.

        Tried after MyMemory by `provider: auto`, and it is the better of the two
        at brand names: it passes the ⑴⑵ placeholders through untouched, while
        MyMemory transliterates them into junk like "锘洪噾" (measured live).
        """
        client = await self._http()
        # One request for the whole batch, so names are guarded per line and the
        # alignment check below still counts the same number of answers.
        lines = [protect_terms(text, self.keep_terms) for text in texts]
        response = await client.get(
            GOOGLE_WEB,
            params={"client": "gtx", "sl": "en", "tl": TARGET, "dt": "t",
                    "q": "\n".join(guarded for guarded, _ in lines)},
        )
        status = getattr(response, "status_code", 200)
        if status >= 400:
            raise RuntimeError(f"google returned HTTP {status}")
        if not response.headers.get("content-type", "").startswith("application/json"):
            raise RuntimeError("google returned a non-JSON body (rate limited)")
        joined = flatten_google(response.json())
        parts = [part.strip() for part in joined.split("\n") if part.strip()]
        if not parts or len(parts) != len(texts):
            # Mis-aligned parts would paste one headline onto another article,
            # which is worse than leaving the text in English.
            log.info("google answer did not line up (%d parts for %d text(s))",
                     len(parts), len(texts))
            return {}
        return self._remember({
            text: restore_terms(part, mapping)
            for text, (_guarded, mapping), part in zip(texts, lines, parts)
            if kept_intact(part, mapping)
        })

    # --------------------------------------------------------------- helpers
    def _remember(self, pairs: dict[str, str]) -> dict[str, str]:
        """Keep only real translations, and undo brand transliterations."""
        clean = {}
        for src, zh in pairs.items():
            zh = restore_proper_nouns(zh or "")
            if zh.strip() and zh.strip() != src.strip() and has_cjk(zh):
                clean[src] = zh
        self.cache.update(clean)
        return clean

    @staticmethod
    def _polish(text: str) -> str:
        """MyMemory leaves stray markup and over-long machine output."""
        out = re.sub(r"<[^>]+>", "", text)
        out = out.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
        out = re.sub(r"\s{2,}", " ", out).strip()
        return out[:200]


_translator: Translator | None = None


def get_translator(config: AppConfig | None = None) -> Translator:
    global _translator
    if _translator is None:
        _translator = Translator(config)
    return _translator


def reset_translator() -> None:
    global _translator
    _translator = None
