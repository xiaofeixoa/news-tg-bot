"""Logging: rotating files per subsystem, with secret redaction.

design doc section 25 - logs/app.log, collector.log, telegram.log, llm.log
"""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

SUBSYSTEMS = ("app", "collector", "telegram", "llm", "db", "scheduler")

# Never let a token leak into a log file.
_PATTERNS = [
    (re.compile(r"(sk-[A-Za-z0-9_\-]{6,})"), r"sk-***"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.=]{6,}", re.I), r"\1***"),
    (re.compile(r'("?(?:api_key|authorization|x-api-key|bot_token)"?\s*[:=]\s*")([^"]{4,})(")', re.I), r"\1***\3"),
    (re.compile(r"(TELEGRAM_BOT_TOKEN|LLM_API_KEY|GITHUB_TOKEN)(\s*[=:]\s*)(\S+)"), r"\1\2***"),
    (re.compile(r"(\d{8,}):\d{2}:[A-Za-z0-9_\-]{20,}"), r"<bot-token-redacted>"),
]


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        for pattern, repl in _PATTERNS:
            message = pattern.sub(repl, message)
        return message


def _file_handler(log_dir: Path, name: str, level: int, max_bytes: int, backups: int) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        log_dir / f"{name}.log", maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
    )
    handler.setLevel(level)
    handler.setFormatter(
        RedactingFormatter("%(asctime)s %(levelname)-7s [%(name)s] %(filename)s:%(lineno)d - %(message)s")
    )
    return handler


def _safe_file_handler(log_dir: Path, name: str, level: int, max_bytes: int, backups: int):
    """A log file we cannot open must not take the service down.

    Seen in production: logs/ was re-chowned to root during an update, every
    RotatingFileHandler open failed, and the whole bot exited at startup while
    journald - the actual log destination under systemd - was perfectly fine.
    """
    try:
        return _file_handler(log_dir, name, level, max_bytes, backups)
    except OSError as exc:
        print(f"WARNING: cannot write {log_dir / (name + '.log')} ({exc}); "
              f"continuing with journald/stdout only. Fix with: "
              f"sudo chown -R <service-user> {log_dir}", file=sys.stderr)
        return None


def setup_logging(log_dir: Path | str, level: str = "INFO", max_bytes: int = 5_242_880, backup_count: int = 5) -> None:
    """Attach one rotating file per subsystem; console output is left to
    journald when running under systemd. Unwritable files degrade to console."""
    log_dir = Path(log_dir)
    numeric = getattr(logging, str(level).upper(), logging.INFO)
    problems: list[str] = []

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        problems.append(f"cannot create {log_dir}: {exc}")

    root = logging.getLogger()
    root.setLevel(min(numeric, logging.INFO))
    for existing in list(root.handlers):
        existing.close()
        root.removeHandler(existing)
    handler = None if problems else _safe_file_handler(log_dir, "app", numeric, max_bytes, backup_count)
    if handler:
        root.addHandler(handler)
    root.addHandler(logging.StreamHandler())  # journald / docker logs

    for subsystem in SUBSYSTEMS:
        if subsystem == "app":
            continue
        logger = logging.getLogger(f"news.{subsystem}")
        logger.propagate = False
        logger.setLevel(numeric)
        for existing in list(logger.handlers):
            existing.close()
            logger.removeHandler(existing)
        if not problems:
            sub_handler = _safe_file_handler(log_dir, subsystem, numeric, max_bytes, backup_count)
            if sub_handler:
                logger.addHandler(sub_handler)

    for chatty in ("httpx", "httpcore", "aiosqlite", "asyncio", "apscheduler.executors.default"):
        logging.getLogger(chatty).setLevel(max(numeric, logging.WARNING))

    if problems:
        print(f"WARNING: logging to files disabled ({problems[0]}); "
              f"using journald/stdout only", file=sys.stderr)


def get_logger(subsystem: str = "app") -> logging.Logger:
    return logging.getLogger("news" if subsystem == "app" else f"news.{subsystem}")


_WARNED_ONCE: set[str] = set()


def warn_once(logger: logging.Logger, key: str, message: str, *args,
              level: int = logging.INFO) -> bool:
    """说一句就够了：重复成立的状态不该每轮重播。

    线上实测（`scripts/log_incidents.py`，2026-09-26..30）：一句"没配 GITHUB_TOKEN"打了
    115 行，一句"源服务器要求降速"打了 246 行——都是每轮都成立的同一件事。真正的异常
    就埋在这种噪音里。新进程会说一次，那是有用的"重启之后问题还在"信号。
    """
    if key in _WARNED_ONCE:
        logger.debug(message, *args)
        return False
    _WARNED_ONCE.add(key)
    logger.log(level, message, *args)
    return True


def reset_warned_once() -> None:
    """测试用：清掉"已经说过"的记账。"""
    _WARNED_ONCE.clear()
