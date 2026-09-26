"""Application entry point.

    python -m app.main              # bot + scheduler (what systemd runs)
    python -m app.main --once       # one collect + process round, no Telegram
    python -m app.main --no-bot     # scheduler only (e.g. bot runs elsewhere)
    python -m app.main --self-check # config / db / source health report

Exit codes: 0 clean, 1 crash, 2 startup failure, 3 configuration error.
Code 3 is declared non-retryable in the systemd unit so a missing bot token
stops the service once with a readable message instead of restarting forever.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from typing import Any

from app.config import AppConfig, get_config
from app.bot.errors import BotNotConfigured
from app.database.database import init_db
from app.logging_setup import get_logger, setup_logging
from app.processing import breaking
from app.scheduler.jobs import NewsJobs, begin_shutdown, create_scheduler, shutdown

log = get_logger("app")


def bootstrap(config: AppConfig | None = None) -> AppConfig:
    config = config or get_config()
    setup_logging(
        config.settings.log_path,
        level=config.get("logging.level", config.settings.log_level),
        max_bytes=int(config.get("logging.max_bytes", 5_242_880)),
        backup_count=int(config.get("logging.backup_count", 5)),
    )
    try:
        init_db()
    except OSError as exc:
        print(f"启动失败：无法写入数据库 ({exc})。"
              f"检查目录权限：sudo chown -R <service-user> {config.settings.data_path}",
              file=sys.stderr)
        raise
    return config


async def self_check(config: AppConfig) -> int:
    from app.collectors import build_collectors, known_types
    from app.services.llm import LLMService

    problems: list[str] = []
    if not config.settings.telegram_bot_token:
        problems.append("TELEGRAM_BOT_TOKEN 未设置")
    if not config.settings.chat_id_whitelist:
        problems.append("ALLOWED_CHAT_IDS 为空 —— Bot 将拒绝所有用户（这是有意为之的安全默认值）")
    if not config.settings.llm_configured:
        problems.append("LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 未设置 —— 将使用规则模式")
    collectors = build_collectors(config)
    if not collectors:
        problems.append("没有启用任何数据源")
    print("AI News Radar 自检")
    print("-" * 46)
    print(f"环境           : {config.settings.app_env}")
    print(f"数据库         : {config.settings.sqlalchemy_url}")
    print(f"时区           : {config.settings.timezone}")
    print(f"日志           : {config.settings.log_path}")
    print(f"Collector 类型 : {', '.join(known_types())}")
    print(f"启用数据源     : {len(collectors)} / {len(config.sources)}")
    for collector in collectors:
        print(f"  · {collector.source_name:<28} {config.source_type_label(collector.type)}")
    llm = LLMService(config)
    print(f"LLM            : {'可用 ' + config.settings.llm_model if llm.enabled else '不可用（规则模式）'}")
    print(f"早报/晚报      : {config.settings.daily_digest_time} / {config.settings.evening_digest_time}")
    print(f"突发           : {breaking.describe(config, ai_enabled=llm.enabled)}"
          + ("" if not config.settings.breaking_news_enabled else
             f" · 每天最多 {config.settings.max_breaking_news_per_day} 条"
             f" · 间隔 {config.settings.breaking_cooldown_minutes} 分钟"))
    print("-" * 46)
    if problems:
        print("需要注意：")
        for problem in problems:
            print(f"  ! {problem}")
        return 1
    print("一切就绪。")
    return 0


async def run_once(config: AppConfig, *, types: list[str] | None = None,
                   process: bool = True) -> dict[str, Any]:
    jobs = NewsJobs(config)
    result: dict[str, Any] = {}
    result["collect"] = await jobs.run_collect(types)
    if process:
        result["process"] = await jobs.run_process()
    return result



async def _seed_free_vocabulary(config: AppConfig) -> None:
    """Ask the pricing gateway once at boot so the /免费 detector knows the
    model names that exist right now before it rescans the archive."""
    from app.services.free_models import get_free_model_watcher

    watcher = get_free_model_watcher(config)
    if not watcher.enabled:
        return
    try:
        await asyncio.wait_for(watcher.snapshot(), timeout=25)
    except Exception as exc:  # noqa: BLE001 - an offline gateway must not stall boot
        log.info("free-model vocabulary not seeded at boot: %s", exc)


def _backfill_free_offers(config: AppConfig) -> None:
    """Tag existing rows with the current /免费 vocabulary once at boot.

    Cheap and token-free, so a vocabulary edit takes effect on old news too.
    """
    from app.database.database import session_scope
    from app.processing.pipeline import (backfill_free_offers, repair_lost_units,
                                         repair_template_titles, repair_truncated_summaries)

    try:
        with session_scope() as session:
            backfill_free_offers(session, config=config)
            repair_template_titles(session)
            repaired = repair_truncated_summaries(session)
            lost_units = repair_lost_units(session)
            from app.processing import enrich
            stale = enrich.reset_stale_translations(session, config=config)
            session.commit()
        if repaired:
            log.info("re-cut %d truncated summary line(s)", repaired)
        if lost_units:
            log.info("requeued %d Chinese line(s) that dropped a guarded unit", lost_units)
        if stale:
            log.info("dropped %d stale Chinese summary line(s) for re-translation", stale)
    except Exception as exc:  # noqa: BLE001 - never block startup over this
        log.warning("boot repair pass skipped: %s", exc)


async def run_forever(config: AppConfig, *, with_bot: bool = True) -> int:
    jobs = NewsJobs(config)
    sender: "TelegramSender | None" = None
    bot = None
    dispatcher = None
    if with_bot:
        # Imported here, not at the top of the function: aiogram costs ~100MB of
        # heap, and a --no-bot box (collector only) has no reason to pay for it.
        from app.bot.bot import create_bot, create_dispatcher, register_commands
        from app.bot.sender import TelegramSender

        bot = create_bot(config)
        sender = TelegramSender(bot, config)
        jobs.attach_sender(sender)
        dispatcher = create_dispatcher(config)

    scheduler = create_scheduler(jobs, config)
    scheduler.start()
    await _seed_free_vocabulary(config)
    _backfill_free_offers(config)
    await jobs.startup_report()

    stop = asyncio.Event()

    def _request_stop(*args: Any) -> None:
        log.info("shutdown requested")
        begin_shutdown()  # quiet the in-flight jobs we are about to cancel
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:  # pragma: no cover - Windows has no add_signal_handler
            # Setting an Event from a raw signal handler is not guaranteed to wake
            # the loop, so hand the wake-up back to the loop itself.
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(_request_stop))

    tasks: list[asyncio.Task] = []
    if dispatcher is not None and bot is not None:
        await register_commands(bot)
        tasks.append(asyncio.create_task(
            dispatcher.start_polling(bot), name="telegram-polling",
        ))
    else:
        log.info("Telegram bot disabled (--no-bot); scheduler only")

    stop_task = asyncio.create_task(stop.wait(), name="stop-signal")
    done, pending = await asyncio.wait(
        [*[t for t in tasks if t], stop_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        if task is not stop_task and task.exception():
            log.error("telegram polling stopped: %s", task.exception())

    await shutdown(jobs, scheduler, sender)
    if bot is not None:
        await bot.session.close()
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ai-news-radar", description="个人 AI 新闻 Agent")
    parser.add_argument("--once", action="store_true", help="采集并处理一轮后退出（不启动 Bot）")
    parser.add_argument("--no-bot", action="store_true", help="只运行采集与调度，不启动 Telegram")
    parser.add_argument("--collect", action="store_true", help="只采集（配合 --once）")
    parser.add_argument("--types", default="", help="限定 collector 类型，逗号分隔，如 rss,hackernews")
    parser.add_argument("--self-check", dest="self_check", action="store_true", help="检查配置与数据源")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = bootstrap()
    except Exception as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 2
    types = [t.strip() for t in args.types.split(",") if t.strip()] or None
    try:
        if args.self_check:
            return asyncio.run(self_check(config))
        if args.once:
            stats = asyncio.run(run_once(config, types=types, process=not args.collect))
            for label, value in stats.items():
                print(f"{label}: {value}")
            return 0
        return asyncio.run(run_forever(config, with_bot=not args.no_bot))
    except KeyboardInterrupt:  # pragma: no cover
        log.info("interrupted")
        return 0
    except BotNotConfigured as exc:
        # A missing token is an operator action item, not a crash: one readable
        # line, and an exit status systemd is told not to retry.
        print(f"配置错误：{exc}", file=sys.stderr)
        log.error("refusing to start: %s", exc)
        return 3
    except Exception as exc:
        log.exception("fatal: %s", exc)
        print(f"致命错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
