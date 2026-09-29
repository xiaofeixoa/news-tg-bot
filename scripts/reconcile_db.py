#!/usr/bin/env python3
"""一次性账目核对：把历史上写错的、缺失的决定行补齐（默认只演练）。

    .venv/bin/python scripts/reconcile_db.py            # 干跑，只报数
    .venv/bin/python scripts/reconcile_db.py --apply   # 真的写库

四件事，每一件都对应一次线上量出来的问题：
  regate     词表加了词之后，过去被门槛丢掉的行重新问一遍（改一次词表就该重跑一次）
  spam       GitHub Trending 里 `(0 stars)` 的新仓库：星数为 0 说明"上榜"这个信号本身
             不成立，标成 filtered_out，别再占简报与 /最新
  matters    为什么值得关注：早于该功能入库的行永远没有这一行，按现在的事实重算
  processed  is_processed=True 但 processed_at 为空的行：没有时刻就没法判断"上次
             决定是什么时候做的"；用入库时刻兜底，并在 meta 里明说是补的
  ghost      测试期留下的订阅者行（chat 111111111，0 条推送账本）
  lost       修复之前被时间类门禁挡掉、又没留下重试标记的突发：补不回来了，就在行上
             写清"为什么没有补发"，否则下一个 Agent 只会看见 is_sent=False

写库前请先备份（`sqlite3` 的 backup API，几分钟就够）。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import or_, select  # noqa: E402
from app.config import get_config  # noqa: E402
from app.database import repository as repo  # noqa: E402
from app.database.database import init_db, session_scope  # noqa: E402
from app.database.models import Article, PushLog, User  # noqa: E402
from app.processing import classifier, breaking, summarizer  # noqa: E402

PHANTOM_CHAT = 111111111          # 测试用的假 chat，不是他
REGATE_DAYS = 7


def _note(row: Article, key: str, value: str) -> None:
    row.meta = {**(row.meta or {}), key: value}


def regate(session, cfg, *, apply: bool) -> dict[str, int]:
    """重新过一遍门槛：新词表能救回的行放回去，由正常处理轮打分、翻译。"""
    since = datetime.utcnow() - timedelta(days=REGATE_DAYS)
    dropped = list(session.scalars(select(Article).where(
        Article.filtered_out.is_(True), Article.is_archived.is_(False),
        Article.published_at >= since)))
    back = 0
    for row in dropped:
        keep, hits = classifier.rule_filter(
            {"title": row.title or "", "content": (row.content or "")[:800],
             "source_name": row.source_name or ""}, cfg)
        if not keep:
            continue
        back += 1
        if apply:
            row.filtered_out = False
            row.is_processed = False
            row.processed_at = None
            row.process_attempts = 0
            _note(row, "regated", {"at": datetime.utcnow().isoformat(timespec="seconds"),
                                   "hits": hits[:5]})
    return {"七天内被丢的行": len(dropped), "重新放行": back}


def _stars_of(row: Article) -> int | None:
    """`meta.stars` 可能是 0，而 `0 or -1` 会把它读成 -1 —— 这里必须区分"0 星"与"没有这个键"。"""
    raw = (row.meta or {}).get("stars")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def spam(session, cfg, *, apply: bool) -> dict[str, int]:
    """`(0 stars)` 的 Trending 行：上榜信号不成立，标掉。"""
    rows = list(session.scalars(select(Article).where(
        Article.source_name.like("%Trending%"), Article.filtered_out.is_(False))))
    zero = [r for r in rows if _stars_of(r) == 0]
    if apply:
        for row in zero:
            row.filtered_out = True
            row.is_processed = True
            _note(row, "spam_dropped", "GitHub Trending 抓到 0 星新仓库，不构成新闻信号")
    return {"Trending 未过滤行": len(rows), "标为 filtered_out": len(zero)}


def matters(session, cfg, *, apply: bool, overwrite: bool = False) -> dict[str, int]:
    """按现在的事实重算"为什么值得关注"，只写有内容的。

    默认只补空行。`overwrite` 会连本工具自己写过的覆盖度句一起重算——修了
    "行数当来源数"之后就得靠它把先前写错的 61 行刷正，而 AI 模式写的句子不在此列。
    """
    blank = Article.why_it_matters.is_(None)
    mine = Article.why_it_matters.like("同一事件另有%")
    rows = list(session.scalars(select(Article).where(
        Article.is_processed.is_(True), Article.is_archived.is_(False),
        Article.filtered_out.is_(False), blank if not overwrite else or_(blank, mine))))
    filled = 0
    for row in rows:
        event = repo.event_for(session, row.event_id) if row.event_id else None
        text = (summarizer.compose_why_it_matters(row, event, config=cfg) or "").strip()
        if not text:
            continue
        filled += 1
        if apply:
            row.why_it_matters = text[:280]
    return {"缺这一行的可见行": len(rows), "重算后有内容": filled}


def processed(session, cfg, *, apply: bool) -> dict[str, int]:
    """processed_at 用入库时刻兜底，并在 meta 里说清是补的。"""
    rows = list(session.scalars(select(Article).where(
        Article.is_processed.is_(True), Article.processed_at.is_(None))))
    if apply:
        for row in rows:
            row.processed_at = row.created_at or datetime.utcnow()
            _note(row, "processed_at_backfilled", "用入库时刻补齐，非真实处理时刻")
    return {"processed_at 为空": len(rows), "补上": len(rows) if apply else 0}


def ghost(session, cfg, *, apply: bool) -> dict[str, int]:
    """删掉测试留下的假订阅者；它有账本就别删（那会丢历史）。"""
    users = list(session.scalars(select(User).where(User.telegram_chat_id == PHANTOM_CHAT)))
    deleted = 0
    for user in users:
        logs = list(session.scalars(select(PushLog).where(PushLog.user_id == user.id)))
        if logs:
            print(f"   跳过 chat={PHANTOM_CHAT}：它还有 {len(logs)} 条推送账本")
            continue
        deleted += 1
        if apply:
            session.delete(user)
    return {"假订阅者行数": len(users), "删除": deleted}


def lost(session, cfg, *, apply: bool) -> dict[str, int]:
    """给"修好之前丢掉、又补不回来"的突发写下原因，别让它只是一个 is_sent=False。"""
    rows = list(session.scalars(select(Article).where(
        Article.is_sent.is_(False), Article.meta.isnot(None))))
    marked = 0
    for row in rows:
        meta = row.meta or {}
        if not meta.get("breaking_reason") or meta.get("breaking_defer") or meta.get("breaking_lost"):
            continue
        ok, why = breaking.gate(row, config=cfg, ai_enabled=cfg.ai_enabled)
        if ok:
            continue                      # 还过门禁的交给正常补发，不在这里下结论
        marked += 1
        print(f"   #{row.id} 门禁现在不接受：{why}")
        if apply:
            _note(row, "breaking_lost", f"补发前已被挡下，现门禁理由：{why}")
    return {"待查的突发候选": len(rows), "写下原因": marked}


STEPS = [("regate", regate), ("spam", spam), ("matters", matters),
         ("processed", processed), ("ghost", ghost), ("lost", lost)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量核对历史账目（默认干跑）")
    parser.add_argument("--apply", action="store_true", help="真的写库；缺省只统计")
    parser.add_argument("--only", default="", help="只跑某几步，逗号分隔：regate,spam,…")
    parser.add_argument("--overwrite", action="store_true",
                    help="matters 连本工具写过的覆盖度句一起重算（修正旧口径）")
    args = parser.parse_args(argv)
    wanted = {s.strip() for s in args.only.split(",") if s.strip()}
    cfg = get_config()
    init_db()
    print(f"模式：{'写库' if args.apply else '干跑'} | 库：{cfg.settings.database_url}")
    for name, step in STEPS:
        if wanted and name not in wanted:
            continue
        with session_scope() as session:
            kwargs = {"apply": args.apply}
            if name == "matters":
                kwargs["overwrite"] = args.overwrite
            counts = step(session, cfg, **kwargs)
            if args.apply:
                session.commit()
        print(f"[{name}] " + "  ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
