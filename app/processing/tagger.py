"""Tag extraction: rule tags for free, AI tags when available (section 9)."""

from __future__ import annotations

import re
from typing import Any

from app.config import AppConfig, get_config

ACRONYMS = [
    "MCP", "RAG", "LLM", "GPT", "API", "GPU", "CUDA", "SGLang", "vLLM", "Ollama", "A100", "H100",
    "H200", "B200", "Mi300X", "TPU", "RLHF", "DPO", "LoRA", "AWQ", "GGUF", "BERT", "CNN", "ViT",
    "Whisper", "SDXL", "Flux", "Gemini", "Claude", "Codex", "Copilot", "Cursor", "DeepSeek",
    "Qwen", "Llama", "Mistral", "Grok", "Phi", "Gemma", "Llama", "Sora", "Veo", "Nvidia",
]
COMPANIES = [
    "OpenAI", "Anthropic", "Google", "DeepMind", "Meta", "Microsoft", "Amazon", "AWS", "Nvidia",
    "Apple", "Tesla", "Mistral", "Cohere", "Hugging Face", "GitHub", "xAI", "Alibaba", "Tencent",
    "Moonshot", "MiniMax", "Baidu", "Intel", "AMD", "IBM", "Salesforce", "Perplexity",
]
TOPIC_WORDS = [
    "fine-tuning", "inference", "benchmark", "open source", "open weights", "dataset", "agent",
    "agentic", "multimodal", "reasoning", "quantization", "distillation", "embedding",
    "vision", "speech", "video generation", "image generation", "robotics", "safety",
    "alignment", "eval", "context window", "pricing", "funding", "acquisition", "regulation",
]
PATTERN = re.compile(r"\b(" + "|".join(re.escape(w) for w in ACRONYMS + COMPANIES + TOPIC_WORDS) + r")\b")


def rule_tags(article: dict[str, Any], config: AppConfig | None = None) -> list[str]:
    text = f"{article.get('title', '')}. {article.get('content') or article.get('summary') or ''}"
    found: list[str] = []
    for match in PATTERN.finditer(text):
        tag = match.group(1).strip()
        if tag.lower() == "llama":
            tag = "Llama"
        if tag not in found:
            found.append(tag)
        if len(found) >= 6:
            break
    if not found:
        source = str(article.get("source_name") or "").strip()
        if source and source not in {"Hacker News", "GitHub Trending", "Reddit"}:
            found.append(source)
    return found


def merge_tags(*groups: list[str] | None, limit: int = 8) -> list[str]:
    out: list[str] = []
    for group in groups:
        for tag in group or []:
            clean = str(tag).strip()[:64]
            if clean and clean.lower() not in {t.lower() for t in out}:
                out.append(clean)
            if len(out) >= limit:
                return out
    return out
