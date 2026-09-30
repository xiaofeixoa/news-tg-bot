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


def test_preview_card_mode_says_the_deep_analysis_is_unavailable(monkeypatch, capsys):
    """没配 LLM 时预览不该再打一遍同样的卡片，还把它标成"AI 深度分析"。"""
    import runpy
    import sys
    from datetime import datetime, timezone

    from app.database import repository as repo
    from app.database.database import session_scope
    from app.processing.normalize import build_article

    with session_scope() as s:
        art = repo.save_article(s, build_article(
            title="OpenAI releases a preview-only model", url="https://openai.com/card-preview",
            source_name="OpenAI", content="OpenAI releases a preview-only model for agents.",
            published_at=datetime.now(timezone.utc).replace(tzinfo=None)))
        article_id = art.id if art else 0
    assert article_id, "没建出行，这条用例就没有意义"

    monkeypatch.setattr(sys, "argv", ["preview.py", "card", str(article_id)])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path("scripts/preview.py", run_name="__main__")
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "不可用：未配置" in out, out
    assert out.count("一句话总结") <= 1, "深度分析不可用时不该把卡片再打印一遍"


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


def test_the_deploy_payload_carries_everything_a_fresh_box_needs():
    """快照回滚 / 换机时，`deploy.sh` 就是唯一的供给路径。

    2026-09-27 重建 192.168.8.99 时清单里少了 `deploy/`：依赖装完、库 init 完，
    才发现没有 systemd 单元可装 —— 而这台机器连不上 github，除了这个 tar 别无来源。
    所以"能不能重建一台"必须是一条测试，不是一次运气。
    """
    script = (PROJECT_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    lines = [ln for ln in script.splitlines() if ln.strip().startswith("tar czf ")]
    assert len(lines) == 1, lines
    payload = lines[0].split()
    required = {"app", "config", "scripts", "tests", "requirements.txt", "docs",
                "deploy", "README.md"}
    assert required <= set(payload), f"部署包少了：{sorted(required - set(payload))}"
    # 排除的必须是机器自己的状态，别把库或 venv 打包过去
    assert not any(t in payload for t in (".venv", "data", "logs", ".env")), payload
    for name in ("ai-news-radar.service", "ai-news-override-no-bot.conf"):
        assert (PROJECT_ROOT / "deploy" / name).is_file(), name
