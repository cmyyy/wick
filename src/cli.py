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
    python src/cli.py enqueue --title "改什么" [--repo owner/name] [--number 12] [--allow-code]
    python src/cli.py pause [--off]
    python src/cli.py panel [--port 9119]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402

# The only repo this worker can safely change: sandbox.create() and git push both
# use this checkout's own root, so a --repo pointing elsewhere would edit the
# wrong repo (#38 F5). Override only if the checkout itself moved.
SELF_REPO = os.environ.get("WICK_REPO", "cmyyy/wick")


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


def cmd_enqueue(args) -> int:
    """Queue a task by hand so the worker picks it up like any other event (#30).

    source_id defaults to timestamp + random suffix. db.now() alone only has
    second resolution, and dedupe_key is UNIQUE: two dispatches in the same
    second silently collapsed into one row (#38 F1). Pass --id to deliberately
    re-use an id and hit dedupe.

    --repo is checked, not just recorded: the worktree and the push are pinned to
    this checkout, so accepting another repo would quietly edit the wrong one
    (#38 F5). --allow-code is the gray-release gate: without it the run is told
    to stay in docs/copy, and touching src/ fails the run loudly (#38 F6).
    """
    if args.repo and args.repo != SELF_REPO:
        print(f"--repo 只支持本仓库 {SELF_REPO}（worktree 与 push 都指向它）；"
              f"{args.repo} 会改错仓，已拒绝", file=sys.stderr)
        return 1
    conn = connect(args)
    try:
        sid = args.id or f"{db.now()}-{uuid.uuid4().hex[:6]}"
        payload = {"title": args.title}
        if args.repo:
            payload["repo"] = args.repo
        if args.number:
            payload["number"] = args.number
        if getattr(args, "allow_code", False):
            payload["allow_code"] = True
        eid, created = db.enqueue(conn, "manual", sid, payload)
        conn.commit()
        if created:
            gate = "" if payload.get("allow_code") else "（未加 --allow-code：只允许改文档/文案）"
            print(f"已派活 #{eid}（manual:{sid}）：{args.title}{gate}")
        else:
            print(f"已存在 #{eid}（dedupe 命中，未重复入队）：{args.title}")
        return 0
    finally:
        conn.close()


def cmd_pause(args) -> int:
    """Stop handing out manual work; other sources keep running (#30).

    A switch in meta, not a deleted row: the queued work stays queued and resumes
    where it was, which is what makes this a usable escape hatch.
    """
    conn = connect(args)
    try:
        db.set_meta(conn, "manual_paused", "0" if args.off else "1")
        conn.commit()
        if args.off:
            print("已恢复：manual 派活会重新被领走")
        else:
            print("已暂停：manual 派活不再被领走（已入队的还在，--off 恢复）")
        return 0
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
    from contextlib import redirect_stderr, redirect_stdout

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
            # stderr goes into the same buffer: a refused command explains itself
            # there, and the assertions below need to see that text too.
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(buf):
                rc = fn(argparse.Namespace(db=str(path), **kw))
            return rc, buf.getvalue()

        # enqueue (#30): a hand-queued task lands as a manual event the worker can
        # claim, and the same call twice is deduped rather than silently doubled.
        rc, out = run(cmd_enqueue, title="改一下 CONFIG 注释", repo="cmyyy/wick", number=None, id="固定-id")
        assert rc == 0 and "已派活" in out, out
        rc2, out2 = run(cmd_enqueue, title="改一下 CONFIG 注释", repo="cmyyy/wick", number=None, id="固定-id")
        assert rc2 == 0 and "dedupe 命中" in out2, out2
        c = db.connect(path)
        row = c.execute("SELECT source_type, source_id, payload FROM events "
                        "WHERE source_id='固定-id'").fetchone()
        assert row["source_type"] == "manual", row["source_type"]
        assert json.loads(row["payload"])["title"] == "改一下 CONFIG 注释"

        # F1 (#38): db.now() only has second resolution, so two dispatches in one
        # second used to collapse into a single row. Pin the clock and dispatch
        # twice with no --id: both must land.
        real_now = db.now
        db.now = lambda: "2026-11-25T00:00:00+00:00"
        try:
            rc, out = run(cmd_enqueue, title="同秒第一条", repo=None, number=None, id=None)
            assert rc == 0 and "已派活" in out, out
            rc, out = run(cmd_enqueue, title="同秒第二条", repo=None, number=None, id=None)
            assert rc == 0 and "已派活" in out, out
        finally:
            db.now = real_now
        same_second = c.execute(
            "SELECT COUNT(*) c FROM events WHERE source_type='manual' "
            "AND json_extract(payload, '$.title') LIKE '同秒第%'").fetchone()["c"]
        assert same_second == 2, f"同秒派两条只入队了 {same_second} 条"

        # F5 (#38): --repo is not decoration. A foreign repo is refused outright,
        # because the worktree and the push are pinned to this checkout.
        rc, out = run(cmd_enqueue, title="改别的仓", repo="someone/else", number=None, id=None)
        assert rc == 1 and "已拒绝" in out, out
        assert c.execute("SELECT COUNT(*) c FROM events WHERE "
                         "json_extract(payload, '$.title')='改别的仓'").fetchone()["c"] == 0

        # F6 (#38): the gray-release gate is recorded in the payload, so the run
        # itself can refuse src/ changes; enqueue says so out loud.
        rc, out = run(cmd_enqueue, title="只改文档", repo=None, number=None, id=None, allow_code=False)
        assert rc == 0 and "--allow-code" in out, out
        rc, out = run(cmd_enqueue, title="改代码", repo=None, number=None, id=None, allow_code=True)
        assert rc == 0 and "未加 --allow-code" not in out, out
        gate_row = c.execute("SELECT payload FROM events WHERE "
                             "json_extract(payload, '$.title')='改代码'").fetchone()
        assert json.loads(gate_row["payload"]).get("allow_code") is True, gate_row["payload"]

        # pause (#30): excluding manual must not hide a github_issue sitting in the
        # same queue -- otherwise the escape hatch is just a stop button.
        db.enqueue(c, "github_issue", "cmyyy/wick#99", {"title": "issue 那条"})
        c.commit()
        got = db.claim_next(c, "cli-selftest", exclude_sources=("manual",))
        assert got is not None and got["source_type"] == "github_issue", dict(got) if got else None
        c.close()
        assert not [r for r in db.connect(path).execute("SELECT id FROM events WHERE source_id='固定-id' AND status='running'")]

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
    p = sub.add_parser("enqueue", help="派一条活给 worker（issue #30）")
    p.add_argument("--title", required=True, help="要做什么")
    p.add_argument("--repo", default=None, help=f"owner/name（只支持本仓库 {SELF_REPO}）")
    p.add_argument("--number", type=int, default=None, help="关联的 issue 号（可选）")
    p.add_argument("--id", default=None, help="source_id（默认时间戳+随机后缀；同名重派需显式给）")
    p.add_argument("--allow-code", action="store_true",
                   help="允许改 src/ 下的代码（默认只允许改文档/文案）")
    p.set_defaults(func=cmd_enqueue)
    p = sub.add_parser("pause", help="暂停/恢复 manual 派活")
    p.add_argument("--off", action="store_true", help="恢复派活")
    p.set_defaults(func=cmd_pause)
    p = sub.add_parser("panel", help="起本地面板（D8）")
    p.add_argument("--port", type=int, default=9119, help="监听端口（0 = 让 OS 分配）")
    p.add_argument("--db", default=None, help="数据库路径（默认 WICK_DB 或 ~/.wick/wick.db）")
    p.set_defaults(func=cmd_panel)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
