"""Normalisation: every collector output becomes one standard Article.

design doc sections 8, 9, 10.1-10.2 (URL tracking params, hashing, language,
content cleaning, timestamp hygiene).
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup

from app.logging_setup import get_logger

log = get_logger("collector")

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "gclid", "gclsrc", "dclid", "fbclid", "msclkid", "twclid", "igshid", "mc_cid",
    "mc_eid", "ref", "referer", "referrer", "cmpid", "smid", "share", "src", "source",
    "s_cid", "ncid", "trk", "trkCampaign", "trkFlavour", "trkId", "trkOrg", "trkPosition",
    "vero_id", "oly_enc_id", "recruiter", "recruitment", "spm", "scene", "from",
}
# Common social-share prefixes that encode the original URL in a query param.
WRAP_PARAMS = ("u", "url", "link", "to", "target", "dest", "q")

HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")
PUNCT_RE = re.compile(r"[^\w\s\-]", re.UNICODE)
# A word is latin/cyrillic letters, digits, or a version number like 4.5 / gpt-5.
WORD_RE = re.compile(r"[a-z0-9]+(?:\.[0-9]+)*", re.IGNORECASE)
VERSION_RE = re.compile(r"\b\d+(?:\.\d+)*\b")
STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "is", "are",
    "be", "as", "at", "by", "this", "that", "it", "its", "from", "new", "will", "has",
    "have", "was", "not", "but", "can", "how", "why", "says", "said", "after", "over",
}


def words(text: str | None) -> list[str]:
    """Version-aware tokenizer: 'GPT-4.5' keeps its number, '4.5' stays one word."""
    out: list[str] = []
    for match in WORD_RE.findall(clean_text(text)):
        word = match.lower().strip("-")
        if not word or word in STOPWORDS or len(word) < 2 and not any(ch.isdigit() for ch in word):
            continue
        out.append(word)
    return out


# Feeds double-escape their entities ("&amp;#128064;"), so a headline can reach
# the chat window as literal "&#128064;" junk. Unescape twice, then drop whatever
# the decoder still could not resolve (truncated codes at a cut boundary).
DANGLING_ENTITY_RE = re.compile(r"&(?:#\d{1,7}#?;|#x[0-9a-fA-F]{1,6};|[a-zA-Z]{2,8};)(?!=)")
TRAILING_AMP_RE = re.compile(r"(?:&[ \t]*)+$")


def unescape_entities(value: str) -> str:
    text = html.unescape(html.unescape(value or ""))
    text = DANGLING_ENTITY_RE.sub("", text)
    # A truncated "&amp" decodes to a bare "&" with nothing after it. A real
    # ampersand in prose is followed by a word ("Tom & Jerry"), so a trailing one
    # is always feed damage.
    return TRAILING_AMP_RE.sub("", text)


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    return WHITESPACE_RE.sub(" ", text).strip()


def strip_html(value: str | None, *, limit: int = 12000) -> str:
    """BeautifulSoup body extraction; feeds ship escaped HTML in descriptions."""
    if not value:
        return ""
    if "<" not in value:
        # BeautifulSoup unescapes for us on the HTML path; plain text needs the
        # same treatment or "&amp;#128064;" survives into the chat window.
        return strip_feed_boilerplate(
            clean_text(_demote_markdown(unescape_entities(value))))[:limit]
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "form", "iframe"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = unescape_entities(text)
    return strip_feed_boilerplate(clean_text(_demote_markdown(text)))[:limit]


# Discourse category feeds describe a topic as "20 posts - 17 participants"
# followed by a bare "Read full topic" link. Neither is content, and a rule-mode
# summary built from them reads like a database error rather than news.
_POSTS_RE = re.compile(r"\d[\d,.]*\s*posts?\s*[-–]\s*\d[\d,.]*\s*participants?", re.I)
_READ_RE = re.compile(r"read full topic", re.I)
# Reddit's RSS ships nothing but this for a link post, three times over:
# "submitted by /u/pmv143 [link] [comments] ..." - it was reaching /新闻 as the
# headline because a machine translation of it matched no pattern here.
_SUBMITTED_RE = re.compile(r"submitted by\s*/?[uU]?/?[\w.-]+(?:\s*$)?", re.I)
_SUBMITTED_ZH_RE = re.compile(r"(?:由|来自)\s*/?u/[\w.-]+(?:\s*提交)?")
_LINK_TAGS_RE = re.compile(r"\[(?:link|comments|permalink|source|details)\]", re.I)
_LINK_TAGS_ZH_RE = re.compile(r"\[(?:链接|评论|原文|来源|永久链接|永久連接)\]")
_CROSSPOST_RE = re.compile(r"crosspost(?:ed)?\s+from\s*/?[rR]/?[\w.-]+", re.I)
_EDITED_RE = re.compile(r"(?:topically|lastically)?\s+edited by\s*/?[uU]?/?[\w.-]+", re.I)


def _collapse_repeats(text: str) -> str:
    """`X X X` from a feed that pasted the same block three times is still `X`.

    Tried both ways: whole tokens, then raw characters, because a Chinese
    sentence repeated without spaces has no token boundary to lean on.
    """
    parts = text.split(" ")
    count = len(parts)
    if count >= 2:
        for size in range(1, count // 2 + 1):
            if count % size:
                continue
            block = parts[:size]
            if block * (count // size) == parts:
                return " ".join(block)
    length = len(text)
    if length >= 24:
        for size in range(1, length // 2 + 1):
            if length % size:
                continue
            chunk = text[:size]
            if chunk * (length // size) == text:
                return chunk
    return text


def strip_feed_boilerplate(value: str) -> str:
    """Drop feed chrome without touching punctuation.

    Deliberately not reusing clean_text(): its NFKC pass rewrites the full-width
    comma in Chinese summaries into an ASCII one.
    """
    text = _READ_RE.sub("", value or "")
    text = _POSTS_RE.sub("", text)
    text = _CROSSPOST_RE.sub("", text)
    text = _EDITED_RE.sub("", text)
    text = _LINK_TAGS_RE.sub("", text)
    text = _LINK_TAGS_ZH_RE.sub("", text)
    text = _SUBMITTED_RE.sub("", text)
    text = _SUBMITTED_ZH_RE.sub("", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    # "crossposted from /r/x - the real title" loses its dangling separator too.
    # ASCII separators only: full-width Chinese punctuation is left exactly as
    # written (see test_boilerplate_stripping_keeps_chinese_punctuation).
    text = text.strip(" -–—•·,;:").strip()
    return _collapse_repeats(text)


MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
MD_EMPHASIS_RE = re.compile(r"(\*\*|__|\*|_)(?=\S)(.+?)(?<=\S)\1")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")


# Where a line may be shortened to. Chinese has no spaces, so punctuation
# carries the whole job; the space is what saves an untranslated English line
# with no punctuation in its first 150 characters.
_SHORTEN_MARKS = ".,!?;:，。！？；：、)）]」』 "


def shorten(value: str, limit: int) -> str:
    """Trim one line to `limit` at a clause boundary, and mark the cut with "…".

    Hard slices produced `first[:160]` summaries that stop inside a quoted word
    ("…选择“CLAUD"), and the same defect showed up in the rendered briefing. The
    cut never lands before 60% of the limit, and runs collapse to spaces so one
    summary stays one line.
    """
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    head = text[:limit]
    floor = int(limit * 0.6)
    cut = max((head.rfind(mark) for mark in _SHORTEN_MARKS
               if head.rfind(mark) >= floor), default=-1)
    if cut < 0:
        return head.rstrip() + "…"
    trimmed = head[:cut + 1].strip()
    # `;` is a legitimate clause boundary and also the end of an HTML entity, so
    # a cut can orphan `&nbs` + `;`. Drop a trailing entity that has no room to
    # close instead of shipping the ampersand.
    ampersand = trimmed.rfind("&")
    if ampersand >= 0 and ";" not in trimmed[ampersand:ampersand + 12]:
        trimmed = trimmed[:ampersand].rstrip()
    return trimmed + "…"


def _demote_markdown(text: str) -> str:
    """Release notes and blog bodies arrive as Markdown.

    Kept raw it leaks into one-line summaries ("## New Features - Added ..."),
    which reads as broken output on Telegram; headings, emphasis and link
    wrappers carry nothing a plain-text digest needs.
    """
    text = MD_HEADING_RE.sub("", text)
    text = MD_LINK_RE.sub(r"\1", text)
    text = MD_EMPHASIS_RE.sub(r"\2", text)
    return re.sub(r"^\s*[-*]\s+", "", text, flags=re.MULTILINE)


def normalize_url(url: str | None, *, strip_params: bool = True) -> str:
    """Drop fragments + tracking params, lowercase the host, add no trailing slash."""
    if not url:
        return ""
    url = clean_text(url).replace(" ", "%20").replace("\t", "")
    if url.startswith("http://") is False and url.startswith("https://") is False:
        if url.startswith("//"):
            url = "https:" + url
        elif "://" not in url:
            url = "https://" + url
    parts = urlsplit(url)
    scheme = "https" if parts.scheme in ("http", "https", "") else parts.scheme
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    query = ""
    if strip_params and parts.query:
        kept = []
        for pair in parts.query.split("&"):
            if not pair:
                continue
            key, _, value = pair.partition("=")
            if key.lower() in TRACKING_PARAMS or key.lower().startswith("utm_"):
                continue
            kept.append(f"{key}={value}" if value else key)
        query = "&".join(sorted(kept))
    return urlunsplit((scheme, netloc, path or "/", query, ""))


def unwrap_redirect(url: str | None) -> str:
    """l.facebook.com / news.google.com style wrappers hide the real article URL."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = parts.netloc.lower()
    wrappers = ("l.facebook.com", "l.instagram.com", "news.google.com", "t.co", "link.zhihu.com",
                "www.reddit.com", "redd.it", "outlook.live.com")
    if not any(host == w or host.endswith("." + w) for w in wrappers):
        return url
    if host.startswith(("www.reddit.com", "reddit.com")):
        match = re.search(r"/url\?(?:.*&)?url=([^&]+)", parts.query or "")
        return url
    for key in WRAP_PARAMS:
        for pair in (parts.query or "").split("&"):
            if pair.startswith(f"{key}="):
                from urllib.parse import unquote

                candidate = unquote(pair.split("=", 1)[1])
                if candidate.startswith("http"):
                    return candidate
    return url


def url_hash(url: str | None) -> str:
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()


def article_hash(title: str | None, url: str | None) -> str:
    """Content identity: same title + same URL is the same record."""
    payload = f"{title_key(title)}|{normalize_url(url)}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def title_key(title: str | None) -> str:
    """Case/punctuation-insensitive stem used by the similarity dedup pass."""
    return " ".join(sorted(words(title)))


def tokens_of(title: str | None) -> set[str]:
    return set(words(title))


def version_numbers(title: str | None) -> set[str]:
    """Model versions are the identity of a release: 4.5 is not 4.6."""
    return {v for v in VERSION_RE.findall(clean_text(title).lower()) if any(ch.isdigit() for ch in v)}


def detect_language(text: str | None) -> str:
    if not text:
        return "en"
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    if cjk >= max(3, len(text) * 0.05):
        return "zh"
    kana = sum(1 for ch in text if "\u3040" <= ch <= "\u30ff")
    if kana > len(text) * 0.05:
        return "ja"
    hangul = sum(1 for ch in text if "\uac00" <= ch <= "\ud7af")
    if hangul > len(text) * 0.05:
        return "ko"
    return "en"


def parse_date(value: Any) -> datetime:
    """Any collector timestamp -> naive UTC. Missing dates become 'just seen'."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(value, tz=timezone.utc)
    elif value:
        text = clean_text(str(value)).replace("Z", "+00:00")
        parsed = None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            from email.utils import parsedate_to_datetime

            try:
                parsed = parsedate_to_datetime(text)
            except (TypeError, ValueError, IndexError):
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
                    try:
                        parsed = datetime.strptime(text[: len(fmt) + 4], fmt)
                        break
                    except ValueError:
                        continue
        if parsed is None:
            parsed = datetime.now(timezone.utc)
    else:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    # A future timestamp means a bad feed clock; clamp it so digests stay sane.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if parsed > now + timedelta(minutes=30):
        log.debug("feed clock in the future for %r, clamped to now", value)
        parsed = now
    return parsed


def to_utc_naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def keyword_hits(text: str, keywords: list[str]) -> list[str]:
    """Layer 1 of cost control (section 23): free rule filtering."""
    lowered = f" {clean_text(text).lower()} "
    hits = []
    for keyword in keywords:
        keyword = keyword.lower().strip()
        if not keyword:
            continue
        if keyword.endswith("-") or keyword.endswith("tun"):  # prefix rules like fine-tun
            if keyword.rstrip("-") in lowered:
                hits.append(keyword)
        elif f" {keyword} " in lowered or f" {keyword}" in lowered or f"{keyword} " in lowered:
            hits.append(keyword)
    return hits


def is_blocked_title(title: str | None, blocklist: list[str]) -> str | None:
    lowered = clean_text(title).lower()
    for pattern in blocklist or []:
        if pattern.lower() in lowered:
            return pattern
    return None


def is_blocked_url(url: str | None, domains: list[str]) -> bool:
    host = (urlsplit(normalize_url(url)).netloc or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains or [])


def build_article(
    *,
    title: str | None,
    url: str | None,
    source_name: str,
    source_type: str = "rss",
    author: str | None = None,
    content: str | None = None,
    published_at: Any = None,
    language: str | None = None,
    quality: str = "C",
    meta: dict[str, Any] | None = None,
    community_heat: float = 0.0,
    source_id: int | None = None,
) -> dict[str, Any]:
    """The single funnel every collector writes through (design doc section 9)."""
    real_url = normalize_url(unwrap_redirect(url))
    clean_title = clean_text(unescape_entities(title))[:500] or "(untitled)"
    body = strip_html(content)
    published = parse_date(published_at)
    return {
        "source_id": source_id,
        "source_name": clean_text(source_name),
        "source_type": source_type,
        "title": clean_title,
        "url": real_url,
        "normalized_url": real_url,
        "url_hash": url_hash(real_url),
        "hash": article_hash(clean_title, real_url),
        "title_norm": title_key(clean_title),
        "author": clean_text(author)[:250] or None,
        "content": body or None,
        "language": language or detect_language(f"{clean_title} {body[:500]}"),
        "published_at": published,
        "quality": quality,
        "community_heat": float(community_heat or 0),
        "meta": dict(meta or {}),
    }
