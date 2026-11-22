"""wick worker: single-process, serial consumer of the events table.

One active worker per inbox (upstream advice). Serial is not conservatism: two
concurrent runs would touch the same files. Real parallelism means sharding by
source_type, not adding threads here.

Usage:
    python worker.py --once                        # drain the queue, then exit
    python worker.py --repo cmyyy/wick             # resident: poll GitHub + consume
    python worker.py --selftest                    # self-check
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402
import outbox  # noqa: E402

log = logging.getLogger("wick.worker")

POLL_INTERVAL = 5        # seconds between queue checks
GITHUB_INTERVAL = 300    # seconds between GitHub polls
HEARTBEAT_INTERVAL = 30  # seconds between lease renewals; must stay well under db.LEASE_SECONDS
FIX_MODE = os.environ.get("WICK_MODE", "fix")  # "fix" edits code, "analyze" only reports

# Who holds a claimed event. Recorded in events.worker_id so a second worker can
# tell a live claim from an abandoned one (issue #1).
WORKER_ID = f"{socket.gethostname()}#{os.getpid()}"


def handle(conn, event) -> str:
    """Process one event and return the text to deliver.

    A github_issue becomes a real Hermes run. WICK_MODE picks between the two
    modes: "fix" (D4/D5) edits inside a worktree, verifies, then asks for
    approval; "analyze" (D3) only reports a plan. Other source types take the
    placeholder path until they have a prompt of their own.
    """
    if event["source_type"] == "github_issue":
        import agent  # local import: keeps --selftest free of model deps
        if FIX_MODE == "analyze":
            return agent.handle_event(conn, event)
        return agent.handle_issue_fix(conn, event)

    payload = json.loads(event["payload"] or "{}")
    title = payload.get("title") or payload.get("text") or "(无标题)"
    return f"[占位] 处理了 {event['source_type']}:{event['source_id']} — {title}"


def _refusal_reason(conn, row, detail: dict) -> str | None:
    """Why this approved action must not run, or None when it should.

    Three windows matter, and each has bitten a real flow:

    1. The event must have finished. An approval queued by an attempt that then
       lost its lease arrives while the new attempt is still running; executing
       it would open a PR from the stale worktree.
    2. The approval must belong to the attempt that finished the event.
    3. One event runs at most one approval, otherwise two attempts mean two PRs.
    """
    ev = conn.execute("SELECT status, attempts FROM events WHERE id=?",
                      (row["event_id"],)).fetchone()
    if ev is None:
        return "事件不存在"
    if ev["status"] not in ("done", "failed"):
        return f"事件当前状态是 {ev['status']}，不是已结束——审批来自一个已被回收的尝试"

    want = detail.get("attempts")
    if want is not None and ev["attempts"] != want:
        return f"审批属于 attempt {want}，而事件现在是 attempt {ev['attempts']}"

    earlier = conn.execute(
        "SELECT id FROM approvals WHERE event_id=? AND executed_at IS NOT NULL AND id<?",
        (row["event_id"], row["id"])).fetchone()
    if earlier is not None:
        return f"同一事件已有更早的审批 #{earlier['id']} 执行过（避免重复 PR）"
    return None


def process_approvals(conn) -> int:
    """Carry out approvals the user has granted. Returns how many ran.

    This is the only path that performs irreversible work, and it only ever
    looks at rows a human marked approved. It also refuses approvals that no
    longer describe reality: an attempt that queued an approval and then lost
    its lease must not open a PR from a worktree the next attempt has taken over.
    """
    import actions

    ran = 0
    for row in db.approved_undone(conn):
        detail = json.loads(row["detail"] or "{}")
        detail.setdefault("action", row["action"])

        refusal = _refusal_reason(conn, row, detail)
        if refusal:
            # Recorded as executed-once with the reason, so it is not retried
            # forever and a human can see why nothing happened.
            db.mark_executed(conn, row["id"], error=refusal)
            db.add_delivery(conn, row["event_id"],
                            f"审批 #{row['id']} 未执行：{refusal}")
            log.warning("审批 %s 被拒执行：%s", row["id"], refusal)
            continue

        worktree = Path(detail.get("worktree") or "")
        try:
            result = actions.execute_approval(detail, worktree)
        except Exception as exc:
            db.mark_executed(conn, row["id"], error=str(exc))
            db.add_delivery(conn, row["event_id"],
                            f"批准的动作执行失败（审批 #{row['id']}）：{exc}")
            log.exception("审批 %s 执行失败", row["id"])
            continue
        db.mark_executed(conn, row["id"])
        db.add_delivery(conn, row["event_id"],
                        f"已{dict(row)['action']}：{result['url']}\n"
                        f"commit {result['commit']}（审批 #{row['id']} 已执行）")
        log.info("审批 %s 已执行: %s", row["id"], result["url"])
        ran += 1
    return ran


def _renew_lease(conn, event_id: int, stop: threading.Event) -> None:
    """Keep the claim alive while handle() runs; a real run takes minutes.

    A failed beat is not fatal: the row just ages, and the run's own finish_event
    still lands. So the try is INSIDE the loop -- one `database is locked` (30s
    busy timeout, some other writer holding the DB) must cost one beat, not the
    rest of a 25-minute run, which would otherwise age into a needless reclaim.
    Losing ownership entirely is only possible if the lease expired and another
    worker reclaimed it -- then the beat is refused and we stop.
    """
    while not stop.wait(HEARTBEAT_INTERVAL):
        try:
            if not db.heartbeat(conn, event_id, WORKER_ID):
                log.warning("事件 %s 的租约已不属于本 worker，停止心跳", event_id)
                return
        except sqlite3.Error as exc:  # transient: db busy/closed mid-run, keep trying
            log.warning("事件 %s 心跳失败（下一拍重试）：%s", event_id, exc)
            continue


def run_once(conn) -> int:
    """Drain the queue, returning how many events were processed.

    Every write after handle() is fenced on the claim. If the lease was reclaimed
    and handed to another worker meanwhile, this attempt's result is superseded:
    drop it -- no delivery, no status change, no approval. Otherwise one recovery
    would deliver the same event twice (issue #1).
    """
    processed = 0
    while True:
        ev = db.claim_next(conn, WORKER_ID)
        if ev is None:
            return processed
        stop = threading.Event()
        beat = threading.Thread(target=_renew_lease, args=(conn, ev["id"], stop), daemon=True)
        beat.start()
        try:
            text = handle(conn, ev)
        except Exception as exc:  # one bad event must not take the worker down
            log.exception("事件 %s 处理失败", ev["id"])
            if not db.finish_event(conn, ev["id"], "failed", WORKER_ID, ev["attempts"], repr(exc)):
                log.warning("事件 %s 已被回收并转交，丢弃本次失败结果", ev["id"])
                continue
            # str(exc), not repr: a failed D4 run carries the whole review report.
            db.add_delivery(conn, ev["id"],
                            f"处理失败：{ev['source_type']}:{ev['source_id']}\n\n{exc}")
            continue
        finally:
            stop.set()   # the lease thread exits at its next wait
            beat.join()
        if not db.finish_event(conn, ev["id"], "done", WORKER_ID, ev["attempts"]):
            log.warning("事件 %s 已被回收并转交（attempts=%s），丢弃本次结果",
                        ev["id"], ev["attempts"])
            continue
        db.add_delivery(conn, ev["id"], text)
        processed += 1
        log.info("done #%s %s:%s", ev["id"], ev["source_type"], ev["source_id"])


def poll_github(conn, repo: str) -> dict:
    """One GitHub poll, isolated so a network failure cannot kill the worker."""
    from sources import github
    stats = github.poll(conn, repo)
    log.info("poll %s: %s", repo, stats)
    return stats


def serve(conn, interval: int = POLL_INTERVAL, repo: str | None = None,
          github_interval: int = GITHUB_INTERVAL) -> None:
    """Resident loop. Exits cleanly on SIGTERM/SIGINT."""
    stopping = {"flag": False}

    def _stop(signum, _frame):
        log.info("收到信号 %s，准备退出", signum)
        stopping["flag"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    n = db.requeue_stale(conn)  # a crashed worker's claim has to age out before it comes back
    if n:
        log.warning("回收了 %s 条心跳超时的事件", n)

    log.info("worker 启动 worker=%s，轮询间隔 %ss，库=%s，仓库=%s",
             WORKER_ID, interval, db.DEFAULT_DB, repo or "(不轮询)")
    next_poll = 0.0
    while not stopping["flag"]:
        if repo and time.time() >= next_poll:
            try:
                poll_github(conn, repo)
            except Exception:  # a flaky network must not stop event consumption
                log.exception("GitHub 轮询失败，%ss 后重试", github_interval)
            next_poll = time.time() + github_interval

        if process_approvals(conn):
            continue  # an approved action just produced deliveries; drain the queue too
        n = run_once(conn)
        try:
            # separate step: producing a delivery is not sending it
            stats = outbox.drain(conn)
            if any(stats.values()):
                log.info("投递: %s", stats)
        except Exception:  # a lost claim race or a bad row must not kill the worker
            log.exception("投递出错，下一轮重试")
        if n == 0:
            # Idle is when a dead worker's claim becomes visible: reclaim it and
            # loop again instead of sleeping on it. Recovery needs no restart.
            if db.requeue_stale(conn):
                continue
            time.sleep(interval)
    log.info("worker 退出")


# ---------------------------------------------------------------- selftest

def _selftest() -> int:
    """Self-check: full in -> process -> out path, plus dedupe and failure isolation.

    handle() is stubbed for the whole test: the real one would call Hermes.
    """
    global handle, HEARTBEAT_INTERVAL
    original = handle
    original_beat = HEARTBEAT_INTERVAL
    handle = lambda conn, ev: f"[test] 处理了 {ev['source_id']}"
    try:
        conn = db.connect(":memory:")

        # Two real events plus one duplicate.
        db.enqueue(conn, "github_issue", "cmyyy/wick#1", {"title": "断点续跑失败"})
        db.enqueue(conn, "github_issue", "cmyyy/wick#2", {"title": "outbox 节流算错"})
        db.enqueue(conn, "github_issue", "cmyyy/wick#1", {"title": "断点续跑失败"})  # duplicate

        n = run_once(conn)
        assert n == 2, n  # the duplicate never reaches handle()

        rows = conn.execute("SELECT status, COUNT(*) c FROM events GROUP BY status").fetchall()
        assert {r["status"]: r["c"] for r in rows} == {"done": 2}, rows

        deliveries = conn.execute("SELECT body FROM deliveries ORDER BY id").fetchall()
        assert len(deliveries) == 2, deliveries
        assert "cmyyy/wick#1" in deliveries[0]["body"], deliveries[0]["body"]
        # run_once only produces deliveries; outbox.drain is what sends them.
        assert conn.execute(
            "SELECT COUNT(*) c FROM deliveries WHERE status='queued'"
        ).fetchone()["c"] == 2

        # An empty queue does nothing.
        assert run_once(conn) == 0


        # Lease (issue #1): while handle() is busy, the heartbeat thread renews the
        # claim, so a second worker starting up mid-run reclaims nothing.
        seen = {}

        def slow(inner_conn, ev):
            seen["before"] = inner_conn.execute(
                "SELECT heartbeat_at FROM events WHERE id=?", (ev["id"],)).fetchone()["heartbeat_at"]
            seen["owner"] = ev["worker_id"]
            time.sleep(1.2)  # long enough to cross a whole-second timestamp boundary
            seen["during"] = inner_conn.execute(
                "SELECT heartbeat_at FROM events WHERE id=?", (ev["id"],)).fetchone()["heartbeat_at"]
            seen["requeued"] = db.requeue_stale(inner_conn)  # a second worker's startup call
            seen["status"] = inner_conn.execute(
                "SELECT status FROM events WHERE id=?", (ev["id"],)).fetchone()["status"]
            return f"[test] 处理了 {ev['source_id']}"

        HEARTBEAT_INTERVAL = 0.05
        handle = slow
        db.enqueue(conn, "manual", "long-1", {"title": "跑得比较久的那条"})
        assert run_once(conn) == 1
        assert seen["owner"] == WORKER_ID, seen
        assert seen["during"] > seen["before"], seen  # beating, not just the claim timestamp
        assert seen["requeued"] == 0 and seen["status"] == "running", seen  # not stolen mid-run
        assert conn.execute(
            "SELECT status FROM events WHERE source_id='long-1'").fetchone()["status"] == "done"

        # End-to-end through the resident loop: serve() -> run_once -> outbox.drain
        # -> a real subprocess that writes the body to a file. The assertions
        # above only prove a delivery is *produced*; delete the drain call from
        # serve() and this block is what goes red.
        # Restore the plain stub: the lease test above replaced handle() with one
        # that sleeps, and this block patches time.sleep to raise to stop the loop.
        # `original` is the real handler (it would call Hermes), so rebuild the stub.
        handle = lambda conn, ev: f"[test] 处理了 {ev['source_id']}"  # noqa: E731
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "rec.py").write_text(
                "import sys, pathlib\n"
                "pathlib.Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')\n",
                encoding="utf-8",
            )
            got = (tmp / "got.txt").resolve()
            e2e = db.connect(tmp / "e2e.db")
            try:
                db.enqueue(e2e, "manual", "e2e-1", {"title": "端到端"})
                saved_cmd = os.environ.get("WICK_SEND_CMD")
                saved_sleep = time.sleep
                os.environ["WICK_SEND_CMD"] = (
                    f'"{sys.executable}" "{(tmp / "rec.py").resolve()}" "{got}"')

                class _OneTick(Exception):
                    pass

                def _stop_after_tick(_seconds):  # one tick of the resident loop is enough
                    raise _OneTick

                time.sleep = _stop_after_tick
                try:
                    try:
                        serve(e2e, interval=0)
                    except _OneTick:
                        pass
                finally:
                    time.sleep = saved_sleep
                    if saved_cmd is None:
                        os.environ.pop("WICK_SEND_CMD", None)
                    else:
                        os.environ["WICK_SEND_CMD"] = saved_cmd

                assert got.exists(), "serve() 没把投递发出去（outbox.drain 还在 serve 里吗？）"
                assert got.read_text(encoding="utf-8") == "[test] 处理了 e2e-1", \
                    got.read_text(encoding="utf-8")
                row = e2e.execute(
                    "SELECT status, attempts FROM deliveries ORDER BY id DESC LIMIT 1").fetchone()
                assert (row["status"], row["attempts"]) == ("sent", 1), dict(row)
            finally:
                e2e.close()

        # A failing drain must not take the resident loop down either: this is
        # how a lost claim race used to kill the worker. The next tick still runs.
        stats_calls = {"n": 0}
        real_drain = outbox.drain

        def broken_drain(_conn, **_kw):
            stats_calls["n"] += 1
            raise ValueError("模拟投递异常：sent → sent")

        outbox.drain = broken_drain
        time.sleep = _stop_after_tick
        try:
            try:
                serve(conn, interval=0)  # ValueError must be swallowed inside serve()
            except _OneTick:
                pass
        finally:
            outbox.drain = real_drain
            time.sleep = saved_sleep
        assert stats_calls["n"] >= 1, "serve() 没有调用 outbox.drain"


        # Failure isolation: a raising handle() marks the event failed, still emits a
        # delivery, and leaves the worker running.
        def boom(_conn, _event):
            raise RuntimeError("模拟处理失败")

        handle = boom
        db.enqueue(conn, "manual", "fail-1", {"title": "会失败的那条"})
        assert run_once(conn) == 0  # failures do not count as processed

        failed = conn.execute("SELECT status, error FROM events WHERE source_id='fail-1'").fetchone()
        assert failed["status"] == "failed" and "模拟处理失败" in failed["error"], dict(failed)
        assert conn.execute(
            "SELECT COUNT(*) c FROM deliveries WHERE body LIKE '处理失败%'"
        ).fetchone()["c"] == 1

        # Issue #1 缺陷1（worker 端）：交付前租约被回收并转交，本次结果必须整体丢弃。
        # 心跳间隔调大，下面的人为回收才不会被心跳线程抢回去。
        HEARTBEAT_INTERVAL = 3600
        stale_ts = (datetime.now(timezone.utc)
                    - timedelta(seconds=db.LEASE_SECONDS + 60)).isoformat(timespec="seconds")

        def steal(inner_conn, ev):
            """模拟另一台 worker 接手：本 worker 停拍 -> 租约超时 -> 被别人领走。"""
            inner_conn.execute("UPDATE events SET heartbeat_at=? WHERE id=?", (stale_ts, ev["id"]))
            inner_conn.commit()
            db.requeue_stale(inner_conn)
            thief = db.claim_next(inner_conn, "worker-B")
            assert thief is not None and thief["id"] == ev["id"], dict(thief or {})
            return "[test] A 的迟到结果"

        def deliveries(inner_conn) -> int:
            return inner_conn.execute("SELECT COUNT(*) c FROM deliveries").fetchone()["c"]

        handle = steal
        db.enqueue(conn, "manual", "stolen-1", {"title": "被抢走的那条"})
        before = deliveries(conn)
        assert run_once(conn) == 0, "被转交的事件不该算本 worker 处理成功"
        row = conn.execute("SELECT status, worker_id, attempts, error FROM events "
                           "WHERE source_id='stolen-1'").fetchone()
        assert (row["status"], row["worker_id"], row["attempts"]) == ("running", "worker-B", 2), dict(row)
        assert row["error"] is None, dict(row)
        assert deliveries(conn) == before, "迟到的成功结果被投递了"

        # 迟到的是失败结果也一样：不覆盖新 worker 的状态、不投递。
        def steal_then_fail(inner_conn, ev):
            steal(inner_conn, ev)
            raise RuntimeError("A 迟到的失败")

        handle = steal_then_fail
        db.enqueue(conn, "manual", "stolen-2", {"title": "被抢走后失败的那条"})
        before = deliveries(conn)
        assert run_once(conn) == 0
        row = conn.execute("SELECT status, worker_id, error FROM events "
                           "WHERE source_id='stolen-2'").fetchone()
        assert row["status"] == "running" and row["worker_id"] == "worker-B", dict(row)
        assert row["error"] is None, dict(row)
        assert deliveries(conn) == before, "迟到的失败结果被投递了"

        # Issue #1 缺陷2：一次心跳抛错不能把心跳线程打死（一次 DB 抖动 = 全程不心跳）。
        HEARTBEAT_INTERVAL = 0.05
        beats = {"n": 0}
        real_beat = db.heartbeat

        def flaky_beat(inner_conn, event_id, worker_id):
            beats["n"] += 1
            if beats["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return real_beat(inner_conn, event_id, worker_id)

        def slow_after_blip(inner_conn, ev):
            time.sleep(1.2)  # 第一拍炸掉之后，线程还得继续拍
            return "[test] 心跳抖动后仍在拍"

        db.heartbeat = flaky_beat
        try:
            handle = slow_after_blip
            db.enqueue(conn, "manual", "flaky-1", {"title": "心跳抖了一下"})
            assert run_once(conn) == 1
        finally:
            db.heartbeat = real_beat
        assert beats["n"] >= 3, f"心跳线程在第一次报错后就退出了: {beats}"
        assert conn.execute("SELECT status FROM events WHERE source_id='flaky-1'"
                            ).fetchone()["status"] == "done"

        # Issue #1 缺陷5：--once 也要做崩溃恢复（演示和调试都走这个入口）。
        with tempfile.TemporaryDirectory() as td:
            once_db = Path(td) / "once.db"
            c2 = db.connect(once_db)
            db.enqueue(c2, "manual", "stale-once", {"title": "崩溃遗留的那条"})
            c2.execute("UPDATE events SET status='running', worker_id='dead-worker', "
                       "heartbeat_at=?, attempts=1 WHERE source_id='stale-once'", (stale_ts,))
            c2.commit()
            c2.close()  # the crashed worker's connection is gone; only the row remains
            p = subprocess.run(
                [sys.executable, str(Path(__file__).parent / "worker.py"),
                 "--once", "--db", str(once_db)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
            )
            assert p.returncode == 0, (p.stdout, p.stderr)
            assert "处理了 1 条事件" in p.stdout, p.stdout + p.stderr

        conn.close()
    finally:
        handle = original
        HEARTBEAT_INTERVAL = original_beat


    # process_approvals 的三道门。缺任何一道，一个失去租约的尝试都能留下一份
    # 能被批准、并且真的会去开 PR 的审批。
    gate = db.connect(":memory:")
    geid, _ = db.enqueue(gate, "manual", "gate-1", {"title": "门测试"})
    gev = db.claim_next(gate, "w-gate")
    gaid = db.request_approval(gate, geid, "提 PR", {"repo": "a/b", "attempts": gev["attempts"]},
                               worker_id="w-gate", attempts=gev["attempts"])
    assert gaid is not None, "主人在跑时应该能插审批"
    db.decide_approval(gate, gaid, True)
    grow = gate.execute("SELECT * FROM approvals WHERE id=?", (gaid,)).fetchone()
    gdet = json.loads(grow["detail"])

    # 门 1：事件还在 running —— 拒绝
    assert _refusal_reason(gate, grow, gdet) is not None, "事件未结束时不该执行审批"

    # 事件正常结束 + attempt 匹配 —— 放行
    db.finish_event(gate, geid, "done", "w-gate", gev["attempts"])
    assert _refusal_reason(gate, grow, gdet) is None, "事件已结束且 attempt 一致，应该放行"

    # 门 2：attempt 不匹配 —— 拒绝
    assert _refusal_reason(gate, grow, {**gdet, "attempts": gev["attempts"] + 7}) is not None, \
        "审批属于旧 attempt 时必须拒绝"

    # 门 3：同一事件已有更早的审批执行过 —— 拒绝第二条
    gaid2 = db.request_approval(gate, geid, "提 PR", {"repo": "a/b", "attempts": gev["attempts"]})
    assert gaid2 is not None
    db.decide_approval(gate, gaid2, True)
    db.mark_executed(gate, gaid)  # 假装第一条已经跑过
    grow2 = gate.execute("SELECT * FROM approvals WHERE id=?", (gaid2,)).fetchone()
    assert _refusal_reason(gate, grow2, json.loads(grow2["detail"])) is not None, \
        "同一事件第二条审批必须拒绝，否则会开第二个 PR"
    gate.close()

    print("worker.py selftest OK: 进→处理→出 / 去重 / 空队列 / 租约心跳 / 失败隔离 / "
          "被抢后丢弃结果 / 心跳抖动不断线 / --once 回收 / 审批三道门 / 常驻循环真发出投递")


    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="wick worker")
    ap.add_argument("--once", action="store_true", help="处理完当前队列就退出")
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL, help="队列轮询间隔（秒）")
    ap.add_argument("--repo", default=None, help="要轮询的 GitHub 仓库（owner/name）")
    ap.add_argument("--github-interval", type=int, default=GITHUB_INTERVAL,
                    help="GitHub 轮询间隔（秒）")
    ap.add_argument("--db", default=None, help="数据库路径（默认 WICK_DB 或 ~/.wick/wick.db）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.selftest:
        return _selftest()

    conn = db.connect(args.db or db.DEFAULT_DB)
    try:
        if args.once:
            n = db.requeue_stale(conn)  # --once is how demos and debugging run: recover here too
            if n:
                log.warning("回收了 %s 条心跳超时的事件", n)
            if args.repo:
                poll_github(conn, args.repo)  # --once also refreshes the source
            ran = process_approvals(conn)
            if ran:
                print(f"执行了 {ran} 个已批准的动作")
            n = run_once(conn)
            print(f"处理了 {n} 条事件")
            print(f"投递: {outbox.drain(conn)}")
            return 0
        serve(conn, args.interval, args.repo, args.github_interval)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
