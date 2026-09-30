"""日志里的错误到底最后一次发生在什么时候 - 一条命令问完。

我每轮部署后都用 `grep -c Traceback logs/*.log` 做"没有新增异常"的核对，但这个数字
有个致命弱点：**它只会变大，永远不会变小**，而且分不清 2026-09-26 的一次事故与刚才这一轮。
线上实测就是这样被误读的：`scheduler.log` 连着几天显示 4 条 Traceback，我一直写"历史遗留"，
把每个 traceback 块按它上面最近的时间戳归位之后才发现，那 4 块全都来自 **同一个时刻**
2026-09-26 10:20:56 的那次 `article_tags` 唯一键事故（早已修好），之后一次都没再出现。

所以这里按"事件"而不是按"行数"来报：同一类错误归一组，给出首次/末次时刻与次数。

只读：只打开日志文件，不碰数据库、不发消息。

    python scripts/log_incidents.py                    # 所有 WARNING/ERROR 分组
    python scripts/log_incidents.py --since 6          # 只看最近 6 小时还在发生的
    python scripts/log_incidents.py --level ERROR      # 只看错误
    python scripts/log_incidents.py --logs logs        # 指定目录（默认取配置里的 LOG_DIR）

`--since` 的窗口按**日志自己的钟**算（`settings.timezone`，也就是 systemd 的 `TZ=`；
两者由 tests/test_log_incidents.py 钉住同源）。用调用者的墙钟是量不出同一个答案的：
2026-10-01 在真机上，同一批日志、同一句 `--since 6`，进程钟为 UTC 时列出 21 类、
最早 12.6 小时前，而按北京时间只有 15 类、最早 5.25 小时前。反方向更糟——新出现的错误
会被滤掉，然后打印一句假的"✅ 最近 6 小时没有新记录"。
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

HEADER = re.compile(r"^(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s+"
                    r"(?P<level>[A-Z]+)\s+\[(?P<logger>[^\]]+)\]\s+(?P<where>[^\s]+) - (?P<msg>.*)$")
EXCEPTION = re.compile(r"^(?:[A-Za-z_][\w.]*\.)?([A-Za-z_][\w.]*(?:Error|Exception))\s*:?\s*(.*)$")
# 时间戳、十六进制地址、数字、URL、被引号包住的长短语：这些都是同一条错误的变体。
NOISE = (re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[,.]?\d*"),
         re.compile(r"0x[0-9a-fA-F]+"),
         re.compile(r"https?://\S+"),
         re.compile(r"\b\d+\b"))
NOTE = "时间口径：日志与下面的时刻都是日志时区的墙上时间（服务用 TZ=Asia/Shanghai 写日志）"
# 由低到高：`--level WARNING` 的意思是"WARNING 及以上"，所以顺序本身就是语义。
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "FATAL")


def _hours(text: str) -> float:
    """`--since` 的错也要说中文：我自己在真机上就把 `--since 21:02` 喂给它过。

    argparse 的默认报错是 `invalid float value: '21:02'`——它没说"这里要的是小时数"，
    而这正是敲命令的人不知道的事。
    """
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            "--since 要的是小时数（例如 --since 6 表示最近 6 小时），不是时刻") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("--since 必须是大于 0 的小时数")
    return value


def _log_zone_now(tz_name: str | None, *, at: datetime | None = None) -> datetime:
    """日志时区的此刻（naive，好和日志里解析出的时间戳同一种形态比较）。

    这里不能用 `datetime.now()`：那一行的比较对象是调用者的钟。2026-10-01 在真机量过，
    同一批日志、同一句 `--since 6`，北京时间下跑列出 15 类（最早 5.25 小时前，符合），
    把进程钟换成 UTC 就变成 21 类、最早 12.6 小时前——问"最近 6 小时"答出 12.6 小时。
    反方向（调用者的钟比日志快 8 小时，例如日志是 UTC 而人在北京）更糟：新出现的错误会被
    悄悄滤掉，然后打印"✅ 最近 6 小时内没有任何新记录"——那是一句假的太平报告，
    而我每次部署后引用正是这句话。
    """
    from app.config import local_now

    if at is None:
        return local_now(tz_name).replace(tzinfo=None)
    if at.tzinfo is None:
        return at                      # 测试注入的就是"日志时区的此刻"
    from zoneinfo import ZoneInfo
    return at.astimezone(ZoneInfo(tz_name or "Asia/Shanghai")).replace(tzinfo=None)


def _signature(text: str) -> str:
    out = text.strip()
    for pattern in NOISE:
        out = pattern.sub("N", out)
    return out[:150]


def _exception_of(block: list[str]) -> str:
    """最后一行异常才是根因：开头那行常常是包装后的 `PendingRollbackError`。"""
    found = ""
    for line in block:
        stripped = line.strip()
        if not stripped or stripped.startswith(("Traceback", "File ", "  ", "+", "|")):
            continue
        match = EXCEPTION.match(stripped)
        if match:
            found = match.group(1)
    return found


def parse_logs(paths: Iterable[Path], *, min_level: str = "WARNING") -> dict[str, dict]:
    """Group records into `{class: {count, first, last, sample, where}}` by signature."""
    floor = LEVELS.index(min_level) if min_level in LEVELS else LEVELS.index("WARNING")
    groups: dict[str, dict] = defaultdict(lambda: {"count": 0, "first": None, "last": None,
                                                    "samples": set()})
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        current: tuple[datetime, str, str, str, list[str]] | None = None
        for line in lines:
            header = HEADER.match(line)
            if header:
                level = header.group("level")
                if current and LEVELS.index(current[1]) >= floor:
                    _absorb(groups, current)
                current = None
                if level in LEVELS and LEVELS.index(level) >= floor:
                    try:
                        stamp = datetime.strptime(header.group("stamp"), "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        continue
                    current = (stamp, level, header.group("logger"),
                               "%s %s" % (header.group("where"), header.group("msg")), [])
                continue
            if current:
                current[4].append(line)
        if current and LEVELS.index(current[1]) >= floor:
            _absorb(groups, current)
    return groups


def _absorb(groups: dict[str, dict], record) -> None:
    stamp, level, logger, message, block = record
    cause = _exception_of(block)
    key = "%s %s %s" % (level, logger, _signature(cause or message))
    if cause and cause not in message:
        key += " ← " + cause
    entry = groups[key]
    entry["count"] += 1
    entry["first"] = min(entry["first"] or stamp, stamp)
    entry["last"] = max(entry["last"] or stamp, stamp)
    entry["samples"].add(message[:160])


def render(groups: dict[str, dict], *, logs_dir: Path, since_hours: float | None = None,
           now: datetime | None = None, tz_name: str = "Asia/Shanghai") -> str:
    if not since_hours:
        header = "目录：%s · 命中 %d 类" % (logs_dir, len(groups))
        cutoff = None
    else:
        current = _log_zone_now(tz_name, at=now)
        cutoff = current - timedelta(hours=since_hours)
        shown = sum(1 for entry in groups.values() if entry["last"] >= cutoff)
        # 两个数一起报：以前只有"命中 99 类"，而下面实际列出的是过滤后的 15 类
        header = ("目录：%s · 日志里共 %d 类，最近 %g 小时内还在出现的 %d 类"
                  "（窗口按 %s 的 %s 起算）"
                  % (logs_dir, len(groups), since_hours, shown, tz_name,
                     current.strftime("%m-%d %H:%M")))
    lines = [NOTE, header]
    if not groups:
        lines.append("✅ 一条 WARNING/ERROR 都没有。")
        return "\n".join(lines)
    shown = 0
    for key, entry in sorted(groups.items(), key=lambda kv: kv[1]["last"], reverse=True):
        if cutoff and entry["last"] < cutoff:
            continue
        shown += 1
        lines.append("\n▸ %s" % key)
        lines.append("  次数 %d · 首次 %s · 最近 %s"
                     % (entry["count"], entry["first"].strftime("%m-%d %H:%M:%S"),
                        entry["last"].strftime("%m-%d %H:%M:%S")))
        for sample in sorted(entry["samples"])[:3]:
            lines.append("  · %s" % sample)
    if cutoff and shown == 0:
        lines.append("\n✅ 最近 %.1f 小时内没有任何新的这一类记录（下面列的都是历史）。" % since_hours)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    # 这台机器的控制台可能是 GBK：一份报告不该因为一个 `▸` 崩掉。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--logs", default=None, help="日志目录（默认取配置的 LOG_DIR）")
    parser.add_argument("--level", default="WARNING", choices=LEVELS[1:], help="最低级别")
    parser.add_argument("--since", type=_hours, default=None, metavar="小时",
                        help="只看最近 N 小时内还出现过的类别（填小时数，不是时刻）")
    parser.add_argument("--all", action="store_true", help="连 INFO 一起统计（很吵）")
    args = parser.parse_args(argv)

    from app.config import get_config

    config = get_config()
    logs_dir = Path(args.logs or config.settings.log_dir)
    if not logs_dir.is_dir():
        print("找不到日志目录：%s" % logs_dir)
        return 1
    min_level = "INFO" if args.all else args.level
    paths = sorted(logs_dir.glob("*.log"))
    groups = parse_logs(paths, min_level=min_level)
    print(render(groups, logs_dir=logs_dir, since_hours=args.since,
                 tz_name=config.settings.timezone))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
