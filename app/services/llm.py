"""OpenAI-compatible LLM client.

Nothing about the provider is hard-coded: base url, key and model names all
come from .env, so swapping OpenAI -> DeepSeek -> Ollama is a config edit
(design doc section 7). Callers never build prompts themselves; they reference
a key in config/prompts.yaml (section 39).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import AppConfig, get_config
from app.logging_setup import get_logger

log = get_logger("llm")


class LLMError(RuntimeError):
    """Raised when the model could not be reached or answered useably."""


class LLMUnavailable(LLMError):
    """Transient provider failure: worth another attempt."""


class LLMNotConfigured(LLMError):
    pass


@dataclass
class Usage:
    calls: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def __str__(self) -> str:  # pragma: no cover - diagnostics only
        return f"calls={self.calls} failed={self.failures} tokens={self.prompt_tokens}+{self.completion_tokens}"


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a reply that may be fenced or chatty."""
    if not text:
        raise ValueError("empty response")
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    start = cleaned.find("{")
    if start == -1:
        raise ValueError("no JSON object in model reply")
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(cleaned)):
        char = cleaned[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return json.loads(cleaned[start : index + 1])
    raise ValueError("unbalanced JSON in model reply")


def _as_list(value: Any, limit: int = 8) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = re.split(r"\n+|;|•", value)
    out = []
    for item in value:
        text = str(item).strip(" -•\t\r\n")
        if text and text.lower() not in {"n/a", "none", "null"}:
            out.append(text[:400])
        if len(out) >= limit:
            break
    return out


def _clamp(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number > 100:  # models occasionally answer 0-1000 or 0-10 by mistake
        number = max(number / 10.0, 100.0) if number <= 1000 else number
    return max(0.0, min(100.0, number))


class LLMService:
    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()
        self.settings = self.config.settings
        self.usage = Usage()
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("llm.enabled", True)) and self.settings.llm_configured

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.llm_timeout, connect=15.0),
                limits=httpx.Limits(max_keepalive_connections=5),
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ------------------------------------------------------------- transport
    async def chat(self, messages: list[dict[str, str]], *, tier: str = "light",
                   temperature: float | None = None, max_tokens: int | None = None) -> str:
        if not self.settings.llm_configured:
            raise LLMNotConfigured("LLM_BASE_URL / LLM_API_KEY / LLM_MODEL are not set")
        model = self.settings.llm_model_for(tier)
        tier_cfg = self.config.get(f"llm.tiers.{tier}", {}) or {}
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature if temperature is not None else tier_cfg.get("temperature", 0.2),
            "max_tokens": max_tokens or tier_cfg.get("max_tokens", 900),
            "stream": False,
        }
        attempts = max(1, int(self.settings.llm_max_retries) + 1)
        last_error: Exception | None = None
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(multiplier=1.5, min=1, max=15),
            retry=retry_if_exception_type(LLMUnavailable),
            reraise=False,
        ):
            with attempt:
                try:
                    return await self._request(payload, model)
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    body = exc.response.text[:200]
                    if status in (408, 409, 425, 429) or status >= 500:
                        raise LLMUnavailable(f"HTTP {status}") from exc
                    # A 401/403/404 will not fix itself by asking again.
                    raise LLMError(f"HTTP {status}: {body}") from exc
                except (asyncio.TimeoutError, httpx.HTTPError) as exc:
                    last_error = exc
                    raise LLMUnavailable(str(exc)) from exc
        self.usage.failures += 1
        raise LLMError(f"LLM request failed after {attempts} attempts: {last_error}")

    async def _request(self, payload: dict[str, Any], model: str) -> str:
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}"}
        client = await self._http()
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
        self.usage.calls += 1
        usage = data.get("usage") or {}
        self.usage.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.usage.completion_tokens += int(usage.get("completion_tokens") or 0)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected response shape: {json.dumps(data)[:200]}") from exc
        if not isinstance(content, str) or not content.strip():
            raise LLMError("empty completion")
        log.debug("llm ok model=%s chars=%d", model, len(content))
        return content

    async def ask_json(self, prompt: str, *, tier: str = "light",
                       system: str = "You output only a single valid JSON object.") -> dict[str, Any]:
        raw = await self.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}], tier=tier
        )
        try:
            return extract_json(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            log.warning("unparsable JSON from model (tier=%s): %s", tier, raw[:200])
            raise LLMError(f"model did not return JSON: {exc}") from exc

    async def ask_text(self, prompt: str, *, tier: str = "strong",
                       system: str | None = None) -> str:
        messages = [{"role": "user", "content": prompt}]
        if system:
            messages.insert(0, {"role": "system", "content": system})
        return await self.chat(messages, tier=tier)

    # ------------------------------------------------------------- task API
    async def classify(self, article: dict[str, Any], interests: list[dict[str, Any]]) -> dict[str, Any]:
        prompt = self.config.render(
            "classification_prompt",
            categories=" / ".join(self.config.category_names),
            subcategories=json.dumps(
                {c: self.config.subcategories(c) for c in self.config.category_names}, ensure_ascii=False
            ),
            interests=json.dumps(interests, ensure_ascii=False) if interests else "（无特殊兴趣配置）",
            title=article.get("title", ""),
            source_name=article.get("source_name", ""),
            quality=article.get("quality", "C"),
            published_at=article.get("published_at", ""),
            url=article.get("url", ""),
            content=_clip(article.get("content") or article.get("title") or "",
                          int(self.config.get("llm.max_content_chars", 6000)) // 2),
        )
        data = await self.ask_json(prompt, tier="light")
        category = str(data.get("category") or "").strip()
        if category not in self.config.category_names:
            category = self.config.fallback_category
        return {
            "is_ai_related": bool(data.get("is_ai_related", True)),
            "category": category,
            "subcategory": str(data.get("subcategory") or "").strip() or None,
            "relevance_score": _clamp(data.get("relevance_score"), 50),
            "importance_score": _clamp(data.get("importance_score")),
            "novelty_score": _clamp(data.get("novelty_score"), 80),
            "tags": _as_list(data.get("tags"), 8),
            "reason": str(data.get("reason") or "")[:300],
        }

    async def summarize(self, article: dict[str, Any]) -> dict[str, Any]:
        prompt = self.config.render(
            "summary_prompt",
            title=article.get("title", ""),
            source_name=article.get("source_name", ""),
            published_at=article.get("published_at", ""),
            content=_clip(article.get("content") or "", int(self.config.get("llm.max_content_chars", 6000))),
        )
        data = await self.ask_json(prompt, tier="light")
        return {
            "summary": str(data.get("summary") or "").strip()[:600],
            "key_points": _as_list(data.get("key_points"), 5),
            "why_it_matters": str(data.get("why_it_matters") or "").strip()[:800],
            "tags": _as_list(data.get("tags"), 6),
            "importance_score": _clamp(data.get("importance"), 0),
        }

    async def deep_analyze(self, article: dict[str, Any], related: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        related_block = ""
        if related:
            related_block = "同一事件的其他来源：\n" + "\n".join(
                f"- {r.get('source_name')} | {r.get('title')}" for r in related
            )
        prompt = self.config.render(
            "deep_analysis_prompt",
            title=article.get("title", ""),
            source_name=article.get("source_name", ""),
            url=article.get("url", ""),
            published_at=article.get("published_at", ""),
            final_score=article.get("final_score", 0),
            content=_clip(article.get("content") or article.get("summary") or "",
                          int(self.config.get("llm.max_content_chars", 6000))),
            related=related_block,
        )
        data = await self.ask_json(prompt, tier="strong")
        return {
            "headline": str(data.get("headline") or "").strip(),
            "what_happened": str(data.get("what_happened") or "").strip(),
            "key_points": _as_list(data.get("key_points"), 6),
            "why_it_matters": str(data.get("why_it_matters") or "").strip(),
            "industry_impact": str(data.get("industry_impact") or "").strip(),
            "open_questions": _as_list(data.get("open_questions"), 4),
            "confidence_note": str(data.get("confidence_note") or "").strip(),
        }

    async def digest_overview(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        listing = "\n".join(
            f"{i + 1}. [{it.get('final_score', 0):.0f}] {it.get('category')} | {it.get('title')}"
            for i, it in enumerate(items)
        )
        try:
            data = await self.ask_json(self.config.render("digest_prompt", items=listing), tier="strong")
        except LLMError:
            return {"overview": "", "highlights": []}
        return {"overview": str(data.get("overview") or "").strip(), "highlights": _as_list(data.get("highlights"), 3)}

    async def answer_question(self, question: str, results: list[dict[str, Any]],
                              context: list[dict[str, str]] | None = None, *, now: str = "") -> str:
        compact = [
            {
                "id": r.get("id"),
                "title": r.get("title"),
                "source": r.get("source_name"),
                "published": r.get("published_at"),
                "category": r.get("category"),
                "score": r.get("final_score"),
                "summary": r.get("summary"),
            }
            for r in results
        ]
        prompt = self.config.render(
            "chat_prompt",
            now=now,
            question=question,
            context=json.dumps(context or [], ensure_ascii=False),
            results=json.dumps(compact, ensure_ascii=False)[:12000],
        )
        return await self.ask_text(prompt, tier="strong")

    async def detect_intent(self, text: str, recent: list[dict[str, Any]]) -> dict[str, Any]:
        prompt = self.config.render(
            "intent_prompt",
            text=text,
            recent=json.dumps([{"index": i + 1, "title": r.get("title")} for i, r in enumerate(recent)],
                              ensure_ascii=False),
        )
        data = await self.ask_json(prompt, tier="light")
        intent = str(data.get("intent") or "other").lower()
        if intent not in {"latest", "search", "summarize", "settings", "sources", "help", "other"}:
            intent = "other"
        return {
            "intent": intent,
            "query": str(data.get("query") or "").strip(),
            "days": int(_clamp(data.get("days"), 14) or 14),
            "index": data.get("index"),
        }

    async def parse_interests(self, text: str) -> dict[str, Any]:
        prompt = self.config.render("interest_prompt", text=text,
                                    categories=" / ".join(self.config.category_names))
        data = await self.ask_json(prompt, tier="light")
        interests = []
        for item in data.get("interests") or []:
            if not isinstance(item, dict):
                continue
            value = str(item.get("value") or "").strip()
            if not value:
                continue
            interests.append(
                {
                    "type": str(item.get("type") or "topic").lower()[:24],
                    "value": value[:120],
                    "weight": max(0.1, min(1.0, float(item.get("weight") or 1.0))),
                }
            )
        return {"interests": interests, "summary": str(data.get("summary") or "").strip()}

    async def same_event(self, a: dict[str, Any], b: dict[str, Any]) -> bool:
        prompt = self.config.render(
            "deduplication_prompt",
            title_a=a.get("title", ""), source_a=a.get("source_name", ""),
            excerpt_a=_clip(a.get("content") or a.get("title") or "", 800),
            title_b=b.get("title", ""), source_b=b.get("source_name", ""),
            excerpt_b=_clip(b.get("content") or b.get("title") or "", 800),
        )
        try:
            data = await self.ask_json(prompt, tier="light")
        except LLMError:
            return False
        return bool(data.get("same_event")) and _clamp(data.get("confidence"), 0) >= 0.75


def _clip(text: str | None, limit: int) -> str:
    text = re.sub(r"\n{3,}", "\n\n", str(text or "")).strip()
    return text if len(text) <= limit else text[:limit] + " …"


_instance: LLMService | None = None


def get_llm() -> LLMService:
    global _instance
    if _instance is None:
        _instance = LLMService()
    return _instance


async def close_llm() -> None:
    global _instance
    if _instance is not None:
        await _instance.close()
        _instance = None
