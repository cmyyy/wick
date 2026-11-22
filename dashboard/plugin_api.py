"""Wick dashboard plugin — backend API routes.

Mounted at /api/plugins/wick/ by the dashboard plugin system. Reads the wick
sqlite database (``WICK_DB`` → ``~/.wick/wick.db``) and exposes four GETs plus
the two approval buttons' POSTs.

Why this file does not ``import`` the repo's ``src/db.py``: the plugin is
*deployed* by copying ``dashboard/*`` into ``~/.hermes/plugins/wick/``, a tree
where ``src/`` does not exist, so a repo import would break exactly where the
plugin has to work. The path rule (``WICK_DB`` → home default) and the column
names are therefore repeated here; the tables themselves are never created or
migrated — the read connection is ``mode=ro``, which refuses a missing file
instead of silently creating an empty database.

Approval POSTs only move an approval out of ``waiting``. ``worker.process_approvals``
is the system's single path that acts on one (lease + fence); a panel that
called ``actions.execute_approval`` itself would bypass both.

Plugin contract: every handler folds its own failure into "no data" (``[]`` /
``{}``) — a broken database must never surface as a 500 in the browser.

Self-check: ``python dashboard/plugin_api.py`` (needs only stdlib; fastapi is
shimmed when the handler is exercised outside Hermes).
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

try:
    from fastapi import APIRouter

    router = APIRouter()
except ImportError:  # selftest runs on a bare python; the real host always has fastapi
    class _NoopRouter:
        def get(self, *_a, **_k):
            return lambda fn: fn

        def post(self, *_a, **_k):
            return lambda fn: fn

    router = _NoopRouter()

LIMIT_DEFAULT = 50
BODY_SUMMARY_CHARS = 120


def _db_path() -> Path:
    return Path(os.environ.get("WICK_DB") or Path.home() / ".wick" / "wick.db")


def _now() -> str:
    """Same layout as src/db.py:now() — UTC, second precision, +00:00 offset."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read(sql: str, params: tuple = ()) -> list[dict]:
    """Read-only query. Raises on missing file/table; handlers fold that into "no data"."""
    uri = _db_path().resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _decide(approval_id: int, approved: bool) -> dict:
    """Flip one waiting approval to approved/rejected. Nothing executes here."""
    path = _db_path().resolve()
    if not path.exists():
        return {"updated": 0, "id": approval_id, "error": "db missing"}
    status = "approved" if approved else "rejected"
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        cur = conn.execute(
            "UPDATE approvals SET status=?, decided_at=? WHERE id=? AND status='waiting'",
            (status, _now(), approval_id),
        )
        conn.commit()
        return {"updated": cur.rowcount, "id": approval_id}
    finally:
        conn.close()


@router.get("/events")
def events(limit: int = LIMIT_DEFAULT):
    """Recent events, newest first.

    Plain `def`, not `async def`: the sqlite calls inside are blocking, and a
    coroutine handler runs on the event loop — in-process plugin code would
    stall the gateway. FastAPI runs a sync handler in its threadpool.
    """
    try:
        return _read(
            "SELECT id, source_type, source_id, status, attempts, due_at, "
            "created_at, updated_at, error FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    except Exception:  # plugin contract: never raise out of a handler
        return []


@router.get("/approvals")
def approvals():
    """Approvals still waiting on a human. detail is JSON text in the table."""
    try:
        rows = _read(
            "SELECT id, event_id, action, detail, status, created_at FROM approvals "
            "WHERE status='waiting' AND executed_at IS NULL ORDER BY id DESC"
        )
    except Exception:
        return []
    for row in rows:
        try:
            row["detail"] = json.loads(row["detail"] or "{}")
        except (TypeError, ValueError):
            row["detail"] = {}
    return rows


@router.get("/deliveries")
def deliveries(limit: int = LIMIT_DEFAULT):
    """Recent deliveries, newest first, body trimmed to a summary."""
    try:
        rows = _read(
            "SELECT id, event_id, channel, status, attempts, body, created_at, sent_at "
            "FROM deliveries ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    except Exception:
        return []
    for row in rows:
        body = row.get("body") or ""
        row["body"] = body[:BODY_SUMMARY_CHARS]
    return rows


@router.get("/stats")
def stats():
    """Health numbers. duplicate_rate counts user-visible repeats, not retries.

    One event notified twice (>1 status='sent' delivery) over the events that
    were notified at all. attempts > 1 is crash-recovery retry, the normal case,
    and is deliberately not the numerator.
    """
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = _read(
            "SELECT "
            "(SELECT COUNT(*) FROM events) AS events_total, "
            "(SELECT COUNT(*) FROM events WHERE status='failed') AS events_failed, "
            "(SELECT COUNT(*) FROM (SELECT event_id FROM deliveries "
            "  WHERE status='sent' AND event_id IS NOT NULL "
            "  GROUP BY event_id HAVING COUNT(*) > 1)) AS duplicate_events, "
            "(SELECT COUNT(DISTINCT event_id) FROM deliveries "
            "  WHERE status='sent' AND event_id IS NOT NULL) AS events_with_sent_delivery, "
            "(SELECT COUNT(*) FROM deliveries WHERE created_at >= ?) AS deliveries_today, "
            "(SELECT value FROM meta WHERE key='stats:stale_requeued') AS stale_requeued",
            (today,),
        )[0]
    except Exception:
        return {}
    sent = int(row.get("events_with_sent_delivery") or 0)
    dup = int(row.get("duplicate_events") or 0)
    # 全仓没有 stats:stale_requeued 的写者（落库的事在 issue #20），取不到就返 null
    # 让前端显示「未接入」——返 0 会被当成「一次都没回收过」这个假事实。
    raw_stale = row.get("stale_requeued")
    try:
        stale = int(raw_stale) if raw_stale is not None else None
    except (TypeError, ValueError):
        stale = None
    return {
        "events_total": int(row.get("events_total") or 0),
        "events_failed": int(row.get("events_failed") or 0),
        "duplicate_events": dup,
        "events_with_sent_delivery": sent,
        "duplicate_rate": round(dup / sent, 4) if sent else 0,
        "stale_requeued": stale,
        "deliveries_today": int(row.get("deliveries_today") or 0),
    }


@router.post("/approvals/{approval_id}/approve")
def approve(approval_id: int):
    try:
        return _decide(approval_id, True)
    except Exception:
        return {"updated": 0, "id": approval_id}


@router.post("/approvals/{approval_id}/reject")
def reject(approval_id: int):
    try:
        return _decide(approval_id, False)
    except Exception:
        return {"updated": 0, "id": approval_id}


# ------------------------------------------------------------------ selftest

def _selftest() -> int:
    """Seed a temp database, then check all six endpoints against it.

    Handlers are plain `def` now (blocking sqlite → threadpool), so they are
    called directly, not awaited.
    """
    import inspect
    import sys
    import tempfile

    # Fixture only: the plugin proper never touches the repo tree.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import db as wickdb

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["WICK_DB"] = str(Path(tmp) / "wick.db")
        conn = wickdb.connect(os.environ["WICK_DB"])

        ev_ok, _ = wickdb.enqueue(conn, "github_issue", "cmyyy/wick#5", {"title": "看板"})
        ev_dup, _ = wickdb.enqueue(conn, "github_issue", "cmyyy/wick#6", {"title": "重复通知"})
        wickdb.add_delivery(conn, ev_ok, "一件事，通知一次")
        conn.execute("UPDATE deliveries SET status='sent' WHERE event_id=?", (ev_ok,))
        wickdb.add_delivery(conn, ev_dup, "第一次")
        wickdb.add_delivery(conn, ev_dup, "第二次")
        conn.execute("UPDATE deliveries SET status='sent' WHERE event_id=?", (ev_dup,))
        conn.commit()
        aid = wickdb.request_approval(conn, ev_ok, "pr_open", {"repo": "cmyyy/wick", "number": 5})
        assert aid is not None
        wickdb.set_meta(conn, "stats:stale_requeued", "3")
        conn.close()

        # 六个 handler 都必须是普通 def：coroutine 会跑在事件循环上，把同步 sqlite
        # 的阻塞算进网关。这条正是「改回 async def 就红」的机关。
        for fn in (events, approvals, deliveries, stats, approve, reject):
            assert not inspect.iscoroutinefunction(fn), f"{fn.__name__} 又变回 async def 了"

        got_events = events()
        assert [e["source_id"] for e in got_events] == ["cmyyy/wick#6", "cmyyy/wick#5"], got_events
        assert got_events[0]["status"] == "pending" and got_events[0]["attempts"] == 0

        got_approvals = approvals()
        assert len(got_approvals) == 1 and got_approvals[0]["id"] == aid, got_approvals
        assert got_approvals[0]["detail"] == {"repo": "cmyyy/wick", "number": 5}, got_approvals[0]

        got_deliveries = deliveries()
        assert len(got_deliveries) == 3 and got_deliveries[0]["body"] == "第二次", got_deliveries
        assert {d["status"] for d in got_deliveries} == {"sent"}

        got_stats = stats()
        assert got_stats["events_total"] == 2, got_stats
        assert got_stats["events_failed"] == 0, got_stats
        assert got_stats["duplicate_events"] == 1, got_stats          # 只有 ev_dup
        assert got_stats["events_with_sent_delivery"] == 2, got_stats
        assert got_stats["duplicate_rate"] == 0.5, got_stats          # 1 / 2，不是 NaN
        assert got_stats["stale_requeued"] == 3, got_stats
        assert got_stats["deliveries_today"] == 3, got_stats

        # 没有写者时是 None（前端显示「未接入」），不是假的 0。
        conn = sqlite3.connect(os.environ["WICK_DB"])
        conn.execute("DELETE FROM meta WHERE key='stats:stale_requeued'")
        conn.commit()
        conn.close()
        assert stats()["stale_requeued"] is None, stats()["stale_requeued"]
        assert stats()["duplicate_rate"] == 0.5, "删 meta 不该影响重复处理率"

        # approve: status flips, and execution stays the worker's job.
        res = approve(aid)
        assert res["updated"] == 1, res
        conn = sqlite3.connect(os.environ["WICK_DB"])
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
        assert row["status"] == "approved" and row["executed_at"] is None, dict(row)
        assert row["decided_at"] is not None
        conn.close()
        assert approvals() == []           # 已决策的不再出现在待批准
        assert approve(aid)["updated"] == 0  # 幂等：不是 waiting 了就不动

        # reject on a fresh approval.
        conn = sqlite3.connect(os.environ["WICK_DB"])
        conn.execute("INSERT INTO approvals (action, detail, created_at) VALUES ('push', '{}', ?)",
                     (wickdb.now(),))
        conn.commit()
        new_id = conn.execute("SELECT MAX(id) FROM approvals").fetchone()[0]
        conn.close()
        assert reject(new_id)["updated"] == 1
        conn = sqlite3.connect(os.environ["WICK_DB"])
        assert conn.execute("SELECT status FROM approvals WHERE id=?", (new_id,)).fetchone()[0] == "rejected"
        conn.close()

        # missing db file: empty, and never created as a side effect.
        absent = Path(tmp) / "nope.db"
        os.environ["WICK_DB"] = str(absent)
        assert events() == []
        assert approvals() == []
        assert deliveries() == []
        assert stats() == {}
        assert not absent.exists(), "读接口不该创建数据库文件"

        # db present but tables missing: still "no data", not an exception.
        bare = Path(tmp) / "bare.db"
        sqlite3.connect(str(bare)).close()
        os.environ["WICK_DB"] = str(bare)
        assert events() == [] and stats() == {}

    print("plugin_api.py selftest OK: 四个 GET 真库读数 / 重复处理率口径 / "
          "假死回收未接入返 null / 六个 handler 非 coroutine / 批准只改状态 / 缺库缺表返回空结构")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
