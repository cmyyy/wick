"""Outbox: put queued delivery rows on a real channel.

Before this module the deliveries table was write-only: rows piled up in
`queued` and nobody sent them. Now one `drain` pass turns due rows into
messages, with a daily cap and retries:

    queued ──► sent        delivered, counted against today's cap
       ├────► throttled   over the cap; due again at the next local midnight
       ├────► failed      MAX_ATTEMPTS spent -- give up
       └────► queued      attempt failed, retry scheduled (self-loop)

`throttled` returns to `queued` by itself once its next_attempt_at passes, and a
retry keeps status `queued` with a future next_attempt_at, so `failed` only ever
means "gave up", never "will try again later".

ponytail: daily spend is a COUNT(*) over today's `sent` rows instead of a
counter in meta. The table is small and a count cannot drift from the rows it
counts.
"""
from __future__ import annotations

import logging
import os
import shlex
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

log = logging.getLogger("wick.outbox")

# 20 a day by default: a busy day must not turn the channel into a firehose.
DAILY_LIMIT = int(os.environ.get("WICK_DAILY_LIMIT", "20"))
MAX_ATTEMPTS = int(os.environ.get("WICK_MAX_ATTEMPTS", "4"))
BACKOFF_BASE = int(os.environ.get("WICK_BACKOFF_BASE", "60"))  # seconds
BACKOFF_CAP = 3600  # an hour is plenty; longer waits belong to a rethink, not a retry

# WICK_SEND_CMD replaces the whole command; the body is appended as the last
# argument. `hermes send` needs no agent loop and no running gateway.
DEFAULT_SEND = "hermes send -t {channel}"
SEND_TIMEOUT = 120

# The deliveries state machine. `queued -> queued` is the retry self-loop.
ALLOWED = {
    "queued": {"sent", "throttled", "failed", "queued"},
    "throttled": {"queued", "failed"},
    "sent": set(),
    "failed": set(),
}


# ------------------------------------------------------------ time helpers

def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds")


def day_start(ts: datetime | None = None) -> datetime:
    """Local midnight: "today" means the user's today, not UTC's."""
    local = (ts or datetime.now(timezone.utc)).astimezone()
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def next_day_start(ts: datetime | None = None) -> datetime:
    return day_start(ts) + timedelta(days=1)


def backoff_delay(attempts: int) -> int:
    """Seconds to wait after the `attempts`-th failed attempt: 60, 120, 240, ..."""
    return min(BACKOFF_BASE * 2 ** (attempts - 1), BACKOFF_CAP)


# ---------------------------------------------------------------- delivery

def send(body: str, channel: str = "feishu", cmd: str | None = None) -> str:
    """Deliver one message. Raises RuntimeError on a non-zero exit or a timeout."""
    # ponytail: the body rides in argv, so a report over ~32k chars hits the
    # Windows command-line limit and fails loudly. Switch to `-f -` plus stdin
    # if that ever happens for real; silently truncating a report is worse.
    raw = cmd or os.environ.get("WICK_SEND_CMD") or DEFAULT_SEND.format(channel=channel)
    argv = shlex.split(raw) + [body]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=SEND_TIMEOUT)
    except FileNotFoundError as e:
        raise RuntimeError(f"找不到投递命令: {argv[0]}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"投递超时（{SEND_TIMEOUT}s）") from e
    except OSError as e:  # e.g. Windows argument too long for a huge body
        raise RuntimeError(f"投递命令启动失败: {e}") from e
    if p.returncode != 0:
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        raise RuntimeError(f"投递失败（退出码 {p.returncode}）: {out[-300:]}")
    return (p.stdout or "").strip()


def transition(conn: sqlite3.Connection, delivery_id: int, to_status: str, *,
               attempts: int | None = None, next_attempt_at: str | None = None,
               sent_at: str | None = None) -> None:
    """Move one delivery, refusing any transition the state machine forbids."""
    row = conn.execute("SELECT status, attempts FROM deliveries WHERE id=?",
                       (delivery_id,)).fetchone()
    if row is None:
        raise ValueError(f"投递 {delivery_id} 不存在")
    if to_status not in ALLOWED[row["status"]]:
        raise ValueError(f"投递 {delivery_id} 非法跃迁：{row['status']} → {to_status}")
    conn.execute(
        "UPDATE deliveries SET status=?, attempts=?, next_attempt_at=?, sent_at=? WHERE id=?",
        (to_status, row["attempts"] if attempts is None else attempts,
         next_attempt_at, sent_at, delivery_id),
    )
    conn.commit()


def sent_today(conn: sqlite3.Connection, ts: datetime | None = None) -> int:
    return conn.execute(
        "SELECT COUNT(*) c FROM deliveries WHERE status='sent' AND sent_at >= ?",
        (_iso(day_start(ts)),),
    ).fetchone()["c"]


def drain(conn: sqlite3.Connection, *, limit: int | None = None,
          now_dt: datetime | None = None, send_fn=None) -> dict:
    """Send every delivery that is due. Returns counts per outcome.

    `limit` overrides the daily cap; `now_dt` and `send_fn` exist so the
    self-check can drive the clock and make the channel fail on purpose.
    """
    limit = DAILY_LIMIT if limit is None else limit
    send_fn = send_fn or send
    now = now_dt or datetime.now(timezone.utc)
    now_s = _iso(now)

    # Yesterday's throttled rows become candidates again at their due time.
    conn.execute("UPDATE deliveries SET status='queued' "
                 "WHERE status='throttled' AND next_attempt_at <= ?", (now_s,))
    conn.commit()

    stats = {"sent": 0, "throttled": 0, "failed": 0, "retrying": 0}
    due = conn.execute(
        "SELECT * FROM deliveries WHERE status='queued' "
        "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY id", (now_s,),
    ).fetchall()
    for row in due:
        if sent_today(conn, now) >= limit:
            transition(conn, row["id"], "throttled", next_attempt_at=_iso(next_day_start(now)))
            stats["throttled"] += 1
            continue
        attempts = row["attempts"] + 1
        try:
            send_fn(row["body"], row["channel"])
        except Exception as exc:
            if attempts >= MAX_ATTEMPTS:
                transition(conn, row["id"], "failed", attempts=attempts)
                log.warning("投递 %s 重试 %s 次后放弃：%s", row["id"], attempts, exc)
                stats["failed"] += 1
            else:
                delay = backoff_delay(attempts)
                transition(conn, row["id"], "queued", attempts=attempts,
                           next_attempt_at=_iso(now + timedelta(seconds=delay)))
                log.info("投递 %s 第 %s 次失败，%ss 后重试：%s",
                         row["id"], attempts, delay, exc)
                stats["retrying"] += 1
        else:
            transition(conn, row["id"], "sent", attempts=attempts, sent_at=now_s)
            stats["sent"] += 1
    return stats


# ---------------------------------------------------------------- selftest

def _selftest() -> int:
    """Self-check: real subprocess send, throttle boundary, backoff, state machine."""
    import tempfile
    from pathlib import Path

    import db

    global DAILY_LIMIT

    # 1. Happy path: one row goes out, body and channel reach the command.
    conn = db.connect(":memory:")
    got: list[tuple[str, str]] = []
    db.add_delivery(conn, None, "已处理：断点续跑失败", "feishu")
    stats = drain(conn, limit=20,
                  send_fn=lambda body, channel="feishu": got.append((channel, body)))
    row = conn.execute("SELECT * FROM deliveries").fetchone()
    assert stats == {"sent": 1, "throttled": 0, "failed": 0, "retrying": 0}, stats
    assert row["status"] == "sent" and row["attempts"] == 1 and row["sent_at"], dict(row)
    assert got == [("feishu", "已处理：断点续跑失败")], got

    # 2. Throttle boundary: cap 1, three rows -> first sent, rest pushed to tomorrow.
    conn = db.connect(":memory:")
    now_dt = datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc)
    for i in range(3):
        db.add_delivery(conn, None, f"第{i + 1}条")
    got.clear()
    stats = drain(conn, limit=1, now_dt=now_dt,
                  send_fn=lambda body, channel="feishu": got.append((channel, body)))
    assert stats["sent"] == 1 and stats["throttled"] == 2, stats
    assert got == [("feishu", "第1条")], got
    rows = conn.execute("SELECT status, next_attempt_at FROM deliveries ORDER BY id").fetchall()
    assert [r["status"] for r in rows] == ["sent", "throttled", "throttled"], [dict(r) for r in rows]
    tomorrow = _iso(next_day_start(now_dt))
    assert rows[1]["next_attempt_at"] == tomorrow == rows[2]["next_attempt_at"], \
        [dict(r) for r in rows]

    # Same local day, later (now_dt is 18:00 local): the cap is spent, nothing goes out.
    got.clear()
    assert drain(conn, limit=1, now_dt=now_dt + timedelta(minutes=30),
                 send_fn=lambda body, channel="feishu": got.append((channel, body))) \
        == {"sent": 0, "throttled": 0, "failed": 0, "retrying": 0}
    assert got == [], got

    # Next day: throttled rows come back on their own and the budget is fresh.
    stats = drain(conn, limit=1, now_dt=now_dt + timedelta(days=1),
                  send_fn=lambda body, channel="feishu": got.append((channel, body)))
    assert stats["sent"] == 1 and got == [("feishu", "第2条")], (stats, got)

    # The default cap is 20/day.
    saved = DAILY_LIMIT
    try:
        DAILY_LIMIT = 20
        conn = db.connect(":memory:")
        for _ in range(25):
            db.add_delivery(conn, None, "刷屏测试")
        stats = drain(conn, now_dt=now_dt, send_fn=lambda body, channel="feishu": None)
        assert (stats["sent"], stats["throttled"]) == (20, 5), stats
    finally:
        DAILY_LIMIT = saved

    # 3. Backoff is exponential and capped (asserting the formula, not the
    # default: WICK_BACKOFF_BASE may legitimately be tuned from the environment).
    assert backoff_delay(1) == BACKOFF_BASE, backoff_delay(1)
    assert backoff_delay(2) == min(BACKOFF_BASE * 2, BACKOFF_CAP), backoff_delay(2)
    assert backoff_delay(3) == min(BACKOFF_BASE * 4, BACKOFF_CAP), backoff_delay(3)
    assert backoff_delay(50) == BACKOFF_CAP, backoff_delay(50)
    # Untuned defaults are the numbers the roadmap promises.
    if "WICK_BACKOFF_BASE" not in os.environ:
        assert BACKOFF_BASE == 60
    if "WICK_DAILY_LIMIT" not in os.environ:
        assert DAILY_LIMIT == 20

    # 4. Failing send: retry with backoff, then give up as `failed` (terminal).
    conn = db.connect(":memory:")
    db.add_delivery(conn, None, "会一直失败")

    def boom(body, channel="feishu"):
        raise RuntimeError("模拟投递失败")

    now = now_dt
    stats = drain(conn, limit=20, now_dt=now, send_fn=boom)
    row = conn.execute("SELECT status, attempts, next_attempt_at FROM deliveries").fetchone()
    assert stats["retrying"] == 1 and stats["failed"] == 0, stats
    assert row["status"] == "queued" and row["attempts"] == 1, dict(row)
    assert row["next_attempt_at"] == _iso(now + timedelta(seconds=BACKOFF_BASE)), dict(row)

    # Not due yet: untouched, no extra attempt burned (half the backoff, so this
    # holds whatever WICK_BACKOFF_BASE is set to).
    assert drain(conn, limit=20, now_dt=now + timedelta(seconds=BACKOFF_BASE / 2), send_fn=boom) \
        == {"sent": 0, "throttled": 0, "failed": 0, "retrying": 0}
    assert conn.execute("SELECT attempts FROM deliveries").fetchone()["attempts"] == 1

    for n in range(2, MAX_ATTEMPTS):
        now = now + timedelta(seconds=backoff_delay(n - 1))
        assert drain(conn, limit=20, now_dt=now, send_fn=boom)["retrying"] == 1, n
        row = conn.execute("SELECT status, attempts, next_attempt_at FROM deliveries").fetchone()
        assert row["status"] == "queued" and row["attempts"] == n, (n, dict(row))
        assert row["next_attempt_at"] == _iso(now + timedelta(seconds=backoff_delay(n))), \
            (n, dict(row))

    now = now + timedelta(seconds=backoff_delay(MAX_ATTEMPTS - 1))
    stats = drain(conn, limit=20, now_dt=now, send_fn=boom)
    row = conn.execute("SELECT status, attempts FROM deliveries").fetchone()
    assert stats["failed"] == 1, stats
    assert row["status"] == "failed" and row["attempts"] == MAX_ATTEMPTS, dict(row)
    # Terminal means terminal: even long after, a failed row is not picked up again.
    assert drain(conn, limit=20, now_dt=now + timedelta(days=30), send_fn=boom) \
        == {"sent": 0, "throttled": 0, "failed": 0, "retrying": 0}

    # 5. Illegal transitions are refused, and so is a missing row.
    conn = db.connect(":memory:")
    did = db.add_delivery(conn, None, "终态测试")
    transition(conn, did, "sent", sent_at=_iso(now_dt))
    for bad in ("queued", "throttled", "failed"):
        try:
            transition(conn, did, bad)
        except ValueError as e:
            assert "非法跃迁" in str(e), e
        else:
            raise AssertionError(f"sent → {bad} 应该被拒绝")
    try:
        transition(conn, 9999, "sent")
    except ValueError:
        pass
    else:
        raise AssertionError("不存在的投递应该报错")

    # 6. The real subprocess path, with a stand-in command instead of Feishu.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        py = str(sys.executable).replace("\\", "/")
        rec = (tmp / "rec.py").resolve().as_posix()
        out = (tmp / "got.txt").resolve().as_posix()
        (tmp / "rec.py").write_text(
            "import sys, pathlib\n"
            "pathlib.Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')\n",
            encoding="utf-8",
        )
        send("真实投递内容", cmd=f'"{py}" "{rec}" "{out}"')
        assert Path(out).read_text(encoding="utf-8") == "真实投递内容"

        bad = (tmp / "bad.py").resolve().as_posix()
        (tmp / "bad.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
        try:
            send("x", cmd=f'"{py}" "{bad}"')
        except RuntimeError as e:
            assert "投递失败" in str(e) and "3" in str(e), e
        else:
            raise AssertionError("非零退出应该抛错")

    # The default command is the one the roadmap promises.
    assert DEFAULT_SEND.format(channel="feishu") == "hermes send -t feishu"

    print("outbox.py selftest OK: 真实调用 / 日上限边界 / 指数退避 / 终态不再重试 / 非法跃迁")
    return 0


def main() -> int:
    if "--drain" in sys.argv[1:]:
        import db
        conn = db.connect()
        try:
            print(f"投递: {drain(conn)}")
        finally:
            conn.close()
        return 0
    return _selftest()


if __name__ == "__main__":
    raise SystemExit(main())
