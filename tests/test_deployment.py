"""Deployment-shape tests: things that only broke once we ran on a real server."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UNIT = PROJECT_ROOT / "deploy" / "ai-news-radar.service"


def test_unwritable_log_files_degrade_to_console_instead_of_killing_the_service(tmp_path, monkeypatch):
    """Seen in production: logs/ was re-chowned during an update, every
    RotatingFileHandler open raised, and the bot exited at startup while
    journald - the real log destination under systemd - was perfectly usable."""
    import logging.handlers

    import app.logging_setup as ls

    def refuse(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(ls, "_file_handler", refuse)

    ls.setup_logging(tmp_path / "logs", level="INFO")  # must not raise

    root = logging.getLogger()
    assert any(isinstance(h, logging.StreamHandler) for h in root.handlers)
    assert not any(isinstance(h, logging.handlers.BaseRotatingHandler) for h in root.handlers)


def test_writable_log_dir_still_creates_per_subsystem_files(tmp_path):
    from app.logging_setup import SUBSYSTEMS, setup_logging

    target = tmp_path / "logs"
    setup_logging(target, level="INFO")
    try:
        logging.getLogger("news").info("hello")
        for handler in logging.getLogger("news").handlers:
            handler.flush()
        written = {p.name for p in target.glob("*.log")}
        assert written == {f"{name}.log" for name in SUBSYSTEMS}
    finally:
        for logger_name in ("", *[f"news.{s}" for s in SUBSYSTEMS]):
            for handler in list(logging.getLogger(logger_name).handlers):
                handler.close()
                logging.getLogger(logger_name).removeHandler(handler)


def test_systemd_unit_keys_live_in_the_right_sections():
    """systemd silently ignores misplaced keys, which is how a start-limit ended
    up doing nothing."""
    text = UNIT.read_text(encoding="utf-8")
    assert "\r\n" not in text, "unit file must be LF for systemd"
    unit_section = text.split("[Service]")[0]
    service_section = text.split("[Service]")[1].split("[Install]")[0]

    for key in ("StartLimitIntervalSec", "StartLimitBurst"):
        assert f"\n{key}=" in unit_section, f"{key} belongs in [Unit]"
        assert f"{key}=" not in service_section
    assert "Restart=always" in service_section
    assert "RestartPreventExitStatus=3" in service_section, "config errors must not crash-loop"
    assert "EnvironmentFile=/etc/ai-news-radar/env" in service_section
    assert "WantedBy=multi-user.target" in text


def test_missing_bot_token_is_a_clean_exit_3(monkeypatch, capsys):
    """Fail closed, say why in one line, and give systemd a non-retryable code."""
    import asyncio

    import app.main as main
    from app.bot.bot import BotNotConfigured

    monkeypatch.setattr(main, "bootstrap", lambda *a, **k: None)
    async def refuse(*args, **kwargs):
        raise BotNotConfigured("TELEGRAM_BOT_TOKEN 未设置")

    monkeypatch.setattr(main, "run_forever", refuse)
    assert main.main([]) == 3
    err = capsys.readouterr().err
    assert "TELEGRAM_BOT_TOKEN" in err and "Traceback" not in err


def test_requirements_cover_every_runtime_import():
    """The venv on the server is built from requirements.txt alone."""
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "-c",
         "import app.main, app.collectors, app.bot.bot, app.services.search;"
         "print('import-ok')"],
        capture_output=True, text=True, cwd=PROJECT_ROOT,
    )
    assert "import-ok" in out.stdout, out.stderr[-400:]
    declared = {
        line.split("==")[0].split("[")[0].strip().lower()
        for line in (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    }
    for package in ("aiogram", "httpx", "feedparser", "beautifulsoup4", "sqlalchemy",
                    "apscheduler", "pydantic", "pydantic-settings", "pyyaml", "tenacity"):
        assert package in declared, f"{package} used at runtime but not pinned"


def test_preview_script_free_mode_runs_the_real_path(tmp_path, monkeypatch, capsys):
    """`preview.py free` must not drift from the handler it mirrors.

    It once kept calling the old single-value `_live()` signature and only blew
    up on the server, which is exactly the failure this test exists to catch.
    """
    import runpy
    import sys

    from app.services import free_models as fm

    async def no_source(self, *args, **kwargs):
        raise RuntimeError("offline test")

    monkeypatch.setattr(fm.FreeModelWatcher, "_fetch", no_source)
    monkeypatch.setattr(sys, "argv", ["preview.py", "free", "--days", "30"])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path("scripts/preview.py", run_name="__main__")
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "近期免费" in out


def test_collection_mode_does_not_import_aiogram(tmp_path):
    """aiogram costs ~100MB of heap; only the bot process should pay for it.

    Runs in a subprocess because the rest of the suite legitimately imports it.
    """
    import subprocess
    import sys
    from pathlib import Path

    probe = tmp_path / "probe.py"
    probe.write_text(
        "import sys\n"
        "import app.main\n"
        "print('aiogram' in sys.modules)\n",
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run([sys.executable, str(probe)], cwd=root,
                            capture_output=True, text=True,
                            env={**os.environ, "PYTHONPATH": str(root)})
    assert result.returncode == 0, result.stderr[-500:]
    assert result.stdout.strip().endswith("False"), \
        "importing app.main must not pull aiogram into collection-only runs"


def test_bot_not_configured_is_importable_without_the_sdk():
    from app.bot.errors import BotNotConfigured

    assert issubclass(BotNotConfigured, RuntimeError)
    import app.main  # noqa: F401  - the exit-3 path depends on this class

    from app.bot.bot import BotNotConfigured as from_bot

    assert from_bot is BotNotConfigured
