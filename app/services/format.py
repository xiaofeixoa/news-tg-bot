"""Telegram message formatting (design doc sections 13, 15, 16).

Kept in the service layer so digests and interactive handlers render identically
and the bot layer stays thin.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from app.config import AppConfig, get_config
from app.processing import breaking
from app.processing.normalize import shorten
from app.services.news import ArticleView

MAX_MESSAGE = 4096
# Leave headroom for HTML entities and the inline keyboard payload living next
# to the text.
SAFE_LIMIT = 3700

CIRCLE = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
BULLET = "•"


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def link(text: str, url: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{esc(text)}</a>'


def tznow(name: str) -> datetime:
    return datetime.now(_zone(name))


def _zone(name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name or "UTC")
    except Exception:  # pragma: no cover
        return timezone.utc


def short_date(value: datetime | None, tz_name: str = "UTC") -> str:
    if not value:
        return ""
    return value.replace(tzinfo=timezone.utc).astimezone(_zone(tz_name)).strftime("%m-%d %H:%M")


def score_emoji(score: float, config: AppConfig | None = None) -> str:
    config = config or get_config()
    hot, star, dot = breaking.emoji_bars(config)
    if score >= hot:
        return "🔥"
    if score >= star:
        return "⭐"
    if score >= dot:
        return "🔹"
    return "▫️"


def digest_header(kind: str, config: AppConfig | None = None, *, date: str | None = None) -> str:
    config = config or get_config()
    header = config.get(f"digest.{kind}.header") or config.get(f"digest.{kind}.title") or "AI Briefing"
    lines = [header]
    if date:
        lines.append(f"📅 {esc(date)}")
    return "\n".join(lines)


def news_list(
    items: Sequence[ArticleView],
    *,
    config: AppConfig | None = None,
    tz_name: str = "UTC",
    title: str = "🤖 今日 AI 新闻",
    show_scores: bool = False,
    with_links: bool = True,
) -> str:
    """Numbered, tappable headline list (design doc section 15)."""
    config = config or get_config()
    if not items:
        return f"{esc(title)}\n\n还没有符合条件的新闻，稍后再试试。"
    lines = [esc(title)]
    for index, item in enumerate(items[: len(CIRCLE)]):
        number = CIRCLE[index]
        head = item.display_line
        marker = f" {score_emoji(item.final_score, config)}{item.final_score:.0f}" if show_scores else ""
        date = short_date(item.published_at, tz_name)
        source = esc(item.source_name)
        if with_links:
            lines.append(f"{number} {link(esc(head)[:120], item.url)}{marker}")
        else:
            lines.append(f"{number} {esc(head)[:120]}{marker}")
        lines.append(f"   <i>{source} · {date} · {esc(item.category or 'Other')}</i>")
    return "\n".join(lines)


def article_card(
    item: ArticleView,
    *,
    config: AppConfig | None = None,
    tz_name: str = "UTC",
    deep: dict[str, Any] | None = None,
) -> str:
    """Single-article card: one-line summary, key points, why it matters (§13)."""
    config = config or get_config()
    emoji = config.get(f"digest.section_emoji.{item.category}", "📰")
    out: list[str] = [f"{emoji} <b>{esc(item.display_title)}</b>", ""]
    summary = (deep or {}).get("what_happened") or item.display_summary or ""
    if not summary and not (deep or {}).get("key_points"):
        summary = item.title if item.display_title != item.title else ""
    if summary and summary != item.display_title:
        out += ["<b>一句话总结：</b>", esc(summary), ""]
    points = (deep or {}).get("key_points") or item.display_key_points or []
    if points:
        out.append("<b>核心内容：</b>")
        out += [f"{BULLET} {esc(p)}" for p in points[:5]]
        out.append("")
    matters = (deep or {}).get("why_it_matters") or item.why_it_matters
    if matters:
        out += ["<b>为什么值得关注：</b>", esc(matters), ""]
    if (deep or {}).get("industry_impact"):
        out += ["<b>行业影响：</b>", esc(deep["industry_impact"]), ""]
    if (deep or {}).get("open_questions"):
        out.append("<b>值得继续跟踪：</b>")
        out += [f"{BULLET} {esc(q)}" for q in deep["open_questions"][:3]]
        out.append("")
    if (deep or {}).get("confidence_note"):
        out += ["<i>说明：" + esc(deep["confidence_note"]) + "</i>", ""]

    scores = (
        f"重要 {item.importance_score:.0f} · 相关 {item.relevance_score:.0f} · "
        f"新鲜 {item.novelty_score:.0f} · 热度 {item.community_heat:.0f} · "
        f"<b>综合 {item.final_score:.0f}</b>"
    )
    out += [
        f"📊 {scores}",
        f"📰 来源：{esc(item.source_name)} · {short_date(item.published_at, tz_name)}",
    ]
    if item.event_members > 1:
        names = ", ".join(item.event_sources) or f"{item.event_members} 个来源"
        out += ["", "<b>相关来源：</b>", f"{BULLET} {esc(names)}"]
    if item.tags:
        out += ["", "🏷 " + " ".join(f"#{esc(t.replace(' ', ''))}" for t in item.tags[:6])]
    text = "\n".join(out)
    return clip(text)


def breaking_card(item: ArticleView, *, config: AppConfig | None = None, tz_name: str = "UTC") -> str:
    config = config or get_config()
    header = config.get("breaking.header", "🚨 AI BREAKING NEWS")
    body = article_card(item, config=config, tz_name=tz_name)
    return clip(f"{esc(header)}\n\n{body}")


def section_blocks(
    items: Sequence[ArticleView],
    *,
    config: AppConfig | None = None,
    tz_name: str = "UTC",
    start_number: int = 1,
    top_count: int = 3,
    show_summary: bool = True,
) -> list[str]:
    """Render a briefing as 🔥 今日重点 + per-category sections (§16.1)."""
    config = config or get_config()
    order: list[str] = list(config.get("digest.section_order", []) or [])
    emoji_table = config.get("digest.section_emoji", {}) or {}
    number = start_number

    def line(item: ArticleView, idx: int) -> str:
        head = item.display_line
        date = short_date(item.published_at, tz_name)
        row = f"{idx}. {link(shorten(head, 150), item.url)}"
        # 第二行给"没当上标题的那一条"：中文摘要打头时补标题，标题打头时补摘要
        # （摘要没翻译就照原样显示英文，2026-09-26 他的决定）。
        second = item.display_summary if head == item.display_title else item.display_title
        if second and second.lower() != head.lower():
            row += f"\n   <i>{esc(shorten(second, 110))}</i>"
        row += f"\n   {esc(item.source_name)} · {date} · {score_emoji(item.final_score, config)}{item.final_score:.0f}"
        return row

    blocks: list[str] = []
    top = sorted(items, key=lambda a: a.final_score, reverse=True)[:top_count]
    if top:
        blocks.append(
            f"<b>{emoji_table.get('Top', '🔥')} 今日重点</b>\n"
            + "\n".join(line(a, number + i) for i, a in enumerate(top))
        )
        number += len(top)
    rest = [a for a in items if a not in top]
    grouped: dict[str, list[ArticleView]] = {}
    for item in rest:
        grouped.setdefault(item.category or config.fallback_category, []).append(item)
    ordered_keys = [c for c in order if c in grouped] + [c for c in grouped if c not in order]
    for category in ordered_keys:
        members = grouped[category]
        marker = emoji_table.get(category, "📰")
        text = f"<b>{marker} {esc(config.category_label(category))}</b>\n" + "\n".join(
            line(a, number + i) for i, a in enumerate(members)
        )
        blocks.append(text)
        number += len(members)
    return blocks


def split_messages(chunks: Iterable[str], *, limit: int = SAFE_LIMIT, header: str = "",
                   footer: str = "") -> list[str]:
    """Telegram hard-caps a message at 4096 chars; keep HTML blocks intact."""
    prefix = f"{header}\n\n" if header else ""
    suffix = f"\n\n{footer}" if footer else ""
    messages: list[str] = []
    current = ""
    for chunk in chunks:
        candidate = chunk if not current else f"{current}\n\n{chunk}"
        if len(prefix) + len(candidate) + len(suffix) <= limit:
            current = candidate
            continue
        if current:
            messages.append(prefix + current + suffix)
        # A single oversized block: hard-split it rather than dropping it.
        while len(chunk) > limit - len(prefix) - len(suffix):
            cut = chunk.rfind("\n", 0, limit - len(prefix) - len(suffix))
            cut = cut if cut > 200 else limit - len(prefix) - len(suffix)
            messages.append(prefix + chunk[:cut] + suffix)
            chunk = chunk[cut:].lstrip("\n")
        current = chunk
    if current:
        messages.append(prefix + current + suffix)
    return messages or [prefix + (header or "AI News Radar") + suffix]


def free_offer_list(items: Sequence[ArticleView], *, config: AppConfig | None = None,
                    tz_name: str = "UTC", days: int = 30, tool: str | None = None,
                    with_links: bool = True, live: str = "", note: str = "",
                    live_checked: bool = True, heading: str | None = None,
                    unverified: bool = False) -> str:
    """Render the /免费 answer: 什么工具/模型现在免费."""
    config = config or get_config()
    from app.processing.free_offers import kind_emoji

    scope = f" · {esc(tool)}" if tool else ""
    if not items:
        # 没有采集到的限免资讯时，实时免费模型就是全部答案
        if live:
            return clip(f"🎁 <b>近期免费 / 限免</b>{scope}\n\n"
                        f"最近 {days} 天没有采到明确的限免公告，"
                        f"下面是当前实测免费的东西：\n\n{live}")
        if tool and not live_checked:
            return (f"🎁 近期免费资讯{scope}\n\n"
                    f"最近 {days} 天没有采到“{esc(tool)}”的限免公告，"
                    f"而且实时定价接口此刻也没连上，暂时给不出结论。\n"
                    f"可以稍后再试，或 <code>/free {days * 3}</code> 拉长窗口。")
        if tool:
            return (f"🎁 近期免费资讯{scope}\n\n"
                    f"最近 {days} 天没有采到“{esc(tool)}”的限免公告，"
                    f"实时定价接口里也没有同名的免费模型。\n"
                    f"（agent 的限免只会出现在新闻里，模型免费才走定价接口。）\n"
                    f"可以试试：<code>/free</code> 看全部，或 <code>/free {days * 3}</code>。")
        return (f"🎁 近期免费资讯{scope}\n\n"
                f"最近 {days} 天没有发现明确的“免费/限免”消息。\n"
                f"可以试试：<code>/free deepseek</code> 指定关键词，"
                f"或等下一轮采集（社区源里免费额度公告最多）。")

    # A caller-supplied heading brings its own emoji.
    title = f"{esc(heading)}" if heading else "🎁 <b>近期免费 / 限免</b>"
    lines = [f"{title}（{days} 天内 {len(items)} 条{scope}）", ""]
    if note:
        lines += [esc(note), ""]
    if live:
        lines += [live, ""]
    for index, item in enumerate(items[:12], start=1):
        offer = item.free_offer or {}
        kind = offer.get("kind") or "其他"
        # 关键词回落出来的条目不是限免，别再挂 🎁 误导人
        emoji = "📰" if unverified else kind_emoji(kind, config)
        tool_name = offer.get("tool") or item.title[:30]
        models = offer.get("models") or []
        signals = offer.get("signals") or []
        head = item.display_summary or item.display_title
        title_part = f"{emoji} <b>{esc(tool_name)}</b>"
        if models:
            title_part += f" · {esc('、'.join(models[:3]))}"
        lines.append(f"{index}. {title_part}")
        line = f"   {link(head[:120], item.url) if with_links else esc(head[:120])}"
        lines.append(line)
        detail = f"   {esc(item.source_name)} · {short_date(item.published_at, tz_name)}"
        if signals:
            detail += f" · {esc(' / '.join(signals[:2]))}"
        if offer and offer.get("subject_in_title") is False:
            # 主体只在正文里出现过，读者应该知道这条是推断出来的
            detail += " · 推断自正文"
        lines.append(detail)
        expiry = offer.get("expiry")
        if expiry:
            lines.append(f"   ⏳ {esc(expiry)}")
        lines.append("")
    if unverified:
        lines.append("<i>以上只是与关键词相关的新闻，不是限免确认；"
                     "真正 0 价的模型看上面的 ⚡ 块。</i>")
    else:
        lines.append("<i>判断依据：正文里同时出现“免费信号”和“具体工具/模型”。"
                     "促销随时变动，用之前请以官网为准。</i>")
    return clip("\n".join(lines))


def _ctx_label(value: int) -> str:
    if value >= 1_000_000:
        return f"{round(value / 1_000_000, 1):g}M"
    return f"{value // 1000}K"


def free_models_section(models: Sequence[Any], *, config: AppConfig | None = None,
                        limit: int = 8, total: int = 0, source: str = "OpenRouter") -> str:
    """The live half of /免费: what costs 0 right now, straight from the gateway."""
    if not models:
        return ""
    shown = list(models)[:limit]
    overall = total or len(shown)
    scope = f"（共 {overall} 个，列出最新 {len(shown)} 个）" if overall > len(shown) \
        else f"（{overall} 个）"
    lines = [f"⚡ <b>{esc(source)} 现在免费可用的模型</b>{scope}", ""]
    for index, model in enumerate(shown, start=1):
        name = model.name or model.id
        lines.append(f"{index}. 🧩 {link(name, model.url) if hasattr(model, 'url') else esc(name)}")
        bits = [esc(model.id)]
        if model.context_length:
            bits.append(f"上下文 {_ctx_label(model.context_length)}")
        days = model.days_free()
        if days:
            bits.append(f"已免费 {days} 天")
        if getattr(model, "vendors", None):
            bits.append(esc("、".join(model.vendors[:3])))
        lines.append(f"   {' · '.join(bits)}")
    lines.append("")
    lines.append(f"<i>数据来源：{esc(source)} 公开定价接口，实时；"
                 "在自己的 agent 里填对应模型 id 即可。</i>")
    return "\n".join(lines)


def free_trend_section(newly: Sequence[Any], ended: Sequence[str], *,
                       source: str = "OpenRouter") -> str:
    """What changed in the free tier since the last look.

    "近期什么模型免费" is a question about change, and a flat list cannot answer
    it: the model that stopped being free yesterday is the one piece of news in
    the whole snapshot.
    """
    lines: list[str] = []
    for model in list(newly)[:6]:
        name = getattr(model, "name", None) or getattr(model, "id", str(model))
        lines.append(f"🆕 {esc(name)} 开始免费")
    for name in list(ended)[:6]:
        lines.append(f"⛔ {esc(name)} 已结束免费")
    if not lines:
        return ""
    return "📈 <b>与上次相比的变化</b>\n" + "\n".join(lines) + \
        f"\n<i>对比对象：{esc(source)} 上一次快照。</i>"


def help_text(command_names: Sequence[str] | None = None) -> str:
    commands = [
        ("/news", "最新 AI 新闻（编号点击查看详细）"),
        ("/latest", "最近 24 小时新闻"),
        ("/today", "今日新闻"),
        ("/yesterday", "昨日新闻"),
        ("/digest", "立即生成今日简报"),
        ("/search 关键词", "搜索历史新闻，例如 /search MCP"),
        ("/summary 编号", "对某条新闻做 AI 深度分析，例如 /summary 123"),
        ("/topics", "按分类查看"),
        ("/free 或 /免费", "近期哪些 agent / 模型免费，如 /free qoder"),
        ("/sources", "查看信息来源与状态"),
        ("/settings", "推送时间与开关"),
        ("/setinterest", "用自然语言设置兴趣"),
        ("/pause / /resume", "暂停 / 恢复自动推送"),
        ("/help", "本帮助"),
    ]
    wanted = {c.split()[0].strip() for c in command_names} if command_names else None
    lines = ["<b>AI News Radar 使用指南</b>", "", "我是你的个人 AI 新闻 Agent：",
             "采集 RSS / Hacker News / GitHub / arXiv 等来源，去重、分类、评分后用中文摘要推送。", ""]
    for command, description in commands:
        if wanted is None or command_name(command) in wanted:
            lines.append(f"<code>{esc(command)}</code> — {esc(description)}")
    lines += ["", "也可以直接问，例如：", "<i>最近 AI Agent 有什么值得关注的？</i>",
              "<i>今天有哪些重要的 AI 新闻？</i>", "<i>第二条详细说一下</i>"]
    return "\n".join(lines)


def command_name(command: str) -> str:
    return command.split()[0].strip()


def clip(value: str, limit: int = SAFE_LIMIT) -> str:
    """Never exceed Telegram's 4096-char message limit; cut on a line break."""
    text = value or ""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit - 12)
    cut = cut if cut > limit // 2 else limit - 12
    return text[:cut].rstrip() + "\n…（内容过长已截断）"


def plain(value: str, limit: int = 200) -> str:
    text = re.sub(r"<[^>]+>", "", value or "")
    return text.strip()[:limit]


def status_line(stats: dict[str, Any]) -> str:
    return (
        f"📈 库内新闻：{stats.get('total_articles', 0)} 条 · 近 24 小时 {stats.get('last_24h', 0)} 条\n"
        f"🧠 AI 处理：{'已启用' if stats.get('llm_enabled') else '未配置（使用规则模式）'}\n"
        f"🔌 数据源：{stats.get('sources', 0)} 启用 / {stats.get('sources_configured', 0)} 配置"
        f" · 近 24 小时出过新闻 {stats.get('sources_delivering', 0)} 个"
        + (f" · {stats.get('sources_failing')} 个正在报错" if stats.get("sources_failing") else "")
    )
