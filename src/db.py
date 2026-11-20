"""wick 数据层：三张表 + 去重 + 状态流转。

设计要点（对应 DECISIONS.md 决策 6）：
- 三张表状态机各自独立，不塞一张表（否则一堆互斥空字段）
- 去重靠唯一索引，不靠应用层判断（并发下应用层判断会漏）
- 事件消费用单条 UPDATE...RETURNING 原子领取，不给两个 worker 抢同一条的机会

ponytail: 单 worker 假设（上游建议「一个 inbox 一个活跃 worker」）。多 worker
并行时把 UPDATE 换成 BEGIN IMMEDIATE + 行锁，或给 source_type 分片。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB = Path(os.environ.get("WICK_DB") or Path.home() / ".wick" / "wick.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key  TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'pending',
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
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   INTEGER REFERENCES events(id),
    action     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '{}',
    status     TEXT NOT NULL DEFAULT 'waiting',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    CHECK (status IN ('waiting','approved','rejected','expired'))
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);

-- 事件源的水位线等键值元数据（例如 github:cmyyy/wick:last_poll）
CREATE TABLE IF NOT EXISTS meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dedupe_key(source_type: str, source_id: str) -> str:
    """去重键：同一来源的同一条内容，无论进来几次都是同一个 key。"""
    return hashlib.sha256(f"{source_type}:{source_id}".encode()).hexdigest()


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # 单写多读，WAL 让读不阻塞写
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------- events

def enqueue(conn: sqlite3.Connection, source_type: str, source_id: str,
            payload: dict[str, Any] | None = None) -> tuple[int | None, bool]:
    """投递一条事件。返回 (event_id, 是否新建)。

    重复投递返回 (已有 id, False)，不抛异常——调用方（轮询器）不该为重复写代码。
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


def claim_next(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """原子领取一条待处理事件并置为 running。没有则返回 None。"""
    row = conn.execute(
        "UPDATE events SET status='running', attempts=attempts+1, updated_at=? "
        "WHERE id = (SELECT id FROM events WHERE status='pending' ORDER BY id LIMIT 1) "
        "RETURNING *",
        (now(),),
    ).fetchone()
    conn.commit()
    return row


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
    """把 running 状态残留的事件放回队列（worker 崩溃后重启时调用）。"""
    cur = conn.execute(
        "UPDATE events SET status='pending', updated_at=? WHERE status='running'", (now(),)
    )
    conn.commit()
    return cur.rowcount


# ------------------------------------------------------------ deliveries

def add_delivery(conn: sqlite3.Connection, event_id: int | None, body: str,
                 channel: str = "feishu") -> int:
    cur = conn.execute(
        "INSERT INTO deliveries (event_id, channel, body, created_at) VALUES (?,?,?,?)",
        (event_id, channel, body, now()),
    )
    conn.commit()
    return int(cur.lastrowid)


# ------------------------------------------------------------- approvals

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


# ------------------------------------------------------- meta (水位等)

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


# --------------------------------------------------------------- selftest

def _selftest() -> int:
    """自检：去重、原子领取、状态流转、投递落库。"""
    conn = connect(":memory:")

    # 去重：同一条插两次，只有一条
    eid1, created1 = enqueue(conn, "github_issue", "cmyyy/wick#1", {"title": "断点续跑失败"})
    eid2, created2 = enqueue(conn, "github_issue", "cmyyy/wick#1", {"title": "断点续跑失败"})
    assert created1 is True and created2 is False, (created1, created2)
    assert eid1 == eid2, (eid1, eid2)
    assert conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 1

    # 不同来源同 id 不算重复
    eid3, created3 = enqueue(conn, "manual", "cmyyy/wick#1")
    assert created3 is True and eid3 != eid1

    # 原子领取
    ev = claim_next(conn)
    assert ev is not None and ev["status"] == "running" and ev["attempts"] == 1
    ev2 = claim_next(conn)  # 还有第二条（manual）
    assert ev2 is not None and ev2["id"] != ev["id"]
    assert claim_next(conn) is None  # 都领完了

    # 状态流转 + 投递
    finish_event(conn, ev["id"], "done")
    add_delivery(conn, ev["id"], "已处理：断点续跑失败")
    row = conn.execute("SELECT status FROM events WHERE id=?", (ev["id"],)).fetchone()
    assert row["status"] == "done"
    assert conn.execute("SELECT COUNT(*) c FROM deliveries").fetchone()["c"] == 1

    # 崩溃恢复：running 的能被放回队列
    n = requeue_stale(conn)
    assert n == 1, n  # 只有 ev2 还在 running
    assert claim_next(conn)["id"] == ev2["id"]

    # 审批状态机
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
    print("db.py selftest OK: 去重 / 原子领取 / 状态流转 / 崩溃恢复 / 审批")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
