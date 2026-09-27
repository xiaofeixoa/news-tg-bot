"""Bot and dispatcher wiring (design doc sections 14, 24, 25)."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, CallbackQuery, ErrorEvent, Message, TelegramObject
from aiogram.utils.token import TokenValidationError

from app.bot.handlers import build_router
from app.bot.middleware import AccessMiddleware, LoggingMiddleware
from app.config import AppConfig, get_config
from app.logging_setup import get_logger
from app.services.digest import DigestService, get_digest_service
from app.services.llm import LLMService, get_llm
from app.services.news import NewsService, get_news_service
from app.services.search import SearchService, get_search_service

log = get_logger("telegram")

COMMANDS = [
    BotCommand(command="start", description="初始化 Bot"),
    BotCommand(command="news", description="最新 AI 新闻"),
    BotCommand(command="latest", description="最近 24 小时"),
    BotCommand(command="today", description="今日新闻"),
    BotCommand(command="yesterday", description="昨日新闻"),
    BotCommand(command="digest", description="立即生成简报"),
    BotCommand(command="search", description="搜索历史新闻"),
    BotCommand(command="summary", description="深度分析某条新闻"),
    BotCommand(command="topics", description="按分类查看"),
    BotCommand(command="free", description="近期哪些 agent/模型免费（/免费）"),
    BotCommand(command="sources", description="查看来源状态"),
    BotCommand(command="settings", description="推送设置"),
    BotCommand(command="setinterest", description="用自然语言设置兴趣"),
    BotCommand(command="pause", description="暂停自动推送"),
    BotCommand(command="resume", description="恢复自动推送"),
    BotCommand(command="help", description="帮助"),
]


from app.bot.errors import BotNotConfigured  # re-exported for callers


def _session_for(config: AppConfig):
    """aiogram session, wrapped in the proxy when TELEGRAM_PROXY is set."""
    proxy = config.settings.telegram_proxy.strip()
    if not proxy:
        return None
    from aiogram.client.session.aiohttp import AiohttpSession

    log.info("Telegram traffic goes through the configured proxy")
    return AiohttpSession(proxy=proxy)


def create_bot(config: AppConfig | None = None) -> Bot:
    config = config or get_config()
    token = config.settings.telegram_bot_token
    if not token:
        raise BotNotConfigured(
            "TELEGRAM_BOT_TOKEN 未设置：请在 .env 中填入 BotFather 颁发的 token"
        )
    try:
        return Bot(token=token,
                   session=_session_for(config),
                   default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    except TokenValidationError as exc:
        raise BotNotConfigured(f"TELEGRAM_BOT_TOKEN 格式无效: {exc}") from exc


async def on_error(event: ErrorEvent) -> bool:
    """A handler crash must neither take the polling loop down nor stay silent.

    The signature is part of the bug fix: aiogram 3.15 dispatches errors as a
    single `ErrorEvent`, so the previous `on_error(event, exception)` raised
    `TypeError: on_error() missing 1 required positional argument: 'exception'`
    the moment it was ever invoked - the safety net itself was dead, and unit
    tests that called it with two arguments could not see that.

    Before this, a crash also lived only in `logs/telegram.log`: an unanswered
    callback spun for a minute and the chat heard nothing. The reporting step has
    its own try/except, because a bot that cannot reply (blocked chat, flood
    wait, no bot context) must not turn one failure into an unhandled second one.
    """
    exception = event.exception
    update = event.update
    log.exception("unhandled error in Telegram handler: %s", exception)
    detail = str(exception).strip() or type(exception).__name__
    # `Update.effective_message` 不在 aiogram 3.15 的字段里（用了就 AttributeError，
    # 等于错误处理器第二次自己坏掉），所以按版本安全地取。
    target = update.callback_query or update.message or getattr(update, "effective_message", None)
    try:
        if isinstance(target, CallbackQuery):
            await target.answer(f"⚠️ 这一步失败了：{detail[:120]}")
        elif isinstance(target, Message):
            await target.answer(
                f"⚠️ 这条消息处理失败了：{type(exception).__name__} · {detail[:160]}\n"
                "已经记进日志，可以直接重发一次。")
    except Exception as exc:  # noqa: BLE001 - 报告失败不能把处理器自己带下水
        log.warning("could not report the handler error to Telegram: %s", exc)
    return True


def create_dispatcher(config: AppConfig | None = None, *,
                      news: NewsService | None = None,
                      search: SearchService | None = None,
                      digest: DigestService | None = None,
                      llm: LLMService | None = None) -> Dispatcher:
    config = config or get_config()
    dp = Dispatcher()
    llm = llm or get_llm()
    news = news or get_news_service()
    dp["app_config"] = config
    dp["news"] = news
    dp["llm"] = llm
    dp["search"] = search or SearchService(config, news, llm)
    dp["digest"] = digest or DigestService(config, news, llm)
    dp.message.outer_middleware(AccessMiddleware(config))
    dp.callback_query.outer_middleware(AccessMiddleware(config))
    dp.message.middleware(LoggingMiddleware())
    dp.callback_query.middleware(LoggingMiddleware())
    dp.include_router(build_router())
    dp.errors.register(on_error)
    return dp


async def register_commands(bot: Bot) -> None:
    try:
        await bot.set_my_commands(COMMANDS)
        log.info("registered %d bot commands", len(COMMANDS))
    except Exception as exc:  # non-fatal: commands are cosmetic
        log.warning("could not register commands: %s", exc)
