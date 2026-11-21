"""wick data layer: three tables, dedupe, and state transitions.

Design notes (see DECISIONS.md #6):
- Each table owns its own state machine instead of one wide table; a single
  table would end up full of mutually exclusive nullable columns.
- Dedupe relies on a UNIQUE index, not an application-level check; a check in
  the app races under concurrency and misses.
- Events are claimed with a single UPDATE ... RETURNING so two workers can
  never grab the same row, and the claim records who holds it (worker_id) and a
  lease (heartbeat_at). A claim only dies of old age: requeue_stale() reclaims
  rows whose worker stopped beating, never rows a live worker is still on.

ponytail: one worker per inbox on purpose (upstream advice, and two concurrent
runs would edit the same files). The lease exists so that starting a second
worker by hand is harmless instead of double-processing; real parallelism means
sharding by source_type, not more workers on one inbox.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB = Path(os.environ.get("WICK_DB") or Path.home() / ".wick" / "wick.db")

# Lease length. A running event stays owned while its worker keeps beating
# heartbeat_at; a claim whose beat is older than this is presumed dead and gets
# requeued. Shorter = a killed worker's event retries sooner, but a stalled
# worker (machine suspended, heartbeat thread starved) is more likely to be
# presumed dead. Keep the beat well under it: worker.HEARTBEAT_INTERVAL.
LEASE_SECONDS = 90

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key  TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'pending',
    worker_id   TEXT,
    heartbeat_at TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    error       TEXT,
    CHECK (status IN ('pending','running','done','failed'))
);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);

CREATE TABLE IF NOT EXISTS deliveries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        INTEGER REFERENCES events(id),
    channel         TEXT NOT NULL DEFAULT 'feishu',
    body            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    created_at      TEXT NOT NULL,
    sent_at         TEXT,
    CHECK (status IN ('queued','sent','throttled','failed'))
);
CREATE INDEX IF NOT EXISTS idx_deliveries_status ON deliveries(status);

CREATE TABLE IF NOT EXISTS approvals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER REFERENCES events(id),
    action      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'waiting',
    created_at  TEXT NOT NULL,
    decided_at  TEXT,
    executed_at TEXT,
    CHECK (status IN ('waiting','approved','rejected','expired'))
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);

-- Key/value metadata for event sources (e.g. github:cmyyy/wick:last_poll watermark).
CREATE TABLE IF NOT EXISTS meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dedupe_key(source_type: str, source_id: str) -> str:
    """Stable key for one item from one source; identical however often it arrives."""
    return hashlib.sha256(f"{source_type}:{source_id}".encode()).hexdigest()


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: the worker's heartbeat thread renews the lease on
    # its in-flight event while the main thread is blocked inside handle().
    # SQLite is built serialized (its default), so the connection stays safe.
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # single writer, many readers: WAL keeps reads unblocked
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that postdate the first deployment; CREATE TABLE IF NOT EXISTS will not."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(approvals)")}
    if "executed_at" not in cols:
        conn.execute("ALTER TABLE approvals ADD COLUMN executed_at TEXT")

    # Lease columns (issue #1). Rows claimed before this upgrade have no
    # heartbeat_at and are treated as stale on the next requeue_stale().
    ecols = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
    for col in ("worker_id", "heartbeat_at"):
        if col not in ecols:
            conn.execute(f"ALTER TABLE events ADD COLUMN {col} TEXT")

    conn.commit()


# ------------------------------------------------------------------ events

def enqueue(conn: sqlite3.Connection, source_type: str, source_id: str,
            payload: dict[str, Any] | None = None) -> tuple[int | None, bool]:
    """Enqueue one event. Returns (event_id, created).

    Duplicates return (existing_id, False) instead of raising: the caller is a
    poller and should not need retry logic just because it saw an item twice.
    """
    key = dedupe_key(source_type, source_id)
    ts = now()
    cur = conn.execute(
        "INSERT OR IGNORE INTO events (dedupe_key, source_type, source_id, payload, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (key, source_type, source_id, json.dumps(payload or {}, ensure_ascii=False), ts, ts),
    )
    conn.commit()
    if cur.rowcount == 1:
        return cur.lastrowid, True
    row = conn.execute("SELECT id FROM events WHERE dedupe_key=?", (key,)).fetchone()
    return (row["id"] if row else None), False


def claim_next(conn: sqlite3.Connection, worker_id: str) -> sqlite3.Row | None:
    """Atomically claim one pending event and mark it running under this worker.

    Stamps worker_id and the starting heartbeat, which is the lease: other
    workers may only take it back once this one stops beating. None if the queue
    is empty.
    """
    ts = now()
    row = conn.execute(
        "UPDATE events SET status='running', worker_id=?, heartbeat_at=?, "
        "attempts=attempts+1, updated_at=? "
        "WHERE id = (SELECT id FROM events WHERE status='pending' ORDER BY id LIMIT 1) "
        "RETURNING *",
        (worker_id, ts, ts),
    ).fetchone()
    conn.commit()
    return row


def heartbeat(conn: sqlite3.Connection, event_id: int, worker_id: str) -> bool:
    """Renew the lease on one in-flight event. False when it is no longer ours.

    Renewing is deliberately ownership-checked: if this row was reclaimed and
    re-claimed elsewhere, the old worker must not resurrect its claim.
    """
    cur = conn.execute(
        "UPDATE events SET heartbeat_at=? WHERE id=? AND worker_id=? AND status='running'",
        (now(), event_id, worker_id),
    )
    conn.commit()
    return cur.rowcount == 1


def finish_event(conn: sqlite3.Connection, event_id: int, status: str,
                 error: str | None = None) -> None:
    if status not in ("done", "failed"):
        raise ValueError(f"finish_event 只接受 done/failed，收到 {status}")
    conn.execute(
        "UPDATE events SET status=?, error=?, updated_at=? WHERE id=?",
        (status, error, now(), event_id),
    )
    conn.commit()


def requeue_stale(conn: sqlite3.Connection) -> int:
    """Reclaim events whose worker stopped beating (kill -9, crash, power loss).

    Only a stale lease is reclaimed, never "everything still running": a live
    worker renews heartbeat_at every few seconds, so starting a second worker by
    hand cannot steal the event the first one is in the middle of processing.
    Recovery latency after a crash is at most LEASE_SECONDS.
    """
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=LEASE_SECONDS)).isoformat(timespec="seconds")
    cur = conn.execute(
        "UPDATE events SET status='pending', worker_id=NULL, heartbeat_at=NULL, updated_at=? "
        "WHERE status='running' AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
        (now(), cutoff),
    )
    conn.commit()
    return cur.rowcount


# -------------------------------------------------------------- deliveries

def add_delivery(conn: sqlite3.Connection, event_id: int | None, body: str,
                 channel: str = "feishu") -> int:
    cur = conn.execute(
        "INSERT INTO deliveries (event_id, channel, body, created_at) VALUES (?,?,?,?)",
        (event_id, channel, body, now()),
    )
    conn.commit()
    return int(cur.lastrowid)


# --------------------------------------------------------------- approvals

def request_approval(conn: sqlite3.Connection, event_id: int | None, action: str,
                     detail: dict[str, Any] | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO approvals (event_id, action, detail, created_at) VALUES (?,?,?,?)",
        (event_id, action, json.dumps(detail or {}, ensure_ascii=False), now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def decide_approval(conn: sqlite3.Connection, approval_id: int, approved: bool) -> None:
    status = "approved" if approved else "rejected"
    cur = conn.execute(
        "UPDATE approvals SET status=?, decided_at=? WHERE id=? AND status='waiting'",
        (status, now(), approval_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise ValueError(f"审批 {approval_id} 不在 waiting 状态，无法决策")


def approved_undone(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Approvals the user said yes to that have not been carried out yet."""
    return conn.execute(
        "SELECT * FROM approvals WHERE status='approved' AND executed_at IS NULL ORDER BY id"
    ).fetchall()


def mark_executed(conn: sqlite3.Connection, approval_id: int, error: str | None = None) -> None:
    """Record that an approved action ran, or failed trying. Either way it is not retried blindly."""
    if error:
        row = conn.execute("SELECT detail FROM approvals WHERE id=?", (approval_id,)).fetchone()
        detail = json.loads(row["detail"] or "{}")
        detail["execution_error"] = error
        conn.execute("UPDATE approvals SET executed_at=?, detail=? WHERE id=?",
                     (now(), json.dumps(detail, ensure_ascii=False), approval_id))
    else:
        conn.execute("UPDATE approvals SET executed_at=? WHERE id=?", (now(), approval_id))
    conn.commit()


# ----------------------------------------------------- meta (watermarks)

def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, now()),
    )
    conn.commit()


# ---------------------------------------------------------------- selftest

def _selftest() -> int:
    """Self-check: dedupe, atomic claim, state transitions, delivery persistence."""
    conn = connect(":memory:")

    # Dedupe: same item twice yields one row.
    eid1, created1 = enqueue(conn, "github_issue", "cmyyy/wick#1", {"title": "断点续跑失败"})
    eid2, created2 = enqueue(conn, "github_issue", "cmyyy/wick#1", {"title": "断点续跑失败"})
    assert created1 is True and created2 is False, (created1, created2)
    assert eid1 == eid2, (eid1, eid2)
    assert conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 1

    # Same id from a different source is not a duplicate.
    eid3, created3 = enqueue(conn, "manual", "cmyyy/wick#1")
    assert created3 is True and eid3 != eid1

    # Atomic claim records the owner and starts a lease.
    ev = claim_next(conn, "w1")
    assert ev is not None and ev["status"] == "running" and ev["attempts"] == 1
    assert ev["worker_id"] == "w1" and ev["heartbeat_at"], dict(ev)
    ev2 = claim_next(conn, "w2")  # the manual row is still pending
    assert ev2 is not None and ev2["id"] != ev["id"] and ev2["worker_id"] == "w2"
    assert claim_next(conn, "w3") is None  # everything claimed

    # State transition plus delivery.
    finish_event(conn, ev["id"], "done")
    add_delivery(conn, ev["id"], "已处理：断点续跑失败")
    row = conn.execute("SELECT status FROM events WHERE id=?", (ev["id"],)).fetchone()
    assert row["status"] == "done"
    assert conn.execute("SELECT COUNT(*) c FROM deliveries").fetchone()["c"] == 1

    # Issue #1: a claim whose heartbeat is fresh is NOT stale. A second worker
    # starting up must leave it running rather than process it a second time.
    assert requeue_stale(conn) == 0, "心跳新鲜的 running 事件被当成残留回收了"
    assert conn.execute("SELECT status FROM events WHERE id=?", (ev2["id"],)).fetchone()["status"] == "running"

    # A live worker renews its lease, so a long run never ages into "stale".
    stale = (datetime.now(timezone.utc)
             - timedelta(seconds=LEASE_SECONDS + 60)).isoformat(timespec="seconds")
    conn.execute("UPDATE events SET heartbeat_at=? WHERE id=?", (stale, ev2["id"]))
    conn.commit()
    assert heartbeat(conn, ev2["id"], "w2") is True, "心跳应该续上自己的租约"
    assert requeue_stale(conn) == 0

    # Crash recovery: rows left running by a worker that stopped beating go back to pending.
    conn.execute("UPDATE events SET heartbeat_at=? WHERE id=?", (stale, ev2["id"]))
    conn.commit()
    n = requeue_stale(conn)
    assert n == 1, n  # only ev2 is still running
    row = conn.execute("SELECT status, worker_id FROM events WHERE id=?", (ev2["id"],)).fetchone()
    assert row["status"] == "pending" and row["worker_id"] is None, dict(row)

    # A beat on a finished or already-reclaimed event is refused, not applied.
    assert heartbeat(conn, ev["id"], "w1") is False  # ev is done
    assert heartbeat(conn, ev2["id"], "w2") is False  # ev2 is pending again

    assert claim_next(conn, "w2")["id"] == ev2["id"]

    # Approval state machine.
    aid = request_approval(conn, ev["id"], "提 PR", {"repo": "cmyyy/wick"})
    decide_approval(conn, aid, True)
    assert conn.execute("SELECT status FROM approvals WHERE id=?", (aid,)).fetchone()["status"] == "approved"
    try:
        decide_approval(conn, aid, True)
    except ValueError:
        pass
    else:
        raise AssertionError("重复决策应该报错")

    conn.close()
    print("db.py selftest OK: 去重 / 原子领取 / 租约心跳 / 状态流转 / 崩溃恢复 / 审批")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
