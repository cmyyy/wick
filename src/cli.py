"""wick CLI: the human side of approvals.

D8 replaces the approve step with a desktop panel; until then this is the only
way to say yes. Deliberately tiny: list, approve, reject, status.

Usage:
    python src/cli.py status
    python src/cli.py approvals [--all]
    python src/cli.py approve <id>
    python src/cli.py reject <id>
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


# ---------------------------------------------------------------- selftest

def _selftest() -> int:
    """Self-check: status counts, pending list, and approve once-and-only-once."""
    import io
    import tempfile
    from contextlib import redirect_stdout

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "wick.db"
        conn = db.connect(path)
        eid, _ = db.enqueue(conn, "github_issue", "cmyyy/wick#1", {"title": "outbox 节流算错"})
        db.finish_event(conn, eid, "done")
        db.add_delivery(conn, eid, "已处理")
        aid = db.request_approval(conn, eid, "提 PR",
                                  {"repo": "cmyyy/wick", "number": 1, "title": "outbox 节流算错"})
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

    print("cli.py selftest OK: 状态计数 / 待批准列表 / 批准一次且只一次 / 缺省 --db 回退")
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

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
