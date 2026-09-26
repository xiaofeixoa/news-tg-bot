"""End-to-end Telegram check against the live Bot API.

    python scripts/telegram_smoke.py                 # getMe + one test message per chat
    python scripts/telegram_smoke.py --text "hello"
    python scripts/telegram_smoke.py --digest        # send the real morning briefing
    python scripts/telegram_smoke.py --free          # send the real /免费 answer
    python scripts/telegram_smoke.py --free --query glm
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiogram.exceptions import TelegramAPIError  # noqa: E402

from app.bot.sender import TelegramSender  # noqa: E402
from app.config import get_config  # noqa: E402
from app.logging_setup import setup_logging  # noqa: E402
from app.services.digest import DigestService  # noqa: E402
from app.services.llm import LLMService  # noqa: E402
from app.services.news import NewsService  # noqa: E402


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", default="✅ AI News Radar 部署成功，Bot 已经可以推送新闻了。")
    parser.add_argument("--digest", action="store_true", help="发送真实早报而不是测试文本")
    parser.add_argument("--free", action="store_true", help="发送 /免费 的真实结果")
    parser.add_argument("--query", default="", help="--free 的参数，例如 glm / 7 / qoder 30")
    parser.add_argument("--chat", type=int, help="只发给这一个 chat id")
    args = parser.parse_args(argv)

    config = get_config()
    setup_logging(config.settings.log_path, level=config.settings.log_level)
    chats = [args.chat] if args.chat else sorted(config.settings.chat_id_whitelist)
    if not chats:
        print("ALLOWED_CHAT_IDS 为空：先在 .env 里填上你的 chat id（给 @userinfobot 发一条消息即可拿到）")
        return 2

    try:
        sender = TelegramSender.from_token(config=config)
    except Exception as exc:  # noqa: BLE001
        print(f"Bot token 无效：{exc}")
        return 2

    bot = sender.bot
    try:
        me = await bot.get_me()
        print(f"token ok: @{me.username} (id={me.id})")
    except TelegramAPIError as exc:
        print(f"Telegram 连接失败：{exc}")
        return 2

    news = NewsService(config)
    llm = LLMService(config)
    digest = DigestService(config, news, llm)
    failures = 0
    for chat_id in chats:
        if args.free:
            # 走 /免费 的真实代码路径：同一个渲染函数 + 同一套按钮
            from app.bot.handlers.free import _collect, _live, _parse_args
            from app.bot.keyboards import inline as K
            from app.services import format as fmt

            days, tool, keyword = _parse_args(args.query, config)
            items, note = _collect(news, days=days, tool=tool, keyword=keyword, config=config)
            live, checked = await _live(config, term=keyword or tool)
            text = fmt.clip(fmt.free_offer_list(items, config=config,
                                                tz_name=config.settings.timezone,
                                                days=days, tool=tool, live=live, note=note,
                                                live_checked=checked, unverified=bool(note)))
            sent = await bot.send_message(chat_id, text, parse_mode="HTML",
                                          reply_markup=K.free_keyboard(
                                              news.free_offer_tools(days=days), days=days))
            print(f"chat {chat_id}: /免费 已送达（{len(items)} 条资讯，"
                  f"{'含实时免费模型' if live else '无实时块'}，{len(sent.text)} 字）")
            failures += 0 if sent else 1
            continue
        if args.digest:
            payload = await digest.generate("morning", chat_id=chat_id, llm=llm)
            if payload.empty or not payload.messages:
                print(f"chat {chat_id}: 暂无可推送的简报（先运行 python -m app.main --once 采集）")
                continue
            sent = await sender.send_digest(chat_id, payload)
            if sent:
                digest.record_delivery(chat_id=chat_id, digest=payload)
            print(f"chat {chat_id}: 已发送 {sent}/{len(payload.messages)} 条简报消息")
            failures += 0 if sent else 1
        else:
            ok = await sender.send(chat_id, f"<b>smoke test</b>\n\n{config.settings.app_env}\n\n{args.text}")
            print(f"chat {chat_id}: {'已送达，去 Telegram 看' if ok else '失败'}")
            failures += 0 if ok else 1
    await sender.close()
    await llm.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
