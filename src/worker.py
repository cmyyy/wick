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
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402

log = logging.getLogger("wick.worker")

POLL_INTERVAL = 5        # seconds between queue checks
GITHUB_INTERVAL = 300    # seconds between GitHub polls


def handle(event) -> str:
    """Process one event and return the text to deliver.

    A github_issue becomes a real Hermes run (D3). Other source types still take
    the placeholder path until they have a prompt of their own.
    """
    if event["source_type"] == "github_issue":
        import agent  # local import: keeps --selftest free of model deps
        return agent.handle_event(event)

    payload = json.loads(event["payload"] or "{}")
    title = payload.get("title") or payload.get("text") or "(无标题)"
    return f"[占位] 处理了 {event['source_type']}:{event['source_id']} — {title}"


def run_once(conn) -> int:
    """Drain the queue, returning how many events were processed."""
    processed = 0
    while True:
        ev = db.claim_next(conn)
        if ev is None:
            return processed
        try:
            text = handle(ev)
        except Exception as exc:  # one bad event must not take the worker down
            log.exception("事件 %s 处理失败", ev["id"])
            db.finish_event(conn, ev["id"], "failed", repr(exc))
            db.add_delivery(conn, ev["id"], f"处理失败：{ev['source_type']}:{ev['source_id']} — {exc!r}")
            continue
        db.finish_event(conn, ev["id"], "done")
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

    n = db.requeue_stale(conn)  # recover rows a previous crash left running
    if n:
        log.warning("恢复了 %s 条残留的 running 事件", n)

    log.info("worker 启动，轮询间隔 %ss，库=%s，仓库=%s", interval, db.DEFAULT_DB, repo or "(不轮询)")
    next_poll = 0.0
    while not stopping["flag"]:
        if repo and time.time() >= next_poll:
            try:
                poll_github(conn, repo)
            except Exception:  # a flaky network must not stop event consumption
                log.exception("GitHub 轮询失败，%ss 后重试", github_interval)
            next_poll = time.time() + github_interval

        if run_once(conn) == 0:
            time.sleep(interval)
    log.info("worker 退出")


# ---------------------------------------------------------------- selftest

def _selftest() -> int:
    """Self-check: full in -> process -> out path, plus dedupe and failure isolation.

    handle() is stubbed for the whole test: the real one would call Hermes.
    """
    global handle
    original = handle
    handle = lambda ev: f"[test] 处理了 {ev['source_id']}"
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

        # An empty queue does nothing.
        assert run_once(conn) == 0

        # Failure isolation: a raising handle() marks the event failed, still emits a
        # delivery, and leaves the worker running.
        def boom(_event):
            raise RuntimeError("模拟处理失败")

        handle = boom
        db.enqueue(conn, "manual", "fail-1", {"title": "会失败的那条"})
        assert run_once(conn) == 0  # failures do not count as processed

        failed = conn.execute("SELECT status, error FROM events WHERE source_id='fail-1'").fetchone()
        assert failed["status"] == "failed" and "模拟处理失败" in failed["error"], dict(failed)
        assert conn.execute(
            "SELECT COUNT(*) c FROM deliveries WHERE body LIKE '处理失败%'"
        ).fetchone()["c"] == 1

        conn.close()
    finally:
        handle = original

    print("worker.py selftest OK: 进→处理→出 / 去重 / 空队列 / 失败隔离")
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
            if args.repo:
                poll_github(conn, args.repo)  # --once also refreshes the source
            n = run_once(conn)
            print(f"处理了 {n} 条事件")
            return 0
        serve(conn, args.interval, args.repo, args.github_interval)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
