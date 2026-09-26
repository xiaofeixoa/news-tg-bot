"""Probe every configured news source.

    python scripts/test_sources.py                  # fetch each enabled source
    python scripts/test_sources.py --only rss       # one type only
    python scripts/test_sources.py --name OpenAI    # one source by name
    python scripts/test_sources.py --all            # include disabled sources
    python scripts/test_sources.py --youtube https://www.youtube.com/@TwoMinutePapers
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.collectors import build_collectors, close_client  # noqa: E402
from app.config import get_config  # noqa: E402

CHANNEL_ID_RE = re.compile(r"channelId[\"']?\s*[:=]\s*[\"'](UC[A-Za-z0-9_-]{20,})")


def _fmt(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.1f}s"


async def probe(collector, verbose: int) -> tuple[bool, str, list[str]]:
    started = datetime.now(timezone.utc)
    try:
        items = await collector.collect()
    except Exception as exc:  # noqa: BLE001 - this script exists to surface errors
        taken = (datetime.now(timezone.utc) - started).total_seconds()
        return False, f"{_fmt(taken)} {type(exc).__name__}: {str(exc)[:120]}", []
    taken = (datetime.now(timezone.utc) - started).total_seconds()
    titles = [f"{i.get('published_at'):%m-%d %H:%M} {str(i.get('title'))[:78]}" for i in items[: max(0, verbose)]]
    return True, f"{_fmt(taken)} {len(items)} item(s)", titles


async def resolve_youtube(handle_url: str) -> None:
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0"}) as client:
        response = await client.get(handle_url)
        match = CHANNEL_ID_RE.search(response.text)
    if not match:
        print(f"no channelId found at {handle_url} (YouTube renders it only for canonical pages)")
        return
    print(f"# add to config/sources.yaml:\n#   - name: Some Channel\n#     type: youtube\n"
          f"#     enabled: true\n#     channels:\n#       - id: {match.group(1)}\n#         name: ...")


async def run(types: list[str] | None, names: list[str] | None, all_sources: bool,
              verbose: int) -> int:
    config = get_config()
    collectors = build_collectors(config, types=types or None, names=names or None,
                                 only_enabled=not all_sources)
    if not collectors:
        print("没有匹配的数据源。检查 config/sources.yaml 与 --only/--name 参数。")
        return 2
    print(f"probing {len(collectors)} source(s)\n")
    failures = 0
    for collector in collectors:
        ok, detail, titles = await probe(collector, verbose)
        mark = "OK  " if ok else "FAIL"
        failures += 0 if ok else 1
        print(f"[{mark}] {collector.source_name:<30} {detail}")
        for title in titles:
            print(f"        · {title}")
    await close_client()
    print(f"\n{len(collectors) - failures} ok, {failures} failed. "
          f"失败的数据源不会影响运行，只会在 logs/collector.log 里留痕。")
    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", action="append", help="限定类型，可重复：rss,hackernews,github,reddit,arxiv,youtube")
    parser.add_argument("--name", action="append", help="按名称测试，可重复")
    parser.add_argument("--all", action="store_true", help="包含 enabled: false 的源")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="打印抓取到的标题")
    parser.add_argument("--youtube", help="从频道主页解析 channelId")
    args = parser.parse_args(argv)

    if args.youtube:
        return asyncio.run(_async_main(resolve=argparse.Namespace(youtube=args.youtube)))
    types = [t.strip() for spec in (args.only or []) for t in spec.split(",") if t.strip()] or None
    return asyncio.run(run(types, args.name, args.all, args.verbose))


async def _async_main(resolve: argparse.Namespace) -> int:
    await resolve_youtube(resolve.youtube)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
