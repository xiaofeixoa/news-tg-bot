"""这个订阅者的简报到底有没有真的发出去 - 一条命令问完。

2026-09-26 早上 08:00 那份早报没发出去过。事后想查的时候，库里只会说"今天没有
morning 这一行"，而这一行分不清到底是**还没到点**、**被订阅者自己的设置挡住**、
还是**发了一半没记账**（v1.28 修的就是最后这一种）。所以把三样东西并排摆出来：
账本 `push_logs`、订阅者自己的设置行、以及发送层 `logs/telegram.log` 里与这个
chat 有关的行。

只读：不写库、不发消息、不重启任何东西。

    python scripts/delivery_report.py                      # 所有订阅者
    python scripts/delivery_report.py --kind morning       # 只看早报
    python scripts/delivery_report.py --hours 168          # 放宽到一周
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import AppConfig, get_config                    # noqa: E402
from app.database import repository as repo                     # noqa: E402
from app.database.database import session_scope                  # noqa: E402
from app.database.models import PushLog, User                   # noqa: E402

UTC = timezone.utc
KINDS = {"morning": ("daily_enabled", "daily_time"), "evening": ("evening_enabled", "evening_time")}
# 账本与所有 DB 时间都是 naive UTC，日志行是北京时间：差 8 小时这件事必须写在脸上。
HEADER_TIME_NOTE = "时间口径：日志行=北京时间，账本 created_at=naive UTC"


def _zone(name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name or "UTC")
    except Exception:
        return UTC


def to_local(value: datetime, tz_name: str) -> datetime:
    """naive-UTC 的账本时间 → 订阅者自己的墙上时间。"""
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return aware.astimezone(_zone(tz_name))


def log_tails(path: Path | None, chat_id: int, *, limit: int = 6) -> list[str]:
    """这个 chat 在发送层日志里的最后几行（时间戳是北京时间）。"""
    if path is None or not path.exists():
        return []
    needle = f"chat_id={chat_id}"
    return [line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if needle in line][-limit:]


def verdict(*, user: User, kind: str, row: PushLog | None, tz_name: str,
            now_local: datetime) -> str:
    """"该不该发"与"账上有没有"并成一句话。不说调度器当时是怎么决定的——
    那是 `logs/scheduler.log` 的活；这里只负责让他一眼看出要往哪边查。
    """
    flag, slot_key = KINDS[kind]
    slot = str(getattr(user, slot_key) or "")
    if user.paused:
        return f"⏸ 没发：订阅者按过 /pause（自动推送暂停中）· 计划 {slot}"
    if not getattr(user, flag, True):
        return f"🔕 没发：这一份被订阅者自己在 /设置 里关掉 · 计划 {slot}"
    if row is None:
        return f"❓ 库里从来没有 {kind} 的账本行（计划 {slot}）"
    sent_local = to_local(row.created_at, tz_name)
    if sent_local.date() == now_local.date():
        return f"✅ 今天已记账：本地 {sent_local:%m-%d %H:%M:%S}（UTC {row.created_at}）"
    if now_local.strftime("%H:%M") >= slot:
        return (f"⚠️ 已过计划时间而今天没有账本行：计划 {slot}，"
                f"上一份是本地 {sent_local:%m-%d %H:%M:%S}")
    return f"⏳ 今天还没到点（计划 {slot}）· 上一份是本地 {sent_local:%m-%d %H:%M:%S}"


def render(*, config: AppConfig, hours: int = 48,
           kinds: Iterable[str] = ("morning", "evening"), log_dir: Path | None = None,
           now: datetime | None = None) -> list[str]:
    now = now or datetime.now(UTC).replace(tzinfo=None)
    since = now - timedelta(hours=hours)
    kinds = tuple(kinds)
    log_path = (log_dir / "telegram.log") if log_dir else None
    out = [f"投递审计 · 窗口 {hours} 小时（UTC {since} → {now}）",
           f"看的种类：{'、'.join(kinds)}" + (f" · 日志目录 {log_dir}" if log_dir else " · 未读日志"),
           HEADER_TIME_NOTE]
    with session_scope() as session:
        users = list(session.query(User).order_by(User.telegram_chat_id))
        if not users:
            return out + ["库里没有任何订阅者行"]
        for user in users:
            tz_name = user.timezone or config.settings.timezone
            now_local = to_local(now, tz_name)
            out += ["", f"订阅者 chat_id={user.telegram_chat_id} · 时区 {tz_name} · "
                        f"本地现在 {now_local:%m-%d %H:%M}"]
            for kind in kinds:
                count = repo.pushes_since(session, user=user, kind=kind, since=since)
                out.append(f"  {kind:<8} 窗口内 {count} 次 · " + verdict(
                    user=user, kind=kind, row=repo.last_push_of(session, user=user, kind=kind),
                    tz_name=tz_name, now_local=now_local))
            breaking = repo.last_push_of(session, user=user, kind="breaking")
            if breaking is not None:
                out.append(f"  breaking 最后一次 本地 {to_local(breaking.created_at, tz_name):%m-%d %H:%M:%S}")
            tails = log_tails(log_path, user.telegram_chat_id)
            if tails:
                out.append(f"  发送层日志（这个 chat 的最后 {len(tails)} 行，北京时间）：")
                out += [f"    {line}" for line in tails]
            elif log_path is not None:
                out.append(f"  发送层日志里没有这个 chat 的行（{log_path}）；"
                           f"成功投递的日志从 v1.31 才有，更早的窗口只能靠账本那一条腿")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="订阅者简报投递与账本的对账（只读）")
    parser.add_argument("--hours", type=int, default=48)
    parser.add_argument("--kind", choices=sorted(KINDS), default=None)
    parser.add_argument("--log-dir", default=None, help="默认用配置里的日志目录")
    args = parser.parse_args(argv)

    config = get_config()
    log_dir = Path(args.log_dir) if args.log_dir else config.settings.log_path
    for line in render(config=config, hours=args.hours,
                       kinds=(args.kind,) if args.kind else tuple(KINDS), log_dir=log_dir):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
