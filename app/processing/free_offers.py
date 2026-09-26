"""Detect "X is free right now" news - the brain behind the /免费 command.

A usable hit needs both halves of the claim:

    free signal (免费 / free tier / limited-time free / $0 ...)
  + subject     (an agent, a model, or a platform from config/free_offers.yaml)

False friends are stripped first, because "camera-free glasses", "encoder-free
ASR" and "feel free" would otherwise flood a freebies feed. Everything is
vocabulary-driven from config, so new tools need no code change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.config import AppConfig, get_config
from app.logging_setup import get_logger

log = get_logger("app")

CJK_RE = re.compile(r"[一-鿿]")


@dataclass
class FreeOffer:
    tool: str
    kind: str
    signals: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    expiry: str | None = None
    confidence: float = 0.0
    # False when the subject only appears somewhere in the body: the offer is
    # real but the headline was not about it, which is worth telling the reader.
    subject_in_title: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "kind": self.kind,
            "signals": self.signals[:4],
            "models": self.models[:4],
            "expiry": self.expiry,
            "confidence": round(self.confidence, 2),
            "subject_in_title": self.subject_in_title,
        }


def _text_of(*parts: str | None) -> str:
    return " ".join(p for p in parts if p)


def _strip_false_friends(text: str, false_friends: list[str]) -> str:
    cleaned = text
    for phrase in sorted(false_friends, key=len, reverse=True):
        if not phrase:
            continue
        cleaned = re.sub(re.escape(phrase), " ", cleaned, flags=re.I)
    return cleaned


def _find_all(text: str, needles: list[str]) -> list[tuple[int, int, str]]:
    """Positions of every needle, for the "nearest signal" tie-breaker."""
    found: list[tuple[int, int, str]] = []
    for needle in needles:
        needle = str(needle).lower().strip()
        if len(needle) < 2:
            continue
        if needle.isascii():
            # A Chinese title glues the product to its version with no space
            # ("限免100刀DeepSeekv4.1Flash"), so a plain word boundary is too
            # strict: allow a version suffix, but still reject "deepseekai".
            pattern = (rf"(?<![a-z0-9]){re.escape(needle)}"
                       rf"(?:(?![a-z0-9])|(?=v\d)|(?=[-_.]\d))")
            for match in re.finditer(pattern, text):
                found.append((match.start(), match.end(), needle))
        else:
            start = text.find(needle)
            while start != -1:
                found.append((start, start + len(needle), needle))
                start = text.find(needle, start + 1)
    return found


def _gap(span: tuple[int, int, str], signals: list[tuple[int, int, str]]) -> int:
    """How far this candidate is from the nearest free signal."""
    if not signals:
        return 0
    start, end, _ = span
    return min(0 if s_start <= end and start <= s_end else
               min(abs(start - s_end), abs(s_start - end)) for s_start, s_end, _ in signals)


def _match_tool(text: str, tools: dict[str, list[str]],
                signals: list[tuple[int, int, str]], config: AppConfig | None = None,
                title_len: int = 0) -> str | None:
    """Pick the subject of the offer.

    Two rules, in order:
      1. a subject named in the *headline* wins over one that only appears in
         the body - a Linux.do post about Qoder that name-drops gemini/grok in
         the discussion is a Qoder story, not a Google one;
      2. an agent/IDE outranks a platform, which outranks a model -
         "opencode 可以免费使用 DeepSeek" is an opencode story, and
         "OpenRouter: DeepSeek R1 is free" is an OpenRouter story, with
         DeepSeek recorded as the free model;
      3. then proximity to the free signal, then earliest position.
    """
    config = config or get_config()
    kind_map = config.free_terms.get("kind_map") or {}
    rank_of = {"编程 Agent/IDE": 0, "API/平台": 1}
    candidates: list[tuple[int, int, int, int, int, str]] = []
    for name, aliases in tools.items():
        rank = rank_of.get(next((k for k, names in kind_map.items() if name in (names or [])), ""), 2)
        for alias in [str(name), *[str(a) for a in (aliases or [])]]:
            needle = alias.lower().strip()
            # 智谱 / 腾讯 are complete names at two characters; the >=3 floor is
            # only needed to stop ASCII fragments like "ai" matching everything.
            if len(needle) < (3 if needle.isascii() else 2):
                continue
            for span in _find_all(text, [needle]):
                # A brand merely mentioned in the body must not steal the
                # subject from the one the headline is actually about.
                in_title = 0 if span[1] <= title_len else 1
                candidates.append((in_title, rank, _gap(span, signals),
                                   span[0], -len(needle), name))
    if not candidates:
        return None
    return min(candidates)[5]


SITE_SUBJECT_RE = re.compile(r"([0-9A-Za-z_\-\u4e00-\u9fff]{2,12}站)")


def _match_site_subject(text: str, title_len: int,
                        accepted: list[tuple[int, int, str]]) -> str | None:
    """A 中转站/公益站 name mentioned in the headline next to a giveaway signal.

    Chinese community promo posts name the site as `XX站` and never appear in any
    vocabulary we could ship. Both parts must be in the title, so
    "站内合适的公益站" and "【公益生图站】…" stay quiet: no free signal.
    """
    if not any(span[1] <= title_len for span in accepted):
        return None
    head = text[:title_len]
    for match in SITE_SUBJECT_RE.finditer(head):
        # "本次咕咕嘎嘎站" - the demonstrative is not part of the name.
        name = re.sub(r"^[本该这那](?:次|家|个)?", "", match.group(1))
        if len(name) >= 3 and name != "站":
            return name
    return None


def _match_models(text: str, models: list[str]) -> list[str]:
    return [needle for _s, _e, needle in _find_all(text, [str(m) for m in models or []])
            if len(needle) >= 3][:6]


def _match_model_subject(text: str, terms: dict[str, Any], title_len: int,
                         accepted: list[tuple[int, int, str]]) -> str | None:
    """The free thing named in the headline, when it is a model and not a tool.

    The live gateway list is merged into `models` at runtime, so a model that
    went free this week is recognisable in news text without a vocabulary edit.
    """
    models = [str(m) for m in terms.get("models") or []]
    spans = [span for span in _find_all(text, models)
             if span[1] <= title_len and len(span[2]) >= 4]
    if not spans:
        return None
    if not any(span[1] <= title_len for span in accepted):
        return None          # a headline signal is mandatory for this looser rule
    spans.sort(key=lambda s: (-len(s[2]), s[0]))
    return spans[0][2]


def _match_signals(text: str, signals_en: list[str], signals_zh: list[str]) -> list[tuple[int, int, str]]:
    return _find_all(text, [str(s) for s in signals_en or []] + [str(s) for s in signals_zh or []])


def _expiry(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns or []:
        try:
            match = re.search(pattern, text, flags=re.I)
        except re.error:  # a bad regex in config must not break the pipeline
            log.warning("invalid expiry_patterns entry skipped: %r", pattern)
            continue
        if match:
            return match.group(0).strip()[:80]
    return None


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _scan(text: str, title_len: int, terms: dict[str, Any],
          config: AppConfig) -> FreeOffer | None:
    """One pass over a combined title+body text.

    A free signal counts when it is in the title, or when it is a *strong*
    signal sitting next to the subject in the body. Without that split, every
    Reddit thread that casually says "free software" or "no cost" becomes a
    白嫖 tip - which is exactly what the first live run produced.
    """
    signals_en = [str(s) for s in terms.get("signals_en", [])]
    signals_zh = [str(s) for s in terms.get("signals_zh", [])]
    strong = {str(s).lower() for s in terms.get("signals_strong", [])}
    spans = _match_signals(text, signals_en, signals_zh)
    accepted = [span for span in spans
                if span[1] <= title_len or span[2] in strong]
    if not accepted:
        return None

    tool_names = {k: v for k, v in (terms.get("tools") or {}).items()}
    tool = _match_tool(text, tool_names, accepted, config=config,
                       title_len=title_len)
    model_subject = None
    if tool is None:
        # "官方提供了免费的 stealth/space-bunny-alpha" names no known agent, but
        # the free thing *is* the subject. Only allow it when both the signal and
        # the model sit in the headline, which is what keeps precision at 1.0.
        model_subject = _match_model_subject(text, terms, title_len, accepted)
        site_subject = None
        if model_subject is None:
            site_subject = _match_site_subject(text, title_len, accepted)
            model_subject = site_subject
        if model_subject is None:
            return None
        tool = model_subject
        if site_subject:
            # a 中转站 is a platform, not a model
            model_subject = None
            tool = site_subject

    aliases = [str(tool), *[str(a) for a in (tool_names.get(tool) or [])]]
    subject_in_title = any(span[1] <= title_len
                           for alias in aliases if len(alias) >= 2
                           for span in _find_all(text, [alias.lower()]))

    # body-only matches must actually sit near the subject
    if all(span[0] > title_len for span in accepted):
        max_distance = int(terms.get("body_max_distance", 140))
        subject_spans = [s for name, aliases in tool_names.items() if name == tool
                         for alias in [name, *(aliases or [])]
                         for s in _find_all(text, [str(alias)])]
        if not any(_gap(subject, accepted) <= max_distance for subject in subject_spans):
            return None

    signals = _dedupe([needle for _s, _e, needle in accepted])
    models = _dedupe(_match_models(text, [str(m) for m in terms.get("models", [])]))[:3]
    kind_map = terms.get("kind_map") or {}
    kind = next((k for k, names in kind_map.items() if tool in (names or [])),
                terms.get("fallback_kind", "其他"))
    if model_subject is not None:
        kind = terms.get("model_kind", "模型")
    confidence = min(1.0, 0.45 + 0.12 * len(signals) + (0.2 if models else 0.0)
                     + (0.1 if any(span[1] <= title_len for span in accepted) else 0.0)
                     - (0.1 if model_subject is not None else 0.0))
    offer = FreeOffer(tool=tool, kind=kind, signals=signals, models=models,
                      expiry=_expiry(text, [str(p) for p in terms.get("expiry_patterns", [])]),
                      confidence=confidence,
                      subject_in_title=subject_in_title)
    log.debug("free offer detected: %s", offer.to_dict())
    return offer


def detect(*parts: str | None, config: AppConfig | None = None) -> FreeOffer | None:
    """Return the free-offer claim in this text, or None if there is none.

    The first argument is treated as the title; the rest is body text.
    """
    config = config or get_config()
    terms = config.free_terms
    if not terms:
        return None

    pieces = [str(p or "") for p in parts]
    title = pieces[0] if pieces else ""
    body = " ".join(pieces[1:])
    title_clean = _strip_false_friends(title.lower(), [str(f) for f in terms.get("false_friends", [])])
    body_clean = _strip_false_friends(body.lower(), [str(f) for f in terms.get("false_friends", [])])[:2500]
    text = f"{title_clean} \n {body_clean}"
    return _scan(text, len(title_clean), terms, config)


def detect_article(article: dict[str, Any] | Any, config: AppConfig | None = None) -> FreeOffer | None:
    """Accepts an Article ORM row or a normalised dict."""
    get = article.get if isinstance(article, dict) else lambda key, default=None: getattr(article, key, default)
    return detect(
        get("title"), get("summary"), get("summary_zh"), get("content"),
        config=config,
    )


def kind_emoji(kind: str, config: AppConfig | None = None) -> str:
    config = config or get_config()
    return (config.free_terms.get("kinds") or {}).get(kind, "🎁")
