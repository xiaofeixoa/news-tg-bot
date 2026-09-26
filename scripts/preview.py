"""Render what Telegram would receive, straight to the terminal.

    python scripts/preview.py digest            # morning briefing text
    python scripts/preview.py digest evening
    python scripts/preview.py news              # the /news list
    python scripts/preview.py card 123          # one article, as the bot shows it
    python scripts/preview.py ask "最近 OpenAI 有什么新闻？"
"""

from __future__ import annotations

import argparse
import asyncio
import html
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_config                      # noqa: E402
from app.logging_setup import setup_logging            # noqa: E402
from app.services.digest import DigestService          # noqa: E402
from app.services.llm import LLMService                # noqa: E402
from app.services.news import NewsService              # noqa: E402
from app.services.search import SearchService          # noqa: E402


def strip_markup(text: str) -> str:
    text = re.sub(r"<a href=\"([^\"]+)\">(.*?)</a>", r"\2 [\1]", text or "")
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def chat_id(config, explicit: int | None) -> int:
    if explicit:
        return explicit
    ids = sorted(config.settings.chat_id_whitelist)
    return ids[0] if ids else 0


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["digest", "news", "card", "ask", "stats", "free"])
    parser.add_argument("argument", nargs="?", default="")
    parser.add_argument("--chat-id", type=int)
    parser.add_argument("--raw", action="store_true", help="保留 HTML 标签")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args(argv)

    config = get_config()
    setup_logging(config.settings.log_path, level="WARNING")
    news = NewsService(config)
    llm = LLMService(config)
    digest = DigestService(config, news, llm)
    search = SearchService(config, news, llm)
    render = (lambda t: t) if args.raw else strip_markup

    if args.mode == "digest":
        payload = await digest.generate(args.argument or "morning", chat_id=chat_id(config, args.chat_id),
                                        llm=llm)
        if payload.empty:
            print("（时间窗内没有达到门槛的新闻）")
            return 1
        for index, message in enumerate(payload.messages, 1):
            print(f"----- message {index}/{len(payload.messages)} -----")
            print(render(message))
            print()
        return 0

    if args.mode == "news":
        # 与 Bot 的 /news 走同一条路径（含按需中文翻译），预览才有意义
        items = await news.ensure_chinese(news.latest(limit=args.limit, hours=72))
        from app.services import format as fmt

        print(render(fmt.news_list(items, config=config, tz_name=config.settings.timezone,
                                   title="🤖 最新 AI 新闻", show_scores=True)))
        print(f"\n可点击按钮：{' '.join(f'{i + 1} -> /summary {a.id}' for i, a in enumerate(items))}")
        return 0

    if args.mode == "card":
        article_id = int(args.argument or 0) or (news.latest(limit=1)[0].id if news.latest(limit=1) else 0)
        item = news.by_id(article_id)
        if item is None:
            print(f"找不到 #{article_id}")
            return 1
        from app.services import format as fmt

        print(render(fmt.article_card(item, config=config, tz_name=config.settings.timezone)))
        print("\n[🧠 AI 深度分析] ->")
        print(render(await search.deep_summary(article_id) or "（分析失败）"))
        return 0

    if args.mode == "ask":
        question = args.argument or "今天有哪些值得关注的 AI 新闻？"
        answer = await search.answer(question, chat_id=chat_id(config, args.chat_id), llm=llm)
        print(f"intent={answer.intent} query={answer.query!r} used={len(answer.used_ids)}")
        print(render(answer.text))
        return 0

    if args.mode == "free":
        from app.bot.handlers.free import _collect, _live
        from app.services import format as fmt

        tool = args.argument or None
        # 和 bot 走同一条代码路径，否则这里绿、线上红
        items, note = _collect(news, days=args.days, tool=tool, keyword=None, config=config)
        live, checked = await _live(config, term=tool)
        print(render(fmt.clip(fmt.free_offer_list(items, config=config,
                                                  tz_name=config.settings.timezone,
                                                  days=args.days, tool=tool, live=live,
                                                  note=note, live_checked=checked,
                                                  unverified=bool(note)))))
        print("\n工具统计:", [(t["tool"], t["count"]) for t in news.free_offer_tools(days=args.days)])
        return 0

    print(strip_markup(__import__("app.services.format", fromlist=["x"]).status_line(news.stats())))
    for topic in news.topics():
        print(f"  {topic['emoji']} {topic['category']:<20} {topic['count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
