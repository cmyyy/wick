"""wick worker：单进程串行消费 events 表。

一个 inbox 一个活跃 worker（上游建议）。串行不是保守，是为了不让两个 run
改同一批文件；真要并行时按 source_type 分片，不是在这里加线程。

用法：
    python worker.py --once        # 处理完当前所有 pending 就退出（测试/兜底）
    python worker.py               # 常驻，5 秒轮询
    python worker.py --selftest    # 自检
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

POLL_INTERVAL = 5  # 秒


def handle(event) -> str:
    """处理一条事件，返回要投递的文本。

    D1：占位，只回声，用来验证「进 → 处理 → 出」这条链路。
    D3：换成通过 gateway RPC 起一次 run，把 run 的输出摘要返回。
    """
    payload = json.loads(event["payload"] or "{}")
    title = payload.get("title") or payload.get("text") or "(无标题)"
    return f"[D1 占位] 处理了 {event['source_type']}:{event['source_id']} — {title}"


def run_once(conn) -> int:
    """处理直到队列空，返回处理条数。"""
    processed = 0
    while True:
        ev = db.claim_next(conn)
        if ev is None:
            return processed
        try:
            text = handle(ev)
        except Exception as exc:  # 单条失败不能拖垮整个 worker
            log.exception("事件 %s 处理失败", ev["id"])
            db.finish_event(conn, ev["id"], "failed", repr(exc))
            db.add_delivery(conn, ev["id"], f"处理失败：{ev['source_type']}:{ev['source_id']} — {exc!r}")
            continue
        db.finish_event(conn, ev["id"], "done")
        db.add_delivery(conn, ev["id"], text)
        processed += 1
        log.info("done #%s %s:%s", ev["id"], ev["source_type"], ev["source_id"])


def serve(conn, interval: int = POLL_INTERVAL) -> None:
    """常驻循环。SIGTERM/SIGINT 时干净退出，把手上的事件交还队列。"""
    stopping = {"flag": False}

    def _stop(signum, _frame):
        log.info("收到信号 %s，准备退出", signum)
        stopping["flag"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    n = db.requeue_stale(conn)  # 上次崩溃留下的 running 放回队列
    if n:
        log.warning("恢复了 %s 条残留的 running 事件", n)

    log.info("worker 启动，轮询间隔 %ss，库=%s", interval, db.DEFAULT_DB)
    while not stopping["flag"]:
        processed = run_once(conn)
        if processed == 0:
            time.sleep(interval)
    log.info("worker 退出")


# --------------------------------------------------------------- selftest

def _selftest() -> int:
    """自检：进 → 处理 → 出 全链路，含去重与失败隔离。"""
    conn = db.connect(":memory:")

    # 两条真事件 + 一条重复
    db.enqueue(conn, "github_issue", "cmyyy/wick#1", {"title": "断点续跑失败"})
    db.enqueue(conn, "github_issue", "cmyyy/wick#2", {"title": "outbox 节流算错"})
    db.enqueue(conn, "github_issue", "cmyyy/wick#1", {"title": "断点续跑失败"})  # 重复

    n = run_once(conn)
    assert n == 2, n  # 重复那条被去重，不处理

    rows = conn.execute("SELECT status, COUNT(*) c FROM events GROUP BY status").fetchall()
    assert {r["status"]: r["c"] for r in rows} == {"done": 2}, rows

    deliveries = conn.execute("SELECT body FROM deliveries ORDER BY id").fetchall()
    assert len(deliveries) == 2, deliveries
    assert "断点续跑失败" in deliveries[0]["body"], deliveries[0]["body"]

    # 队列空了再跑不做事
    assert run_once(conn) == 0

    # 失败隔离：handle 抛异常时事件标 failed、仍产出投递、worker 不挂
    global handle
    original = handle

    def boom(_event):
        raise RuntimeError("模拟处理失败")

    handle = boom
    try:
        db.enqueue(conn, "manual", "fail-1", {"title": "会失败的那条"})
        assert run_once(conn) == 0  # 失败不计入 processed
    finally:
        handle = original

    failed = conn.execute("SELECT status, error FROM events WHERE source_id='fail-1'").fetchone()
    assert failed["status"] == "failed" and "模拟处理失败" in failed["error"], dict(failed)
    assert conn.execute(
        "SELECT COUNT(*) c FROM deliveries WHERE body LIKE '处理失败%'"
    ).fetchone()["c"] == 1

    conn.close()
    print("worker.py selftest OK: 进→处理→出 / 去重 / 空队列 / 失败隔离")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="wick worker")
    ap.add_argument("--once", action="store_true", help="处理完当前队列就退出")
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL, help="轮询间隔（秒）")
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
            n = run_once(conn)
            print(f"处理了 {n} 条事件")
            return 0
        serve(conn, args.interval)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
