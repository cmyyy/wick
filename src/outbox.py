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

# A claimed row is hidden from other drains for this long: long enough for one
# send (SEND_TIMEOUT) and short enough that a crashed drain's row is retried
# later instead of being stuck forever.
# ponytail: a lease instead of a `sending` status -- a new status would need a
# CHECK-constraint migration, and the lease also recovers crashed drains for free.
CLAIM_LEASE = 600

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

def _argv(raw: str) -> list[str]:
    """Split a command string into argv.

    On Windows shlex's POSIX rules eat the backslashes in `C:\\Tools\\send.cmd`,
    so use its non-POSIX mode there and strip the quotes that mode keeps.
    """
    if os.name != "nt":
        return shlex.split(raw)
    return [a[1:-1] if len(a) > 1 and a[0] == a[-1] and a[0] in "\"'" else a
            for a in shlex.split(raw, posix=False)]


def resolve_target(channel: str) -> str:
    """Turn a bare platform name into a target `hermes send` accepts.

    `hermes send -t feishu` prints "No home channel set" and still exits 0, so a
    bare platform name is a silent black hole: the outbox would retry forever and
    never say why. Precedence: an explicit `<platform>:<chat_id>`, then
    WICK_SEND_TARGET, then `<PLATFORM>_HOME_CHANNEL`, then a loud error.
    """
    if ":" in channel:
        return channel
    for env in ("WICK_SEND_TARGET", f"{channel.upper()}_HOME_CHANNEL"):
        value = os.environ.get(env)
        if not value:
            continue
        return value if ":" in value else f"{channel}:{value}"
    raise RuntimeError(
        f"投递目标不完整：{channel!r} 缺 chat_id。三种修法任选一种——"
        f"给 deliveries.channel 写完整目标（如 {channel}:oc_xxx）、"
        f"设 WICK_SEND_TARGET、或设 {channel.upper()}_HOME_CHANNEL。"
        f"不设的话 hermes send 会静默什么都不发（退出码仍是 0）。"
    )


def send(body: str, channel: str = "feishu", cmd: str | None = None) -> str:
    """Deliver one message. Raises RuntimeError on a non-zero exit or a timeout."""
    # ponytail: the body rides in argv, so a report over ~32k chars hits the
    # Windows command-line limit and fails loudly. Switch to `-f -` plus stdin
    # if that ever happens for real; silently truncating a report is worse.
    raw = cmd or os.environ.get("WICK_SEND_CMD")
    if not raw:  # a custom command supplies its own target
        raw = DEFAULT_SEND.format(channel=resolve_target(channel))
    argv = _argv(raw) + [body]
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
    """Move one delivery, refusing any transition the state machine forbids.

    The UPDATE carries `AND status=<the status we validated>`, so a row another
    process moved in between is reported instead of silently overwritten.
    """
    row = conn.execute("SELECT status, attempts FROM deliveries WHERE id=?",
                       (delivery_id,)).fetchone()
    if row is None:
        raise ValueError(f"投递 {delivery_id} 不存在")
    if to_status not in ALLOWED[row["status"]]:
        raise ValueError(f"投递 {delivery_id} 非法跃迁：{row['status']} → {to_status}")
    cur = conn.execute(
        "UPDATE deliveries SET status=?, attempts=?, next_attempt_at=?, sent_at=? "
        "WHERE id=? AND status=?",
        (to_status, row["attempts"] if attempts is None else attempts,
         next_attempt_at, sent_at, delivery_id, row["status"]),
    )
    conn.commit()
    if cur.rowcount != 1:
        raise ValueError(f"投递 {delivery_id} 已被其他进程改动，"
                         f"{row['status']} → {to_status} 未生效")


def claim(conn: sqlite3.Connection, delivery_id: int, now: datetime,
          lease: int = CLAIM_LEASE) -> sqlite3.Row | None:
    """Reserve one due delivery for this drain; None if another drain got it.

    A single UPDATE is atomic under SQLite's write lock: whoever pushes
    next_attempt_at out of the due window first wins, everyone else sees
    rowcount 0. The row keeps status `queued`, so a drain that dies mid-send
    leaves it to be retried once the lease expires.
    """
    row = conn.execute(
        "UPDATE deliveries SET next_attempt_at=? WHERE id=? AND status='queued' "
        "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) RETURNING *",
        (_iso(now + timedelta(seconds=lease)), delivery_id, _iso(now)),
    ).fetchone()
    conn.commit()
    return row


def sent_today(conn: sqlite3.Connection, ts: datetime | None = None) -> int:
    return conn.execute(
        "SELECT COUNT(*) c FROM deliveries WHERE status='sent' AND sent_at >= ?",
        (_iso(day_start(ts)),),
    ).fetchone()["c"]


def drain(conn: sqlite3.Connection, *, limit: int | None = None,
          now_dt: datetime | None = None, send_fn=None) -> dict:
    """Send every delivery that is due. Returns counts per outcome.

    Safe to run concurrently (resident worker plus a manual `--drain`): each row
    is claimed atomically before its body is sent, so two drains never send the
    same delivery and neither raises on a lost race.

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
        "SELECT id FROM deliveries WHERE status='queued' "
        "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY id", (now_s,),
    ).fetchall()
    for candidate in due:
        # Reserve before sending. A second drain (resident worker plus a manual
        # `--drain`) sees the same candidate list; only the claim winner sends,
        # so the body cannot go out twice and the loser never reaches a
        # `sent -> sent` transition.
        row = claim(conn, candidate["id"], now)
        if row is None:
            continue
        if sent_today(conn, now) >= limit:
            # ponytail: the cap is counted per drain, so two simultaneous drains
            # can overshoot it by up to one pass each. A hard cap needs a shared
            # counter row; not worth it for a channel throttle.
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
    #    Native paths on purpose (backslashes, and this temp dir contains a
    #    space): the command string must split without eating either.
    assert _argv(r'"C:\Program Files\send me.cmd" -t x') == \
        [r"C:\Program Files\send me.cmd", "-t", "x"], _argv(r'"C:\Program Files\send me.cmd" -t x')
    if os.name == "nt":  # POSIX rules would turn this into C:Toolssend.cmd
        assert _argv(r"C:\Tools\send.cmd --flag") == [r"C:\Tools\send.cmd", "--flag"], \
            _argv(r"C:\Tools\send.cmd --flag")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        py = str(sys.executable)
        rec = str((tmp / "rec.py").resolve())
        out = str((tmp / "got.txt").resolve())
        (tmp / "rec.py").write_text(
            "import sys, pathlib\n"
            "pathlib.Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')\n",
            encoding="utf-8",
        )
        send("真实投递内容", cmd=f'"{py}" "{rec}" "{out}"')
        assert Path(out).read_text(encoding="utf-8") == "真实投递内容"

        bad = str((tmp / "bad.py").resolve())
        (tmp / "bad.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
        try:
            send("x", cmd=f'"{py}" "{bad}"')
        except RuntimeError as e:
            assert "投递失败" in str(e) and "3" in str(e), e
        else:
            raise AssertionError("非零退出应该抛错")

    # The default command is the one the roadmap promises.
    assert DEFAULT_SEND.format(channel="feishu") == "hermes send -t feishu"

    # A bare platform name must never become a silent black hole: `hermes send
    # -t feishu` exits 0 while sending nothing, so a missing chat_id has to fail
    # loudly here instead of burning retries.
    assert resolve_target("feishu:oc_123") == "feishu:oc_123"
    saved = {k: os.environ.pop(k, None) for k in ("WICK_SEND_TARGET", "FEISHU_HOME_CHANNEL")}
    try:
        os.environ["WICK_SEND_TARGET"] = "feishu:oc_from_env"
        assert resolve_target("feishu") == "feishu:oc_from_env", "WICK_SEND_TARGET 未生效"

        os.environ.pop("WICK_SEND_TARGET")
        os.environ["FEISHU_HOME_CHANNEL"] = "oc_home"  # bare id must get the prefix
        assert resolve_target("feishu") == "feishu:oc_home", "HOME_CHANNEL 未补全前缀"

        os.environ.pop("FEISHU_HOME_CHANNEL")
        try:
            resolve_target("feishu")
        except RuntimeError as e:
            msg = str(e)
            assert "WICK_SEND_TARGET" in msg and "FEISHU_HOME_CHANNEL" in msg, msg
            assert "静默" in msg, "错误信息要说明不设会静默失败"
        else:
            raise AssertionError("缺 chat_id 时必须报错，不能静默重试")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # 7. Claiming: a reserved row is invisible to a second drain, and comes back
    #    by itself once the lease runs out (a drain that died mid-send).
    conn = db.connect(":memory:")
    did = db.add_delivery(conn, None, "占位测试")
    t0 = datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc)
    assert claim(conn, did, t0) is not None
    assert claim(conn, did, t0) is None, "已被占位的投递不能再被第二个人占位"
    assert claim(conn, did, t0 + timedelta(seconds=CLAIM_LEASE)) is not None, "租约到期应可重新占位"

    # 8. transition re-checks the status it validated: a row another process
    #    moved in between is reported, not silently overwritten.
    with tempfile.TemporaryDirectory() as td:
        conn_a = db.connect(Path(td) / "race.db")
        conn_b = db.connect(Path(td) / "race.db")
        try:
            did = db.add_delivery(conn_a, None, "并发跃迁")
            moved = {"done": False}

            def move_it_first(sql):
                # Runs just before conn_a's UPDATE: conn_b wins the race for real.
                if sql.startswith("UPDATE deliveries SET status") and not moved["done"]:
                    moved["done"] = True
                    conn_a.set_trace_callback(None)
                    conn_b.execute("UPDATE deliveries SET status='sent' WHERE id=?", (did,))
                    conn_b.commit()

            conn_a.set_trace_callback(move_it_first)
            try:
                transition(conn_a, did, "throttled")
            except ValueError as e:
                assert "已被其他进程改动" in str(e), e
            else:
                raise AssertionError("状态在半路被改掉时报错，而不是覆盖")
            conn_a.set_trace_callback(None)
            assert conn_a.execute("SELECT status FROM deliveries WHERE id=?",
                                  (did,)).fetchone()["status"] == "sent"
        finally:
            conn_a.set_trace_callback(None)
            conn_a.close()
            conn_b.close()

    # 9. Two drains at once over the same rows: every body goes out exactly once
    #    and nothing escapes either drain. Without the atomic claim both would
    #    send all five and the loser would die on `sent -> sent`.
    with tempfile.TemporaryDirectory() as td:
        import threading
        import time as _time

        db_path = str(Path(td) / "concurrent.db")
        seed = db.connect(db_path)  # schema and rows from this thread only
        for i in range(5):
            db.add_delivery(seed, None, f"并发第{i + 1}条")
        seed.close()
        sent_bodies: list[str] = []
        guard = threading.Lock()

        def slow_send(body, channel="feishu"):
            _time.sleep(0.02)  # widen the window the unclaimed SELECT raced through
            with guard:
                sent_bodies.append(body)

        gate = threading.Barrier(2)
        failures: list[BaseException] = []
        stats_by_drain: list[dict] = []

        def run():
            # A sqlite connection belongs to the thread that made it, so each
            # drain gets its own -- exactly like two real worker processes.
            conn = db.connect(db_path)
            try:
                gate.wait(10)  # start both drains on the same candidate list
                stats_by_drain.append(drain(conn, limit=20, send_fn=slow_send))
            except BaseException as exc:  # nothing may escape a drain
                failures.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert not failures, failures
        assert len(stats_by_drain) == 2, stats_by_drain  # both drains returned normally
        assert sorted(sent_bodies) == [f"并发第{i + 1}条" for i in range(5)], sent_bodies
        assert sum(s["sent"] for s in stats_by_drain) == 5, stats_by_drain
        conn = db.connect(db_path)
        rows = conn.execute("SELECT status, attempts FROM deliveries ORDER BY id").fetchall()
        assert [(r["status"], r["attempts"]) for r in rows] == [("sent", 1)] * 5, \
            [dict(r) for r in rows]
        conn.close()

    print("outbox.py selftest OK: 真实调用 / 日上限边界 / 指数退避 / 终态不再重试 / "
          "非法跃迁 / 原子占位 / 并发 drain 不重复")
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
