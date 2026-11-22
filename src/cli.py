"""wick CLI: the human side of approvals and retries.

The D8 page (`python src/cli.py panel`) is the pretty path; this CLI is the
scriptable one (and the only one that works over SSH). Commands:

Usage:
    python src/cli.py status
    python src/cli.py approvals [--all]
    python src/cli.py approve <id>
    python src/cli.py reject <id>
    python src/cli.py retry <event_id>
    python src/cli.py schedule <ISO 时间> <做什么>
    python src/cli.py panel [--port 9119]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402


def connect(args):
    """Open the DB from CLI args.

    Passing args.db=None to db.connect() would override its DEFAULT_DB default and
    crash on Path(None), which is exactly what happens when the flag is omitted.
    """
    return db.connect(args.db or db.DEFAULT_DB)


def _fmt_row(r) -> str:
    detail = json.loads(r["detail"] or "{}")
    target = detail.get("repo", "?")
    number = detail.get("number", "?")
    title = (detail.get("title") or "")[:60]
    err = detail.get("execution_error")
    line = f"  #{r['id']:<3} [{r['status']:<8}] {r['action']:<6} {target}#{number} {title}"
    if r["executed_at"]:
        line += f"  (已执行 {r['executed_at']}"
        line += f", 失败: {err})" if err else ")"
    return line


def cmd_status(args) -> int:
    conn = connect(args)
    counts = {
        "events": conn.execute("SELECT status, COUNT(*) c FROM events GROUP BY status").fetchall(),
        "deliveries": conn.execute("SELECT status, COUNT(*) c FROM deliveries GROUP BY status").fetchall(),
        "approvals": conn.execute("SELECT status, COUNT(*) c FROM approvals GROUP BY status").fetchall(),
    }
    print(f"库: {args.db or db.DEFAULT_DB}\n")
    for table, rows in counts.items():
        if not rows:
            print(f"{table:<12} (空)")
            continue
        parts = ", ".join(f"{r['status']}={r['c']}" for r in rows)
        print(f"{table:<12} {parts}")
    conn.close()
    return 0


def cmd_approvals(args) -> int:
    conn = connect(args)
    sql = "SELECT * FROM approvals"
    if not args.all:
        sql += " WHERE status IN ('waiting','approved') AND executed_at IS NULL"
    rows = conn.execute(sql + " ORDER BY id").fetchall()
    if not rows:
        print("没有待批准的项。" if not args.all else "approvals 表是空的。")
    else:
        print("待批准（批准后 worker 会执行）：" if not args.all else "全部审批记录：")
        for r in rows:
            print(_fmt_row(r))
        if not args.all:
            print(f"\n批准: python src/cli.py approve <id>   |   拒绝: python src/cli.py reject <id>")
    conn.close()
    return 0


def _decide(args, approved: bool) -> int:
    conn = connect(args)
    try:
        row = conn.execute("SELECT * FROM approvals WHERE id=?", (args.id,)).fetchone()
        if row is None:
            print(f"审批 #{args.id} 不存在", file=sys.stderr)
            return 1
        if row["status"] != "waiting":
            print(f"审批 #{args.id} 已经是 {row['status']}，不再改动", file=sys.stderr)
            return 1
        db.decide_approval(conn, args.id, approved)
        verb = "已批准" if approved else "已拒绝"
        print(f"{verb} #{args.id}：{row['action']}")
        if approved:
            print("worker 下一轮会执行它（若 worker 没在跑，启动 `python src/worker.py --repo cmyyy/wick`）")
        return 0
    finally:
        conn.close()


def cmd_approve(args) -> int:
    return _decide(args, True)


def cmd_reject(args) -> int:
    return _decide(args, False)


def cmd_retry(args) -> int:
    """Put a finished event back in the queue. The only retry the system has.

    done/failed are terminal: claim_next only ever picks `pending`, and dedupe
    means the same issue will not be enqueued twice. Without this command a
    second run on a task is impossible by hand as well as by machine, so the
    progress file a run writes could never be read by a later run.

    attempts is kept: it is the attempt counter, not a retry counter, and the
    next claim makes it N+1 -- which is also what keeps the old attempt's late
    writes fenced off.
    """
    conn = connect(args)
    try:
        row = conn.execute("SELECT id, status, attempts FROM events WHERE id=?",
                           (args.id,)).fetchone()
        if row is None:
            print(f"事件 #{args.id} 不存在", file=sys.stderr)
            return 1
        if row["status"] not in ("done", "failed"):
            print(f"事件 #{args.id} 当前是 {row['status']}，只有 done/failed 能 retry",
                  file=sys.stderr)
            return 1
        cur = conn.execute(
            "UPDATE events SET status='pending', worker_id=NULL, heartbeat_at=NULL, "
            "updated_at=? WHERE id=? AND status IN ('done','failed')",
            (db.now(), args.id),
        )
        conn.commit()
        if cur.rowcount != 1:
            print(f"事件 #{args.id} 状态刚刚变了，没有重排", file=sys.stderr)
            return 1
        print(f"已重排事件 #{args.id}（attempts={row['attempts']} 保留，"
              f"下次领取是 attempt {row['attempts'] + 1}）")
        return 0
    finally:
        conn.close()


def cmd_schedule(args) -> int:
    """Queue a followup by hand, the same way the agent queues its own (#14)."""
    conn = connect(args)
    try:
        eid, created = db.schedule_followup(conn, args.when, args.what)
        verb = f"已排 followup #{eid}" if created else f"已存在 followup #{eid}（dedupe 命中，未重复入队）"
        print(f"{verb}：{args.when} {args.what}")
        return 0
    except ValueError as exc:
        print(f"排不上：{exc}（时间要 ISO 8601，且必须在将来）", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_panel(args) -> int:
    """Serve the D8 page (src/server.py). Blocks until Ctrl+C."""
    import server
    return server.serve(args.port, args.db or db.DEFAULT_DB)


# ---------------------------------------------------------------- selftest

def _selftest() -> int:
    """Self-check: status counts, pending list, and approve once-and-only-once."""
    import io
    import tempfile
    from contextlib import redirect_stdout

    # ignore_cleanup_errors: on Windows the sqlite WAL/SHM files of a just-closed
    # connection can still be held for a moment, which made cleanup raise after
    # every assertion had already passed.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        path = Path(td) / "wick.db"
        conn = db.connect(path)
        eid, _ = db.enqueue(conn, "github_issue", "cmyyy/wick#1", {"title": "outbox 节流算错"})
        assert eid is not None
        # #10 fenced every write a lease owns: claim first, and ask for the approval
        # while the event is still running (the fence refuses a finished event).
        claimed = db.claim_next(conn, "cli-selftest")
        assert claimed is not None and claimed["id"] == eid
        aid = db.request_approval(conn, eid, "提 PR",
                                  {"repo": "cmyyy/wick", "number": 1, "title": "outbox 节流算错"},
                                  worker_id="cli-selftest", attempts=claimed["attempts"])
        assert aid is not None, "running 状态下应该能插审批"
        db.finish_event(conn, eid, "done", "cli-selftest", claimed["attempts"])
        db.add_delivery(conn, eid, "已处理")
        conn.close()

        def run(fn, **kw) -> tuple[int, str]:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = fn(argparse.Namespace(db=str(path), **kw))
            return rc, buf.getvalue()

        rc, out = run(cmd_status)
        assert rc == 0 and "events" in out and "deliveries" in out, out
        assert "queued=1" in out, out

        rc, out = run(cmd_approvals, all=False)
        assert rc == 0 and f"#{aid}" in out and "cmyyy/wick#1" in out, out

        rc, out = run(cmd_approve, id=aid)
        assert rc == 0 and "已批准" in out, out
        rc, out = run(cmd_approve, id=aid)  # already decided: refused, no traceback
        assert rc == 1, out
        rc, out = run(cmd_approve, id=aid + 99)
        assert rc == 1, out

        # Omitting --db (args.db is None) must fall back to db.DEFAULT_DB.
        # db.connect(None) would override the default and crash on Path(None).
        original_default = db.DEFAULT_DB
        db.DEFAULT_DB = Path(td) / "fallback.db"
        try:
            with redirect_stdout(io.StringIO()):
                rc = cmd_status(argparse.Namespace(db=None))
            assert rc == 0, "args.db=None 应该回退到 db.DEFAULT_DB，不是崩"
            assert (Path(td) / "fallback.db").exists(), "回退后的库文件没被创建"
        finally:
            db.DEFAULT_DB = original_default

        # P0-1: retry is the only path that gives a task a second run (done and
        # failed are terminal, and dedupe keeps the issue from being enqueued
        # again). done -> pending, attempts kept, and claim_next must take it.
        rc, out = run(cmd_retry, id=eid)
        assert rc == 0 and "已重排" in out, out
        conn = db.connect(path)
        again = db.claim_next(conn, "cli-selftest-2")
        assert again is not None and again["id"] == eid, dict(again or {})
        assert again["attempts"] == claimed["attempts"] + 1, dict(again)
        conn.close()
        # Only a finished event can be retried; a claimed (running) one cannot.
        rc, out = run(cmd_retry, id=eid)
        assert rc == 1, ("running 事件不该被 retry", out)
        rc, out = run(cmd_retry, id=eid + 99)
        assert rc == 1, out

    print("cli.py selftest OK: 状态计数 / 待批准列表 / 批准一次且只一次 / 缺省 --db 回退 / "
          "retry 把 done 事件放回 pending 且保留 attempts")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="wick CLI")
    ap.add_argument("--db", default=None, help="数据库路径（默认 WICK_DB 或 ~/.wick/wick.db）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="各表状态计数").set_defaults(func=cmd_status)
    sub.add_parser("selftest", help="自检").set_defaults(func=lambda _args: _selftest())

    p = sub.add_parser("approvals", help="列出待批准项")
    p.add_argument("--all", action="store_true", help="包含已决策和已执行的")
    p.set_defaults(func=cmd_approvals)

    for name, fn, desc in (("approve", cmd_approve, "批准"), ("reject", cmd_reject, "拒绝")):
        p = sub.add_parser(name, help=desc)
        p.add_argument("id", type=int)
        p.set_defaults(func=fn)

    p = sub.add_parser("retry", help="把 done/failed 事件重新排回 pending（保留 attempts）")
    p.add_argument("id", type=int, help="事件 id（src/cli.py status 里能看到）")
    p.set_defaults(func=cmd_retry)
    p = sub.add_parser("schedule", help="给未来的自己排一个 followup")
    p.add_argument("when", help="ISO 时间，如 2026-11-23T09:00:00+08:00（不带偏移按本机时区）")
    p.add_argument("what", help="到时候做什么")
    p.set_defaults(func=cmd_schedule)
    p = sub.add_parser("panel", help="起本地面板（D8）")
    p.add_argument("--port", type=int, default=9119, help="监听端口（0 = 让 OS 分配）")
    p.set_defaults(func=cmd_panel)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
