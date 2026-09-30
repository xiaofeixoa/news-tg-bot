"""启动路径：他第一次撞到、出事最难复现的那 24%。

`app/main.py` 之前 24% —— 里面既决定 systemd 怎么起这个进程，也决定
`--self-check` 告诉他什么，还决定 SIGTERM 之后走不走 `shutdown()`。
这些分支坏掉的形态是"服务看起来是活的"，不是报错。
"""

from __future__ import annotations

import asyncio

import pytest

import app.main as entry
from app.config import get_config


@pytest.fixture
def routing(monkeypatch):
    """把真正的网络/调度都换成记录器，只留路由与退出码。"""
    calls: dict[str, object] = {}

    async def fake_once(config, *, types=None, process=True):
        calls["once"] = {"types": types, "process": process}
        return {"collect": {"fetched": 3}, "process": {"processed": 1}}

    async def fake_forever(config, *, with_bot=True):
        calls["forever"] = {"with_bot": with_bot}
        return 0

    monkeypatch.setattr(entry, "run_once", fake_once)
    monkeypatch.setattr(entry, "run_forever", fake_forever)
    return calls


def test_bare_invocation_boots_the_daemon_with_the_bot(routing, capsys):
    assert entry.main([]) == 0
    assert routing["forever"] == {"with_bot": True}
    assert "提示" not in capsys.readouterr().out


def test_no_bot_stays_scheduler_only(routing):
    assert entry.main(["--no-bot"]) == 0
    assert routing["forever"] == {"with_bot": False}


def test_once_is_a_one_shot_and_prints_the_round_stats(routing, capsys):
    assert entry.main(["--once"]) == 0
    assert routing["once"] == {"types": None, "process": True}
    out = capsys.readouterr().out
    assert "collect:" in out and "process:" in out
    assert "提示" not in out, "已经写了 --once 就不该再被教育一次"


def test_collect_without_once_no_longer_boots_the_daemon(routing, capsys):
    """旧行为：`--collect` 被解析完就丢掉，进程直接变成常驻服务（还带 Bot 轮询）。"""
    assert entry.main(["--collect"]) == 0
    assert "once" in routing and "forever" not in routing
    assert routing["once"] == {"types": None, "process": False}
    assert "--once" in capsys.readouterr().out


def test_types_without_once_is_also_a_one_shot(routing, capsys):
    assert entry.main(["--types", "rss,hackernews"]) == 0
    assert "forever" not in routing
    assert routing["once"]["types"] == ["rss", "hackernews"]
    assert routing["once"]["process"] is True
    assert "提示" in capsys.readouterr().out


def test_a_broken_config_exits_2_and_says_启动失败(monkeypatch, capsys):
    def explode():
        raise RuntimeError("settings.yaml 读不到")

    monkeypatch.setattr(entry, "bootstrap", explode)
    assert entry.main(["--once"]) == 2
    assert "启动失败" in capsys.readouterr().err


def test_an_unwritable_database_gets_a_chinese_hint_about_permissions(monkeypatch, capsys):
    """权限问题的下一步动作是 chown，别让运维去猜。"""
    monkeypatch.setattr(entry, "setup_logging", lambda *a, **k: None)
    init_db_calls = {"n": 0}

    def fail():
        init_db_calls["n"] += 1
        raise OSError(13, "permission denied")

    monkeypatch.setattr(entry, "init_db", fail)
    with pytest.raises(OSError):
        entry.bootstrap(get_config())
    err = capsys.readouterr().err
    assert "无法写入数据库" in err and "chown" in err
    assert init_db_calls["n"] == 1


def test_missing_bot_token_is_exit_3_because_systemd_must_not_retry(monkeypatch):
    from app.bot.errors import BotNotConfigured

    async def refuse(config, *, with_bot=True):
        raise BotNotConfigured("TELEGRAM_BOT_TOKEN 未设置")

    monkeypatch.setattr(entry, "run_forever", refuse)
    assert entry.main([]) == 3


def test_any_other_crash_is_exit_1(monkeypatch):
    async def boom(config, *, with_bot=True):
        raise RuntimeError("polling exploded")

    monkeypatch.setattr(entry, "run_forever", boom)
    monkeypatch.setattr(entry.log, "exception", lambda *a, **k: None)
    assert entry.main([]) == 1


@pytest.mark.asyncio
async def test_self_check_reports_the_mode_that_is_actually_running(capsys, monkeypatch):
    monkeypatch.setattr(entry, "init_db", lambda: None)
    code = await entry.self_check(get_config())
    out = capsys.readouterr().out
    assert code == 1, "测试环境没有 bot token，自检必须报需要注意，而不是说一切就绪"
    assert "需要注意" in out and "TELEGRAM_BOT_TOKEN" in out
    assert "规则模式" in out, "没 key 的机器要说清现在是规则模式"
    assert "启用数据源" in out and "突发" in out
    assert "Traceback" not in out


@pytest.mark.asyncio
async def test_self_check_with_a_token_and_sources_is_not_a_failure(capsys, monkeypatch):
    config = get_config()
    monkeypatch.setattr(entry, "init_db", lambda: None)
    # 白名单和 llm_configured 都是派生字段，要设它们背后的那几个值
    monkeypatch.setattr(config.settings, "telegram_bot_token", "123:abc")
    monkeypatch.setattr(config.settings, "allowed_chat_ids", "111111111")
    monkeypatch.setattr(config.settings, "llm_base_url", "https://example.org/v1")
    monkeypatch.setattr(config.settings, "llm_api_key", "sk-test")
    monkeypatch.setattr(config.settings, "llm_model", "some-model")
    # 测试会话一开始就自己建了库，所以"本次新建"标记在整套测试里都是真话；
    # 这条用例问的是"一切配好了吗"，替它把那个状态钉成普通重启。
    monkeypatch.setattr("app.database.database.database_is_fresh", lambda *a: False)
    code = await entry.self_check(config)
    assert code == 0
    assert "一切就绪" in capsys.readouterr().out


# ------------------------------------------------------- 关停路径（#5 守卫）
class Scheduler:
    def start(self) -> None:
        return None

    def shutdown(self, wait=False) -> None:
        return None


def _stub_boot(monkeypatch, fake_wait):
    """把 run_forever 里所有真会动外部世界的东西换成记录器。"""
    async def no_seed(config):
        return None

    async def quiet_report(self):
        return None

    async def quiet_shutdown(jobs, scheduler, sender):
        return None

    async def shim(tasks, **kwargs):
        # 真被 wait 叫醒之前，那些任务不会被我们取消；测试里把它们交给 fake_wait
        # 当"已完成"返回，就得先取消，否则事件循环收尾时会刷
        # "Task was destroyed but it is pending"，把真正的警告埋掉。
        for task in tasks:
            task.cancel()
        return await fake_wait(list(tasks), **kwargs)

    monkeypatch.setattr(entry.asyncio, "wait", shim)
    monkeypatch.setattr(entry, "shutdown", quiet_shutdown)
    monkeypatch.setattr(entry, "create_scheduler", lambda jobs, config: Scheduler())
    monkeypatch.setattr(entry, "_seed_free_vocabulary", no_seed)
    monkeypatch.setattr(entry, "_backfill_free_offers", lambda config: None)
    monkeypatch.setattr(entry.NewsJobs, "startup_report", quiet_report)
    monkeypatch.setattr(entry.NewsJobs, "attach_sender", lambda self, sender: None)
    return quiet_shutdown


@pytest.mark.asyncio
async def test_a_cancelled_task_in_done_does_not_skip_the_shutdown_path(monkeypatch):
    """`task.exception()` 对被取消的任务是**抛出** CancelledError，而它不是 Exception：
    旧代码会把下面的 shutdown() 整个跳过（调度器没关、发送会话没关），
    而 main() 的 `except Exception` 也接不住它 —— 表现是"服务重启后像半死"。
    """
    shut = {"done": False}

    async def fake_wait(tasks, **kwargs):
        async def victim():
            await asyncio.sleep(5)

        cancelled = asyncio.create_task(victim())
        await asyncio.sleep(0)
        cancelled.cancel()
        try:
            await cancelled
        except asyncio.CancelledError:
            pass
        return {cancelled} | set(tasks), set()

    async def mark_shutdown(jobs, scheduler, sender):
        shut["done"] = True

    _stub_boot(monkeypatch, fake_wait)
    monkeypatch.setattr(entry, "shutdown", mark_shutdown)
    assert await entry.run_forever(get_config(), with_bot=False) == 0
    assert shut["done"] is True, "关不掉的东西下次启动才会以半死状态回来"


@pytest.mark.asyncio
async def test_a_failed_polling_task_is_still_logged(monkeypatch):
    """守卫不能把真正的报错一起咽掉。"""
    records: list[str] = []

    class Recorder:
        def info(self, msg, *args):
            records.append(str(msg % args if args else msg))

        warning = error = exception = info

    async def dying():
        await asyncio.sleep(0)
        raise RuntimeError("polling died")

    task = asyncio.create_task(dying())
    try:
        await task
    except RuntimeError:
        pass

    async def fake_wait(tasks, **kwargs):
        return {task} | set(tasks), set()

    monkeypatch.setattr(entry, "log", Recorder())
    _stub_boot(monkeypatch, fake_wait)
    assert await entry.run_forever(get_config(), with_bot=False) == 0
    assert any("polling died" in m for m in records), records


# ---------------------------------------- 数据库："我打开的是刚建的空库"必须说出来
class _Rec:
    """`news.db` 这个 logger 不向 root 传播，caplog 看不见它——直接换掉 log 对象。"""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str]] = []

    def _add(self, level: str):
        def emit(msg, *a, **k):
            self.rows.append((level, str(msg) % a if a else str(msg)))
        return emit

    def __getattr__(self, name: str):
        if name in ("info", "warning", "error", "debug"):
            return self._add(name.upper())
        raise AttributeError(name)


@pytest.fixture
def boot_url(tmp_path) -> str:
    return f"sqlite:///{(tmp_path / 'boot.db').as_posix()}"


def test_a_database_created_by_this_boot_is_announced(boot_url, monkeypatch):
    """09-27 那台机器被快照回滚清空后，启动日志和 46 次正常启动一模一样。"""
    from app.database import database as db

    rec = _Rec()
    monkeypatch.setattr(db, "log", rec)
    db.init_db(boot_url)
    warned = [m for lvl, m in rec.rows if lvl == "WARNING"]
    assert warned, rec.rows
    assert "新建" in warned[0] and "boot.db" in warned[0], warned
    assert db.database_is_fresh(boot_url)


def test_the_next_process_over_the_same_file_only_says_ready(boot_url, monkeypatch):
    from app.database import database as db

    db.init_db(boot_url)                    # 第一个进程：库真的是它建的
    db._fresh_files.discard(boot_url)       # 重启后的新进程再打开同一个文件
    rec = _Rec()
    monkeypatch.setattr(db, "log", rec)
    db.init_db(boot_url)
    assert [m for lvl, m in rec.rows if lvl == "WARNING"] == [], rec.rows
    assert any("database ready" in m for _, m in rec.rows), rec.rows
    assert db.database_size_kb(boot_url) > 0, "文件就在盘上，自检要给得出大小"


def test_a_dsn_carrying_two_values_fails_before_it_builds_anything(tmp_path, boot_url):
    """`grep KEY= env | cut -d= -f2-` 撞上重复键会得到两行；SQLite 会照建不误。"""
    from app.database import database as db

    mangled = boot_url + f"\nsqlite:///{(tmp_path / 'other.db').as_posix()}"
    with pytest.raises(ValueError) as exc:
        db.get_engine(mangled)
    assert "两次" in str(exc.value), exc.value
    assert [p.name for p in tmp_path.iterdir()] == [], "报错之前不该留下任何文件/目录"


def test_bootstrap_turns_a_mangled_url_into_a_readable_startup_failure(monkeypatch, capsys):
    monkeypatch.setattr(entry, "setup_logging", lambda *a, **k: None)

    def fail():
        raise ValueError("DATABASE_URL 看起来被拼在了一起：同一个键没有被定义两次")

    monkeypatch.setattr(entry, "init_db", fail)
    with pytest.raises(ValueError):
        entry.bootstrap(get_config())
    err = capsys.readouterr().err
    assert "拼在了一起" in err and "两次" in err, err


@pytest.mark.asyncio
async def test_self_check_names_a_brand_new_database(capsys, monkeypatch):
    monkeypatch.setattr(entry, "init_db", lambda: None)
    monkeypatch.setattr("app.database.database.database_is_fresh", lambda *a: True)
    code = await entry.self_check(get_config())
    out = capsys.readouterr().out
    assert code == 1
    assert "本次新建（空库）" in out, out
    assert "0 条新闻" in out and "DATABASE_URL" in out, out


@pytest.mark.asyncio
async def test_the_online_line_carries_the_new_database_marker(monkeypatch):
    """`AI News Radar online` 是唯一每轮都要 grep 的那一行，空库要写在它上面。"""
    from app.scheduler import jobs as jobs_mod
    from app.scheduler.jobs import NewsJobs
    from app.services.news import NewsService

    monkeypatch.setattr(NewsService, "stats", lambda self: {
        "total_articles": 0, "sources": 20, "sources_configured": 37,
        "sources_delivering": 0, "llm_enabled": False})
    monkeypatch.setattr(jobs_mod, "database_is_fresh", lambda *a: True)
    lines: list[str] = []
    monkeypatch.setattr(jobs_mod.log, "info",
                        lambda *a, **k: lines.append(str(a[0]) % a[1:] if a else str(a[0])))
    await NewsJobs(get_config()).startup_report()
    assert any("[db=new" in m and "0 article(s)" in m for m in lines), lines


@pytest.mark.asyncio
async def test_a_normal_boot_does_not_shout_about_a_new_database(monkeypatch):
    from app.scheduler import jobs as jobs_mod
    from app.scheduler.jobs import NewsJobs
    from app.services.news import NewsService

    monkeypatch.setattr(NewsService, "stats", lambda self: {
        "total_articles": 1765, "sources": 20, "sources_configured": 37,
        "sources_delivering": 18, "llm_enabled": False})
    monkeypatch.setattr(jobs_mod, "database_is_fresh", lambda *a: False)
    lines: list[str] = []
    monkeypatch.setattr(jobs_mod.log, "info",
                        lambda *a, **k: lines.append(str(a[0]) % a[1:] if a else str(a[0])))
    await NewsJobs(get_config()).startup_report()
    assert lines and "[db=new" not in lines[0], lines
