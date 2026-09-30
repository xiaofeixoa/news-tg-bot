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
NOTE = "时间口径：日志与下面的时刻都是北京时间（服务用 TZ=Asia/Shanghai 写日志）"
# 由低到高：`--level WARNING` 的意思是"WARNING 及以上"，所以顺序本身就是语义。
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "FATAL")


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
           now: datetime | None = None) -> str:
    lines = [NOTE, "目录：%s · 命中 %d 类" % (logs_dir, len(groups))]
    if not groups:
        lines.append("✅ 一条 WARNING/ERROR 都没有。")
        return "\n".join(lines)
    cutoff = (now or datetime.now()) - timedelta(hours=since_hours) if since_hours else None
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
    parser.add_argument("--since", type=float, default=None, metavar="小时",
                        help="只看最近 N 小时内还出现过的类别")
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
    print(render(groups, logs_dir=logs_dir, since_hours=args.since))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
