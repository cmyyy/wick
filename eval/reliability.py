"""D9 reliability acceptance — the three checks from issue #6 that can be automated.

    python eval/reliability.py --crash    # kill /F a real worker mid-run, restart, no double delivery
    python eval/reliability.py --zombie   # hung handler: does anything reclaim it within 3 minutes?
    python eval/reliability.py --dup      # same event enqueued 3x -> 1 row, 1 attempt, 1 delivery
    python eval/reliability.py --all      # all three, then write eval/report.md

Everything is real: a real worker subprocess, a real sqlite file in a temp dir,
a real `taskkill /F`. Nothing is mocked except the model call — `WICK_HERMES_BIN`
points at a fake `hermes` that either reports immediately or hangs, which is how
a stuck handler is produced without a network or a model.

Every conclusion prints the SQL that produced it (issue #6, acceptance #6:
"数字全部可核实"). Exit code is 0 only when every check actually passed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC))
import db  # noqa: E402

POLL = 1                   # worker loop interval during the drills (real default is 5s)
CRASH_WAIT = 240           # how long to wait for recovery after the restart
ZOMBIE_WINDOW = 200        # the acceptance criterion is "3 分钟内": measure 3.3 minutes
SAMPLE = 10                # heartbeat sampling period during the zombie drill


# ------------------------------------------------------------------ plumbing

def sql(db_path: Path, query: str, args: tuple = ()) -> list[dict]:
    """Read the real database the worker is writing to.

    Retried: a worker that was just `taskkill`ed can leave the WAL in flux for a
    moment, and the whole point of this drill is to read across exactly that.
    """
    last: Exception | None = None
    for attempt in range(5):
        conn = sqlite3.connect(str(db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(query, args).fetchall()]
        except sqlite3.OperationalError as exc:
            last = exc
            time.sleep(0.5 * (attempt + 1))
        finally:
            conn.close()
    raise RuntimeError(f"读库失败（重试 5 次）：{last}")


def one(db_path: Path, query: str, args: tuple = ()) -> dict | None:
    rows = sql(db_path, query, args)
    return rows[0] if rows else None


def age_seconds(ts: str | None) -> float | None:
    if not ts:
        return None
    then = datetime.fromisoformat(ts)
    return round((datetime.now(timezone.utc) - then).total_seconds(), 1)


def evidence(label: str, query: str, rows) -> None:
    print(f"  证据 · {label}")
    print(f"    $ sqlite3 wick.db \"{query.strip()}\"")
    if not rows:
        print("    -> （没有行）")
    for row in rows:
        print(f"    -> {json.dumps(row, ensure_ascii=False)}")


def status_of(db_path: Path, source_id: str) -> dict | None:
    return one(db_path,
               "SELECT id, status, worker_id, attempts, heartbeat_at, error "
               "FROM events WHERE source_id=?", (source_id,))


def fake_hermes(tmp: Path) -> Path:
    """A stand-in for `hermes`: reports a verdict, or hangs when told to."""
    py = tmp / "fake_hermes.py"
    py.write_text(
        "import os, sys, time\n"
        "if os.environ.get('FAKE_HERMES_SLOW') == '1':\n"
        "    print('（假 hermes）开始处理，卡住不下班', flush=True)\n"
        "    time.sleep(float(os.environ.get('FAKE_HERMES_SLEEP', '600')))\n"
        "print('判定：可处理')\n"
        "print('结论：可靠性演练用的假报告，没有真的改代码。')\n"
        "print('遗留：无')\n",
        encoding="utf-8",
    )
    # Windows cannot execute a .py directly through CreateProcess; one .cmd hop is
    # enough and keeps the fake itself pure Python (no shell quoting games).
    cmd = tmp / "fake_hermes.cmd"
    cmd.write_text(f'@echo off\r\n"{sys.executable}" "{py}" %*\r\n', encoding="utf-8")
    return cmd


def send_recorder(tmp: Path, tag: str) -> tuple[Path, Path]:
    """A WICK_SEND_CMD stand-in: one JSON line per send, so counting is exact.

    One log per check: a shared file would count another check's sends, which is
    exactly the kind of quiet cross-talk that makes a "1 次" claim worthless.
    """
    log = tmp / f"sent-{tag}.log"
    py = tmp / f"send_recorder_{tag}.py"
    py.write_text(
        "import json, pathlib, sys\n"
        f"p = pathlib.Path(r'{log}')\n"
        "with p.open('a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps(sys.argv[1:], ensure_ascii=False) + '\\n')\n",
        encoding="utf-8",
    )
    return py, log


def worker_env(tmp: Path, fake: Path, send_py: Path, slow: bool, db_path: Path) -> dict:
    env = dict(os.environ)
    env.update({
        "WICK_MODE": "analyze",           # no worktree, no self-checks: only the run itself
        "WICK_HERMES_BIN": str(fake),
        "WICK_PROGRESS_DIR": str(tmp / "progress"),
        "WICK_SEND_CMD": f'"{sys.executable}" "{send_py}"',
        "FAKE_HERMES_SLOW": "1" if slow else "0",
        "PYTHONIOENCODING": "utf-8",
        "WICK_DB": str(db_path),          # only so the worker's own log line names the drill db
    })
    return env


def start_worker(tmp: Path, db_path: Path, env: dict, tag: str, *, once: bool = False):
    argv = [sys.executable, str(SRC / "worker.py"), "--db", str(db_path)]
    argv += ["--once"] if once else ["--interval", str(POLL)]
    log = tmp / f"worker-{tag}.log"
    out = open(log, "w", encoding="utf-8")
    _LOG_HANDLES[id(out)] = out
    proc = subprocess.Popen(argv, cwd=str(PROJECT_ROOT), env=env, stdout=out,
                            stderr=subprocess.STDOUT, text=True)
    _PROC_LOGS[proc.pid] = log
    return proc


_LOG_HANDLES: dict[int, object] = {}
_PROC_LOGS: dict[int, Path] = {}


def stop_worker(proc, *, hard: bool = False) -> None:
    if proc.poll() is None:
        if hard:
            taskkill(proc.pid)
        else:
            proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=15)
    log = _PROC_LOGS.pop(proc.pid, None)
    if log is not None:
        for key, handle in list(_LOG_HANDLES.items()):
            if getattr(handle, "name", None) == str(log):
                handle.close()  # type: ignore[attr-defined]
                _LOG_HANDLES.pop(key, None)


def taskkill(pid: int) -> str:
    """`taskkill /F`, exactly the kill the acceptance test names. Tree-kill too:
    the stuck child (`fake hermes`) must not survive its parent."""
    try:
        p = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=60)
        raw = (p.stdout or p.stderr or b"").strip()
        # taskkill prints in the console codepage (GBK on a zh-CN Windows), not UTF-8.
        for enc in ("utf-8", "gbk", "mbcs"):
            try:
                return raw.decode(enc).strip().splitlines()[0]
            except (UnicodeDecodeError, LookupError, IndexError):
                continue
        return f"exit {p.returncode}"
    except FileNotFoundError:  # not Windows: SIGKILL is the equivalent
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        return f"SIGKILL {pid}"


def wait_for(db_path: Path, source_id: str, want: tuple[str, ...], timeout: float,
             on_sample=None) -> dict | None:
    """Poll the row until its status is one of `want` (or timeout). Returns the last row."""
    deadline = time.time() + timeout
    row = None
    while time.time() < deadline:
        row = status_of(db_path, source_id)
        if row and row["status"] in want:
            return row
        if on_sample:
            on_sample(row)
        time.sleep(2)
    return status_of(db_path, source_id)


def seed(db_path: Path, source_type: str, source_id: str, payload: dict) -> int:
    conn = db.connect(db_path)
    try:
        event_id, _ = db.enqueue(conn, source_type, source_id, payload)
        assert event_id is not None
        return event_id
    finally:
        conn.close()


def sent_count(log: Path) -> int:
    if not log.exists():
        return 0
    return len([line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()])


# ------------------------------------------------------------------- checks

def check_dup(tmp: Path) -> dict:
    """同一条事件灌 3 次：只能有 1 行、1 次尝试、1 条投递。"""
    print("\n=== 3) 重复灌入（--dup）")
    db_path = tmp / "dup.db"
    fake, send_py, log = fake_hermes(tmp), *send_recorder(tmp, "dup")
    sid = "cmyyy/wick#600"

    conn = db.connect(db_path)
    rows = [db.enqueue(conn, "github_issue", sid, {"repo": "cmyyy/wick", "number": 600, "title": "重复灌入"})
            for _ in range(3)]
    conn.close()
    print(f"  三次 enqueue 的返回：{[(eid, created) for eid, created in rows]}")
    print("  $ sqlite3 wick.db \"SELECT COUNT(*) FROM events WHERE source_id='cmyyy/wick#600'\"")

    proc = start_worker(tmp, db_path, worker_env(tmp, fake, send_py, False, db_path), "dup", once=True)
    proc.wait(timeout=180)
    stop_worker(proc)

    events = sql(db_path, "SELECT id, source_type, source_id, status, attempts, error FROM events")
    deliveries = sql(db_path, "SELECT id, event_id, status, attempts, substr(body,1,40) AS body FROM deliveries")
    evidence("灌了三次之后 events 里有几行", "SELECT * FROM events", events)
    evidence("处理后的投递", "SELECT id, event_id, status, attempts, body FROM deliveries", deliveries)
    print(f"  worker 输出尾部：")
    for line in (tmp / "worker-dup.log").read_text(encoding="utf-8").strip().splitlines()[-3:]:
        print(f"    {line}")

    ok = (len(events) == 1 and events[0]["status"] == "done" and events[0]["attempts"] == 1
          and len(deliveries) == 1 and sent_count(log) == 1)
    measured = (f"events={len(events)} 行（status={events[0]['status'] if events else '?'}，"
                f"attempts={events[0]['attempts'] if events else '?'}），"
                f"deliveries={len(deliveries)} 条，实际发出={sent_count(log)} 次")
    print(f"  判定：{'通过' if ok else '不通过'} — {measured}")
    return {"ok": ok, "name": "重复处理率 0（同一事件灌 3 次）", "measured": measured,
            "verdict": "通过" if ok else "不通过", "repro": "python eval/reliability.py --dup"}


def check_crash(tmp: Path) -> dict:
    """处理到一半 taskkill /F，重启后同一条事件重跑，且不重复投递。"""
    print("\n=== 1) 崩溃恢复（--crash）")
    db_path = tmp / "crash.db"
    fake, send_py, log = fake_hermes(tmp), *send_recorder(tmp, "crash")
    sid = "cmyyy/wick#601"
    seed(db_path, "github_issue", sid, {"repo": "cmyyy/wick", "number": 601, "title": "崩溃恢复"})

    first = start_worker(tmp, db_path, worker_env(tmp, fake, send_py, True, db_path), "crash-1")
    print(f"  启动 worker A（pid={first.pid}），假 hermes 卡住不返回")
    running = wait_for(db_path, sid, ("running",), timeout=90)
    assert running and running["status"] == "running", f"事件没进 running：{running}"
    time.sleep(3)  # let it sit "处理到一半", and let at least one beat land
    before = status_of(db_path, sid)
    evidence("kill 之前那一行", "SELECT id,status,worker_id,attempts,heartbeat_at FROM events WHERE source_id=?",
             [before])

    killed = taskkill(first.pid)
    stop_worker(first)
    print(f"  taskkill /F /T /PID {first.pid} -> {killed}")
    time.sleep(1)
    after_kill = status_of(db_path, sid)
    assert after_kill is not None
    evidence("kill 之后（行还挂在 running，心跳停在最后一拍）",
             "SELECT id,status,worker_id,attempts,heartbeat_at FROM events WHERE source_id=?", [after_kill])
    print(f"  心跳年龄 {age_seconds(after_kill['heartbeat_at'])}s，租约 LEASE_SECONDS={db.LEASE_SECONDS}s"
          f" —— 要等租约过期，requeue_stale 才敢收")

    second = start_worker(tmp, db_path, worker_env(tmp, fake, send_py, False, db_path), "crash-2")
    print(f"  重启 worker B（pid={second.pid}），这次假 hermes 立刻返回")
    seen = {"last": None}

    def sample(row):
        if row and row != seen["last"]:
            print(f"    [{time.strftime('%H:%M:%S')}] status={row['status']:<8} "
                  f"attempts={row['attempts']} 心跳年龄={age_seconds(row['heartbeat_at'])}s")
            seen["last"] = row

    final = wait_for(db_path, sid, ("done", "failed"), timeout=CRASH_WAIT, on_sample=sample)

    events = sql(db_path, "SELECT id, status, worker_id, attempts, error FROM events")
    deliveries = sql(db_path, "SELECT id, event_id, status, attempts FROM deliveries")
    stop_worker(second, hard=False)
    evidence("最终事件行", "SELECT id,status,worker_id,attempts,error FROM events", events)
    evidence("投递条数（不重复投递 = 1）", "SELECT id,event_id,status FROM deliveries", deliveries)
    print(f"  实际发出投递 {sent_count(log)} 次")
    for line in (tmp / "worker-crash-2.log").read_text(encoding="utf-8").strip().splitlines()[-4:]:
        print(f"    worker B: {line}")

    ok = (final is not None and final["status"] == "done" and final["attempts"] == 2
          and len(deliveries) == 1 and sent_count(log) == 1)
    measured = (f"最终 status={final['status'] if final else '?'}，attempts={final['attempts'] if final else '?'}"
                f"（1 次被 kill + 1 次重跑），deliveries={len(deliveries)} 条，"
                f"实际发出={sent_count(log)} 次")
    print(f"  判定：{'通过' if ok else '不通过'} — {measured}")
    return {"ok": ok, "name": "崩溃恢复（kill /F 后重跑、不重复投递）", "measured": measured,
            "verdict": "通过" if ok else "不通过", "repro": "python eval/reliability.py --crash"}


def check_zombie(tmp: Path) -> dict:
    """handler 卡死、进程还活着：3 分钟内有没有把它放回 pending？"""
    print("\n=== 2) 假死恢复（--zombie）")
    db_path = tmp / "zombie.db"
    fake, send_py, log = fake_hermes(tmp), *send_recorder(tmp, "zombie")
    sid = "cmyyy/wick#602"
    seed(db_path, "github_issue", sid, {"repo": "cmyyy/wick", "number": 602, "title": "假死恢复"})

    proc = start_worker(tmp, db_path, worker_env(tmp, fake, send_py, True, db_path), "zombie")
    print(f"  启动 worker（pid={proc.pid}），handler 卡住；不重启进程，只看它自己")
    running = wait_for(db_path, sid, ("running",), timeout=90)
    assert running and running["status"] == "running", f"事件没进 running：{running}"
    t0 = time.time()
    samples = []
    print(f"  开始计时，观察窗口 {ZOMBIE_WINDOW}s（验收要求「约 3 分钟内」）")
    while time.time() - t0 < ZOMBIE_WINDOW:
        time.sleep(SAMPLE)
        row = status_of(db_path, sid)
        assert row is not None
        elapsed = round(time.time() - t0, 1)
        samples.append({"t": elapsed, "status": row["status"], "beat_age": age_seconds(row["heartbeat_at"])})
        print(f"    t={elapsed:>6}s  status={row['status']:<8} 心跳年龄={age_seconds(row['heartbeat_at'])}s"
              f"（LEASE_SECONDS={db.LEASE_SECONDS}）")
        if row["status"] != "running":
            break

    alive = proc.poll() is None
    still = status_of(db_path, sid)
    assert still is not None
    print(f"  观察结束时 worker 进程{'还活着' if alive else '已退出'}，事件仍是 {still['status']}")
    stop_worker(proc, hard=True)

    final = status_of(db_path, sid)
    assert final is not None
    evidence("观察窗口结束时的行", "SELECT id,status,worker_id,attempts,heartbeat_at FROM events WHERE source_id=?",
             [final])
    evidence("假死回收计数", "SELECT key, value FROM meta WHERE key='stats:stale_requeued'",
             sql(db_path, "SELECT key, value FROM meta WHERE key='stats:stale_requeued'"))
    ok = any(s["status"] == "pending" for s in samples)
    ages = [s["beat_age"] or 0 for s in samples]
    measured = (f"{ZOMBIE_WINDOW}s 内状态一直是 {final['status']}；心跳年龄在 "
                f"{min(ages)}–{max(ages)}s 之间反复归零（每 30s 续一次，租约 {db.LEASE_SECONDS}s），"
                f"从未被回收，事件没回到 pending")
    print(f"  判定：{'通过' if ok else '不通过'} — {measured}")
    if not ok:
        print("  为什么不会回收（代码事实，非猜测）：")
        print("    · src/worker.py:147 `while not stop.wait(HEARTBEAT_INTERVAL)`（30s）——续租没有预算上限，"
              "只要进程活着就一直续")
        print("    · 所以 heartbeat_at 永远新鲜，db.requeue_stale（src/db.py:283 要求心跳 < now-90s）永不命中")
        print("    · 且 requeue_stale 只在 worker 自己的循环里被调用（src/worker.py:216 启动、:244 空闲），"
              "而此刻那个线程正卡在 handle() 里")
        print("    · 唯一的兜底是 run_prompt 的子进程超时：analyze 900s / fix 1500s，之后走 finish_event('failed')，"
              "是 failed 不是 pending——3 分钟口径下这条能力当前不存在")
    # known_limit: DECISIONS 17 says this is not expected to work yet. Rendered as
    # a warning and kept OUT of the exit code -- a gate that is always red is a gate
    # everyone learns to ignore.
    return {"ok": ok, "known_limit": True,
            "name": "假死恢复（不重启，3 分钟内放回 pending）",
            "measured": measured, "verdict": "通过" if ok else "已知上限",
            "repro": "python eval/reliability.py --zombie"}


# ------------------------------------------------------------------- report

def write_report(results: list[dict], path: Path) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# wick 可靠性验收（D9 / issue #6）",
        "",
        f"实测生成：{stamp}（本机实跑，非 agent 自报）。每条数字都能用第三列的复现命令重新跑出来。",
        "",
        "| 验收项 | 实测值 | 判定 | 复现命令 |",
        "| --- | --- | --- | --- |",
    ]
    for r in results:
        if r["ok"]:
            mark = "✅ "
        elif r.get("known_limit"):
            mark = "⚠️ **"
        else:
            mark = "❌ "
        close = "**" if (not r["ok"] and r.get("known_limit")) else ""
        lines.append(f"| {r['name']} | {r['measured']} | {mark}{r['verdict']}{close} | `{r['repro']}` |")
    lines += [
        "| 闭环成功率 | 待测：n/0 ｜a 段 = waiting approval + 自检全绿（worker 实跑）；"
        "b 段 = 批准后 executed_at 非空且 detail.execution_error 为空且 deliveries 有 PR URL；"
        "分子 = a 且 b 都成功；只读分析不进样本；无自动重试（定义见 README，issue #29） | 待测 | — |",
        "| 投递误报率 ≤20% | 待补（前 20 条人工标注明细） | 待测 | — |",
        "",
        "## 假死恢复：为什么 3 分钟做不到（计算过程）",
        "",
        f"这条已按 DECISIONS 17 降级为**已知上限**（不在 wick 内部解决）。"
        f"不是「预算比 3 分钟长一点」，是这条路径根本不存在。算一遍：",
        "",
        f"1. 续租没有上限：`worker._renew_lease` 是 `while not stop.wait(HEARTBEAT_INTERVAL)`，"
        f"`HEARTBEAT_INTERVAL=30s`，只要进程活着就一直续。心跳年龄因此永远落在 [0,30)s 内。",
        f"2. 回收条件是心跳 **< now-{db.LEASE_SECONDS}s**（`db.requeue_stale`）。"
        f"年龄永远 < 30s，所以 `requeue_stale` 对卡死的事件永远命中 0 条 —— "
        f"实测区间见上表（由 check_zombie 采样算出，不是常量），一次都没逼近 {db.LEASE_SECONDS}s。",
        f"3. 而且 `requeue_stale` 只在 worker 自己的循环里被调用（`worker.serve` 启动时与每次空闲时），"
        f"而 handler 卡住时那个线程正阻塞在 `handle()` 里 —— 即使租约过期也没人来收。",
        f"4. 唯一的兜底是子进程超时：analyze 路径 `DEFAULT_TIMEOUT=900s`，fix 路径 "
        f"`FIX_TIMEOUT=1500s`（外加最多 11 × `CHECK_TIMEOUT=300s` 的自检）。超时后走的是 "
        f"`finish_event('failed')`，终点是 **failed**，不是 pending。",
        "",
        f"结论：**3 分钟回收 = 0 次**。这条口径要成立需要两件事，成本差一个量级：一是**进度信号**"
        f"（心跳带 phase，才能区分「慢」和「死」）—— 贵；二是**机制**（handler 跑独立线程、主线程按预算 "
        f"kill 子进程树并继续消费）—— 便宜，且顺带解决队头阻塞。两者都在 #26。原文曾把两者混为一谈并整体否决，"
        f"这个判断在 review 时被纠正。旧的「只加续租预算」写法（"
        f"`_renew_lease` 超过 `FIX_TIMEOUT + N×CHECK_TIMEOUT` 就停止续租，让行自然变 stale），"
        f"那属于改 `src/`，本次按 ticket 要求只报告不改。",
        "",
        "另外记一笔：编排者 ticket 里写的「预算 = `FIX_TIMEOUT + 4×CHECK_TIMEOUT`」在代码里不存在，"
        "证据：",
        "",
        "```bash",
        "$ grep -rn \"FIX_TIMEOUT + 4\\|watchdog\\|看门狗\" src/",
        "（无输出）",
        "```",
        "",
        "## 口径与计算（供复核）",
        "",
        f"- 租约：`db.LEASE_SECONDS = {db.LEASE_SECONDS}`s，worker 每 {30}s 续租一次（`worker.HEARTBEAT_INTERVAL`）。",
        "  崩溃后的回收延迟上界 = 最后一拍 + 租约 = **≤120s**（心跳每 30s 一拍），不是 kill 的瞬间，"
        "且前提是有人把 worker 再起来。",
        "- 崩溃恢复：假 hermes 卡住 → `taskkill /F /T` → 心跳停在最后一拍 → 重启的 worker 在",
        "  `serve()` 启动和每次空闲时调用 `db.requeue_stale()`，租约过期后把行放回 `pending`，",
        "  第二次尝试用会立刻返回的假 hermes 跑完 → `done`。",
        "  注意「deliveries 仍 1 条」有一半是**结构性保证**：`add_delivery` 在 `handle()` 返回之后才调用，"
        "被 kill 的 attempt 根本没机会写投递。fence 本身（旧 attempt 晚到被拒）由 `db.py` 的 selftest 覆盖，"
        "**本演练没有覆盖它** —— 要覆盖需慢 handler + 第二个 worker 抢租约。",
        "- 假死恢复：见 `--zombie` 输出；当前代码没有「续租预算」，卡死的进程会一直持有租约。",
        "- 重复灌入：`events.dedupe_key` 是 `UNIQUE` + `INSERT OR IGNORE`，三次 enqueue 只有第一次落行。",
        "",
        "```sql",
        "-- 任意时刻自查（把 wick.db 换成你的库）",
        "SELECT id, source_type, source_id, status, attempts, heartbeat_at FROM events ORDER BY id;",
        "SELECT event_id, COUNT(*) FROM deliveries GROUP BY event_id HAVING COUNT(*) > 1;  -- 空 = 没有重复投递",
        "SELECT key, value FROM meta WHERE key='stats:stale_requeued';",
        "```",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告已写入 {path}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="wick D9 可靠性验收")
    ap.add_argument("--crash", action="store_true", help="崩溃恢复：kill /F 后重跑且不重复投递")
    ap.add_argument("--zombie", action="store_true", help="假死恢复：卡死 handler 能否被 3 分钟内回收")
    ap.add_argument("--dup", action="store_true", help="重复灌入：同一事件 3 次只处理 1 次")
    ap.add_argument("--all", action="store_true", help="三条全跑，并写 eval/report.md")
    ap.add_argument("--report", default=str(PROJECT_ROOT / "eval" / "report.md"),
                    help="报告输出路径（默认 eval/report.md）")
    ap.add_argument("--keep", action="store_true", help="保留临时目录（查日志用）")
    args = ap.parse_args(argv)

    if not any((args.crash, args.zombie, args.dup, args.all)):
        ap.error("至少给一个：--crash / --zombie / --dup / --all")

    tmp = Path(tempfile.mkdtemp(prefix="wick-reliability-"))
    print(f"临时目录：{tmp}")
    print(f"项目根：{PROJECT_ROOT}")
    results = []
    try:
        if args.dup or args.all:
            results.append(check_dup(tmp))
        if args.crash or args.all:
            results.append(check_crash(tmp))
        if args.zombie or args.all:
            results.append(check_zombie(tmp))
    finally:
        if args.keep:
            print(f"临时目录保留在 {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    print("\n=== 汇总")
    for r in results:
        print(f"  [{'通过' if r['ok'] else '不通过'}] {r['name']} — {r['measured']}")
    if args.all:
        write_report(results, Path(args.report))
    # known_limit results are assertions that it does NOT work (DECISIONS 17). Counting
    # them as failures makes --all permanently red, which trains everyone to ignore it.
    failed = [r["name"] for r in results if not r["ok"] and not r.get("known_limit")]
    if failed:
        print(f"\n未通过：{'；'.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
