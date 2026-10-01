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


# 采集到的社区帖子里有别人贴出来的账号密码（组合列表）。真机 2026-10-01 数到
# 1946 行里有 3 行是这个形状，#1720 一份同时躺在 summary/summary_zh/content 三个字段里。
# 尾巴只吃 ASCII 可打印字符：中文正文里嵌一个邮箱时，不能把后面的句子一起吞掉。
_CREDENTIAL = re.compile(r"[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})[!-~]*")


def redact_secrets(value: str) -> str:
    """邮箱只留域名，紧跟其后的 token 串整个去掉。

    放在 `esc()` 里而不是各个面板里：`esc` 是本项目所有用户可见文本的唯一出口，
    一处修好等于所有面修好——写在 `ArticleView` 上会漏掉 `/免费` 那一类直接喂
    `display_summary` 的面板（v1.75/v1.80/v1.83 反复验证过的"第二份实现更弱"）。
    """
    if "@" not in value:
        return value
    return _CREDENTIAL.sub(r"***@\1", value)


def esc(value: Any) -> str:
    return html.escape(redact_secrets(str(value if value is not None else "")), quote=False)


def link(text: str, url: str) -> str:
    """正文和 href 走同一道去毒：凭据常常就写在链接的参数里。

    v1.88 只在 `esc()` 里遮，那是文本出口；`href` 走的是 `html.escape` 这条路。
    真机 2026-10-01 数到：库里 1946 行 URL 带 `@` 的是 0 行、ETag 键里邮箱形状 0 条，
    所以这条是**潜伏**（没有一条新闻现在真的会推给他），但它是 v1.88 自己写下的边界。
    顺序是先遮再转义：遮完的串仍是普通文本，交给 `html.escape` 处理引号。
    """
    safe = html.escape(redact_secrets(url or ""), quote=True)
    return f'<a href="{safe}">{esc(text)}</a>'


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


_TZ_CITY = {
    "Asia/Shanghai": "上海", "Asia/Chongqing": "重庆", "Asia/Urumqi": "乌鲁木齐",
    "Asia/Hong_Kong": "香港", "Asia/Macau": "澳门", "Asia/Taipei": "台北",
    "Asia/Tokyo": "东京", "Asia/Seoul": "首尔", "Asia/Singapore": "新加坡",
    "Asia/Kolkata": "加尔各答", "Asia/Dubai": "迪拜",
    "Europe/London": "伦敦", "Europe/Paris": "巴黎", "Europe/Berlin": "柏林",
    "Europe/Moscow": "莫斯科", "America/New_York": "纽约", "America/Chicago": "芝加哥",
    "America/Los_Angeles": "洛杉矶", "America/Sao_Paulo": "圣保罗",
    "Australia/Sydney": "悉尼", "Pacific/Auckland": "奥克兰", "UTC": "协调世界时",
}


def timezone_label(name: str | None) -> str:
    """`Asia/Shanghai` → "上海 UTC+8"：设置页上那句"按哪个钟"不能是英文标识符。

    偏移当场用 zoneinfo 算而不是写死（调两次表的区，写死的那半年就是假话）；
    认不出城市名的区只报偏移，认不出区本身才原样回显 - 那时偏移也无从算起。
    空名字跟着调度器同一套回落走（`_zone("")` 也是 UTC），所以报 UTC+0。
    """
    raw = str(name or "").strip()
    try:
        from zoneinfo import ZoneInfo

        offset = datetime.now(ZoneInfo(raw or "UTC")).utcoffset() or timedelta(0)
    except Exception:
        return esc(raw)
    minutes = int(offset.total_seconds() // 60)
    hours, remainder = divmod(abs(minutes), 60)
    stamp = f"UTC{'+' if minutes >= 0 else '-'}{hours}" + (f":{remainder:02d}" if remainder else "")
    city = _TZ_CITY.get(raw)
    return f"{city} {stamp}" if city else stamp


def score_emoji(score: float, config: AppConfig | None = None,
                *, cohort: Sequence[float] | None = None) -> str:
    """🔥/⭐/🔹 - where this row stands among the rows on the same page.

    An absolute cut-off cannot work here, and this file has now been re-tuned for
    it twice: v1.45 set the bars at the live percentiles (62/54/49), and tonight's
    晚报 still printed 🔥 on all 8 rows, because a briefing is by construction the
    *top* of the pool - the evening's eight items spanned 65.6..78.0, entirely
    above the 🔥 line. A marker that every row shares carries no information, and
    here it actively fought the "🔥 今日重点" heading three rows above it.

    Ranking inside the rendered page is immune to where the scale sits, so it needs
    no recalibration when scoring changes. `dot_score` stays absolute: a row below
    the briefing floor is ▫️ however it ranks, so a weak page never crowns anything.
    Pages of one or two rows cannot be ranked, and fall back to the absolute bars.
    """
    config = config or get_config()
    hot, star, dot = breaking.emoji_bars(config)
    if score < dot:
        return "▫️"
    if cohort and len(cohort) >= 3:
        ordered = sorted(cohort, reverse=True)
        rank = ordered.index(score)                  # ties share the best band
        third = max(1, len(ordered) // 3)
        return "🔥" if rank < third else "⭐" if rank < 2 * third else "🔹"
    if score >= hot:
        return "🔥"
    if score >= star:
        return "⭐"
    return "🔹"


def digest_header(kind: str, config: AppConfig | None = None, *, date: str | None = None) -> str:
    config = config or get_config()
    header = config.get(f"digest.{kind}.header") or config.get(f"digest.{kind}.title") or "AI 简报"
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
    shown = items[: len(CIRCLE)]
    cohort = [float(i.final_score or 0) for i in shown]
    for index, item in enumerate(shown):
        number = CIRCLE[index]
        head = item.display_line
        marker = (f" {score_emoji(item.final_score, config, cohort=cohort)}{item.final_score:.0f}"
                  if show_scores else "")
        date = short_date(item.published_at, tz_name)
        source = esc(item.source_name)
        if with_links:
            lines.append(f"{number} {link(esc(head)[:120], item.url)}{marker}")
        else:
            lines.append(f"{number} {esc(head)[:120]}{marker}")
        lines.append(f"   <i>{source} · {date} · {esc(config.category_label(item.category))}</i>")
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
    deep_points = (deep or {}).get("key_points") or []
    points = deep_points or item.display_key_points or []
    if points:
        # 说清楚这段是原文：藏起来看起来像卡片坏了，不标出来看起来像翻错了。
        heading = ("核心内容（以下为原文，中文翻译还没轮到）"
                   if not deep_points and item.key_points_in_english else "核心内容")
        out.append(f"<b>{heading}：</b>")
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
    if item.event_sources:
        # 只列别家媒体（`news._view` 已排除本条自己的来源）；一家的转载不配这一行。
        out += ["", "<b>相关来源：</b>", f"{BULLET} {esc('、'.join(item.event_sources))}"]
    if item.tags:
        out += ["", "🏷 " + " ".join(f"#{esc(t.replace(' ', ''))}" for t in item.tags[:6])]
    text = "\n".join(out)
    return clip(text)


def breaking_card(item: ArticleView, *, config: AppConfig | None = None, tz_name: str = "UTC") -> str:
    config = config or get_config()
    header = config.get("breaking.header", "🚨 AI 突发新闻")
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
    # One cohort for the whole briefing, so a category row and the 今日重点 rows
    # are ranked against the same set instead of each section re-crowning its own
    # leader.
    cohort = [float(a.final_score or 0) for a in items]

    def line(item: ArticleView, idx: int) -> str:
        head = item.display_line
        date = short_date(item.published_at, tz_name)
        row = f"{idx}. {link(shorten(head, 150), item.url)}"
        # 第二行给"没当上标题的那一条"：中文摘要打头时补标题，标题打头时补摘要
        # （摘要没翻译就照原样显示英文，2026-09-26 他的决定）。
        second = item.display_summary if head == item.display_title else item.display_title
        if second and second.lower() != head.lower():
            row += f"\n   <i>{esc(shorten(second, 110))}</i>"
        row += (f"\n   {esc(item.source_name)} · {date} · "
                f"{score_emoji(item.final_score, config, cohort=cohort)}{item.final_score:.0f}")
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
                    unverified: bool = False, total: int | None = None) -> str:
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
    lines = [f"{title}（{days} 天内 {offer_scope(total, len(items))}{scope}）", ""]
    if note:
        lines += [esc(note), ""]
    if live:
        lines += [live, ""]
    for index, item in enumerate(items[:12], start=1):
        offer = item.free_offer or {}
        kind = offer.get("kind") or "其他"
        # 关键词回落出来的条目不是限免，别再挂 🎁 误导人
        emoji = "📰" if unverified else kind_emoji(kind, config)
        tool_name = offer.get("tool") or item.display_title[:30]
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


def utf16_len(value: str) -> int:
    """Telegram 数的长度是 **UTF-16 码元**，不是 Python 的字符数：🟢 占两个。"""
    return len((value or "").encode("utf-16-le")) // 2


def clip(value: str, limit: int = SAFE_LIMIT) -> str:
    """Never exceed Telegram's message limit; cut on a line break and say so.

    按码元量：一条全是 emoji 的 3000 字消息，`len()` 说 3000，Telegram 看到的是
    6000，会被整条拒收——而"被拒收"在用户那边等于"我问了，没有回答"。
    """
    text = value or ""
    mark = "\n…（内容过长已截断）"
    room = limit - utf16_len(mark)
    if utf16_len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, max(1, room))
    cut = cut if cut > room // 2 else max(1, room)
    head = text[:cut]
    while head and utf16_len(head) > room:
        head = head[: max(0, len(head) - 40)]
    return head.rstrip() + mark


def plain(value: str, limit: int = 200) -> str:
    text = re.sub(r"<[^>]+>", "", value or "")
    return text.strip()[:limit]


def source_health_note(stats: dict[str, Any]) -> str:
    """持续失败的按名字说，抖一下的只说个数。

    以前这两种共用"正在报错"四个字：2026-09-30 线上 `/stats` 报"3 个正在报错"时，
    那三个是 GitHub 匿名配额抖动（计数 1-2，下一轮自愈），而真正的坏源（Reddit 连着
    15 次 403）报的是同一句话。能行动的信息和噪音必须分开写。
    """
    detail = list(stats.get("sources_failing_detail") or [])
    failing = int(stats.get("sources_failing") or 0) or len(detail)
    blipping = int(stats.get("sources_blipping") or 0)
    bits: list[str] = []
    if failing:
        named = "、".join("%s（连续 %s 次：%s）" % (
            str(item.get("name") or "?"), item.get("errors"),
            str(item.get("error") or "").strip()[:40] or "原因未记录") for item in detail[:2])
        more = f" 等共 {failing} 个" if failing > len(detail[:2]) else ""
        bits.append(f" · ⛔ 持续失败：{named or f'{failing} 个'}{more}")
    if blipping:
        bits.append(f" · {blipping} 个刚抖了一下（下一轮自动重试）")
    return "".join(bits)


def source_state_flag(state: dict[str, Any], *, enabled: bool = True,
                      fail_threshold: int = 5) -> tuple[str, str]:
    """`/来源` 每行的 (旗子, 说明行)。

    口径必须和 `/stats`、健康检查一致（v1.67 分的两类：连击到警戒线才算坏源，
    1..阈值-1 次只是刚抖了一下）：**只有连续失败才是坏源**。旧代码看
    `last_error` 非空就涂红，而 v1.68 之后"我们在守对方给的 Retry-After"也会留下
    `last_error` 却不计入连击——那会让一个准点干活的源被画成 🔴。
    等待与否用采集器此刻真实的退避状态（`wait_left` 秒）判断，不去猜错误文案；
    真在等的时候也不再回抄 `last_error`，因为那一格里写的本来就是同一句话。
    """
    if not enabled:
        return "⚪️", ""
    errors = int(state.get("error_count") or 0)
    wait_left = float(state.get("wait_left") or 0)
    succeeded = bool(state.get("last_success_at"))
    parts: list[str] = []
    if errors:
        flag = "🔴" if errors >= fail_threshold else "🟠"
        parts.append(f"连续失败 {errors} 次" if errors >= fail_threshold
                     else f"偶发失败 {errors} 次（警戒线 {fail_threshold} 次）")
        if wait_left <= 0:
            text = plain(str(state.get("last_error") or ""), 120)
            if text:
                parts.append(text)
    else:
        flag = "🟢" if succeeded else "🟡"
    if wait_left > 0:
        parts.append(f"正在按对方要求降速，约 {max(1, int(wait_left / 60))} 分钟后再问")
    elif not errors:
        # 只剩这一种情况会写 `last_error` 而不涨连击：我们自己这轮没去问（守对方的
        # Retry-After，或 GitHub 匿名配额已用完）。真实失败会被下一次成功清掉，
        # 所以"0 次失败 + 有一句错误"必然是一次推迟，说成说明而不是红灯。
        note = plain(str(state.get("last_error") or ""), 160)
        if note:
            parts.append(f"这一轮没有去问它：{note}")
    return flag, ("   <i>" + "；".join(parts) + "</i>" if parts else "")


def search_title(query: str, *, matched: int, shown: int, days: int,
                 pool_capped: bool = False, pool: int = 0) -> str:
    """检索标题：先说命中几条，再说这一页几条，池子满了就承认数得保守。

    以前这一行是 `f"最近 {days} 天 {len(items)} 条"`，而 `items` 是被 `limit` 截过的
    一页：线上 `/search Claude` 于是对 245 条真实命中说"20 条"。
    """
    title = f"🔎 “{esc(plain(query, 40))}” · 最近 {days} 天命中 {matched} 条"
    if matched > shown:
        title += f"，这里列出前 {shown} 条"
    if pool_capped:
        title += f"（只数了近 {pool} 条里的命中，关键词越宽这个数越保守）"
    return title


def offer_scope(total: int | None, shown: int) -> str:
    """"共 245 条，这里列出最新 12 条"——以前这一行只会说"12 条"，那是页大小。"""
    if total is None or total <= shown:
        return f"{shown} 条"
    return f"共 {total} 条，这里列出最新 {shown} 条"


def pending_note(stats: dict[str, Any]) -> str:
    """积压要连着"最久等了多久"一起说：条数看不出急不急，6 小时才是突发时效的分水岭。"""
    pending = int(stats.get("pending_processing") or 0)
    if not pending:
        return ""
    hours = stats.get("processing_oldest_hours")
    if hours is None:
        return f" · 待处理 {pending} 条"
    mark = "⚠️ " if float(hours) >= 6 else ""
    return f" · {mark}待处理 {pending} 条（最久 {float(hours):.1f} 小时）"


def status_line(stats: dict[str, Any]) -> str:
    return (
        f"📈 库内新闻：{stats.get('total_articles', 0)} 条 · 近 24 小时 {stats.get('last_24h', 0)} 条"
        f"{pending_note(stats)}\n"
        f"🧠 AI 处理：{'已启用' if stats.get('llm_enabled') else '未配置（使用规则模式）'}\n"
        f"🔌 数据源：{stats.get('sources', 0)} 启用 / {stats.get('sources_configured', 0)} 配置"
        f" · 近 24 小时出过新闻 {stats.get('sources_delivering', 0)} 个"
        + source_health_note(stats)
        + disk_line(stats)
    )


def paged_header(title: str, total: int, shown: int, *, page: int = 1, pages: int = 1,
                 more: str = "") -> str:
    """列表的第一行：名字 + 真实总数 + 这一页是其中哪一段 + 下一步去哪。

    翻页按钮以前把每一页都重新写成"🤖 AI 新闻"：`/today` 的第 2 页顶部换了名字，
    而 v1.84 刚加上的"共 102 条"在第二页上直接消失。标题必须由同一处生成，
    这样第一页和后面的每一页说的是同一件事。
    """
    parts = [title]
    if total <= 0:
        return title
    parts.append(f"共 {total} 条")
    if pages > 1:
        parts.append(f"这批 {shown} 条的第 {page}/{pages} 页")
    elif shown < total:
        parts.append(f"这里列出最新 {shown} 条")
    if more and total > shown:
        parts.append(f"更多请用 {more}")
    return " · ".join(parts)


def score_scope(value: float, *, ceiling: float, eligible: int) -> str:
    """`/设置` 里"最低评分"那一行：数字 + 它的上限 + 这一档现在有几条达标。

    面板原来只写 `📊 最低评分：45`，而"上限是多少"和"提到 70 会发生什么"都要他自己猜。
    真机 2026-10-01 近 24 小时的实测：45→320 条、50→252、60→71、70→10、78→3、**80→0**。
    0 那一档必须当场说，因为他一旦停在那里，第二天看到的是空简报而不是这条解释。
    """
    text = f"📊 最低评分：{value:.0f}（上限 {ceiling:.0f}"
    if value > ceiling:
        # 历史上他能一路点到 90（v1.94 之前 clamp 是 90），所以库里可能真有高于上限的行；
        # 对那种行说"已经是最高的了"是错的——他不在最高档，他早在够不着的区间里。
        text += f"，已超过上限 {ceiling:.0f}"
    elif value == ceiling:
        text += "，已经是最高的了"
    text += f" · 近 24 小时 {eligible} 条达标）"
    if eligible == 0:
        text += "\n   ⚠️ 这一档现在一条都不达标，早晚报会是空的；点 🔽 降下来才有内容"
    return text


def quota_line(quota: dict[str, Any] | None) -> str:
    """今天还能不能收到突发：把正在生效的上限写在他看的那一屏上。

    配额用完时"突发新闻：开"是一个会让人白等的状态（2026-10-01 实测 5/5 用光后，
    #1813 只能拿到 `daily cap reached (5/5)`，要等到当地零点）。
    """
    if not quota:
        return ""
    limit = int(quota.get("limit") or 0)
    if limit <= 0:
        return ""
    used = int(quota.get("used") or 0)
    cooldown = int(quota.get("cooldown_minutes") or 0)
    pace = f"，最快每 {cooldown} 分钟一条" if cooldown > 0 else ""
    if used >= limit:
        return (f"今日突发名额已用完 {used}/{limit}"
                f"（下一条要等当地 00:00 之后{pace}）")
    return f"今日还可推送 {limit - used}/{limit} 条{pace}"


def disk_rate(stats: dict[str, Any]) -> str:
    """磁盘"方向"的那句话——`/stats` 与维护日志共用一份。

    以前两处各写一份，连措辞都不一样（"24h 方向未知" vs "24h 方向还不知道（样本不够）"），
    而两边都把窗口写成"最近 24h"。可 2026-10-01 真机只有 9 个点、跨 4.27 小时：
    同一行里"-58MB"是 4 小时的净变化，"约 5.4 天写满"又是把它放大成一天速率算的——
    两个印在一起的数字互相反悔（读者拿 1758÷58 会算出 30 天，然后以为坏了）。
    所以窗口按实际跨度写，天数只在样本够长（≥12 小时）时才给。
    """
    delta = stats.get("disk_delta_mb")
    span = stats.get("disk_span_hours")
    days = stats.get("disk_days_left")
    if delta is None:
        return "方向还不知道（样本不够）"
    window = ("最近 24 小时" if (span or 0) >= 23
              else "最近 %s 小时" % ("%.1f" % span if span else "?"))
    if delta >= 0:
        return "%s %s%dMB，没有在变少" % (window, "+" if delta else "±", abs(int(delta)))
    if days:
        return "%s %dMB，照这个速度约 %s 天写满" % (window, int(delta), days)
    return "%s %dMB，在变少；样本只跨 %s 小时，还不够算「还剩几天」" % (
        window, int(delta), "%.1f" % span if span else "?")


def disk_line(stats: dict[str, Any], config: AppConfig | None = None) -> str:
    """磁盘：数字 **和方向**。只给"剩多少"会被读成倒计时，也会被读成没事。

    量过的两个点：09-26 剩 769MB、10-01 剩 1818MB，中间那 1GB 是 logrotate 放回来的。
    所以"变小了"要配天数、"变大了/没变"要明说没有在变少，样本不够就承认还不知道。
    """
    free = stats.get("disk_free_mb")
    if free is None:
        return ""
    free = int(free)
    threshold = int((config or get_config()).get("alerts.min_free_mb", 1024))
    rate = " · " + disk_rate(stats)
    if free > threshold:
        return f"\n💾 磁盘：剩 {free}MB{rate}"
    return (f"\n⚠️ 磁盘只剩 {free / 1024:.1f}GB（低于 {threshold / 1024:.0f}GB 告警线）{rate}，"
            "采集随时可能因写不进数据库而停住")
