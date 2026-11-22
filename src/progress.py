"""Cross-run memory: one progress file per task, plus a one-line index.

A task that takes several runs (a big issue fix, an analysis that needs another
pass) must start where the last run stopped instead of re-reading everything
from zero. Two files carry that:

    <home>/progress/index.md       one line per IN-FLIGHT task: status + when
    <home>/progress/<task_id>.md   append-only log: verdict, files, checks, left

Every run loads the index and its own task log, never anyone else's log. A
finished task drops out of the index (status fix / blocked / skip); its file
stays, the history is not lost. A prompt gets the index plus the last
TASK_BLOCKS rounds of its own log, so what goes into a prompt is bounded by the
number of in-flight tasks and by TASK_BLOCKS -- not by how many rounds a task
has accumulated.

What a killed attempt leaves behind: nothing. The log is written when a run
RETURNS, so `kill -9` loses that round entirely. The recovery point is the last
COMPLETED round's record: a reclaimed attempt continues from there, not from
the moment it died.

The directory lives OUTSIDE the worktree on purpose: sandbox.py hands every
attempt its own throwaway tree, so progress written inside one would disappear
with it (that is exactly what cross-run memory must not do).

Writes are fenced: append() takes the claim (event id, attempts, worker_id) and
refuses to write once the lease has moved on. A reclaimed attempt that runs on
after its lease died would otherwise append a loser's verdict after the
winner's, and the next prompt would read it as the latest conclusion. The index
is read-modify-write, so it also refuses to overwrite a line whose timestamp is
newer than the one being written.

ponytail: plain files, no locking, one worker per inbox (see db.py). A kill in
the middle of a write can tear the append-only log; a torn or hand-edited index
line is dropped on the next read. Other tasks' lines survive unless the index
itself was truncated -- the index is rebuilt only from what still parses, so a
lost line stays lost until that task runs again.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INDEX_NAME = "index.md"

# `- issue-3: fix（2026-11-22T12:00:00）`; anything else in the file is ignored,
# so a hand-edited or half-written index degrades instead of raising. Group 3 is
# the timestamp, used to keep a late writer from clobbering a newer line.
INDEX_LINE = re.compile(r"^-\s+([^\s:]+):\s*([^\s（(]+)(?:[（(]([^）)]*)[）)])?")

# Verdicts from both prompt templates, shortened for the index line.
STATUS = {
    "已修复": "fix", "部分修复": "partial", "无法修复": "blocked",
    "可处理": "plan", "需补充信息": "need-info", "不处理": "skip",
}

# The index is the "what is in flight" line, not an archive: a task that reached
# a terminal status is removed from it (its own file keeps the history).
IN_FLIGHT = {"plan", "need-info", "partial", "run"}

TASK_BLOCKS = 6      # rounds of one task's own log injected into a prompt
REPORT_CHARS = 2000  # analysis report kept per round
PATCH_LINES = 100    # lines of the previous round's patch kept per round


def _dir(env: Mapping[str, str] | None = None) -> Path:
    """Resolved at import for PROGRESS_DIR, and callable for the selftest."""
    env = os.environ if env is None else env
    return Path(env.get("WICK_PROGRESS_DIR") or Path.home() / ".wick" / "progress")


PROGRESS_DIR = _dir()


def task_id(event) -> str:
    """`issue-<n>` for a GitHub issue, `event-<id>` for anything else.

    Pure: takes the event row, touches no disk. One task = one file is the
    whole mechanism, so this must be stable across runs and attempts.

    ponytail: no repo in the name -- wick polls one repo at a time (worker
    --repo), so issue numbers cannot collide. Sharing one DB across repos would
    make two `issue-4`s share a file; add the repo here when that happens.
    """
    payload = event["payload"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload or "{}")
        except ValueError:
            payload = {}
    number = (payload or {}).get("number")
    if event["source_type"] == "github_issue" and number is not None:
        return f"issue-{number}"
    return f"event-{event['id']}"


def path_for(task: str) -> Path:
    return PROGRESS_DIR / f"{task}.md"


def _read(path: Path) -> str:
    """File contents, or "" when missing/unreadable. Invalid bytes are replaced."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _index_lines(text: str) -> list[str]:
    """Only the lines that parse as index entries; junk never reaches a prompt."""
    return [ln for ln in text.splitlines() if INDEX_LINE.match(ln.strip())]


def parse_index(text: str) -> dict[str, str]:
    """task -> status word. Unparsable lines are skipped, never raised."""
    out = {}
    for line in text.splitlines():
        m = INDEX_LINE.match(line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def index_line(task: str, status: str, ts: str) -> str:
    return f"- {task}: {status}（{ts}）"


def _update_index(task: str, status: str, ts: str) -> None:
    """Replace this task's line in place, append it, or drop it when finished.

    Other lines are untouched. A line stamped later than `ts` wins: two attempts
    can be alive long enough to write out of order (SIGSTOP, DB lock), and the
    index must show the newest known state, not the last writer.
    """
    path = PROGRESS_DIR / INDEX_NAME
    lines = _index_lines(_read(path))
    mine = None
    for i, line in enumerate(lines):
        m = INDEX_LINE.match(line.strip())
        if m and m.group(1) == task:
            mine = (i, m.group(3) or "")
            break
    if mine and mine[1] > ts:
        return
    if status in IN_FLIGHT:
        if mine:
            lines[mine[0]] = index_line(task, status, ts)
        else:
            lines.append(index_line(task, status, ts))
    elif mine:
        del lines[mine[0]]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _owns(conn, guard: tuple[int, int, str]) -> bool:
    """True while `guard` (event_id, attempts, worker_id) still owns the event."""
    if conn is None:
        return False
    event_id, attempts, worker_id = guard
    try:
        row = conn.execute(
            "SELECT attempts, worker_id, status FROM events WHERE id=?", (event_id,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row["attempts"] == attempts and row["worker_id"] == worker_id
                and row["status"] == "running")


def _clip(text: str, limit: int = REPORT_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "\n…（报告已截断）"


def _patch_head(patch: str, lines: int = PATCH_LINES) -> str:
    rows = (patch or "").strip().splitlines()
    if len(rows) <= lines:
        return "\n".join(rows)
    return "\n".join(rows[:lines] + [f"…（patch 共 {len(rows)} 行，只留前 {lines} 行）"])


def _tail(body: str, keep: int = TASK_BLOCKS) -> str:
    """The log's last `keep` rounds; older blocks are replaced by one marker."""
    lines = body.splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.startswith("## ")]
    if len(starts) <= keep:
        return body
    return "\n".join([f"（更早的 {len(starts) - keep} 轮记录已省略）"] + lines[starts[-keep]:])


def append(task: str, *, verdict: str | None = None, files: list[str] | None = None,
           checks: str = "", leftover: str = "无", report: str = "", patch: str = "",
           ts: str | None = None, conn=None,
           guard: tuple[int, int, str] | None = None) -> Path | None:
    """Append one run's outcome to the task log and refresh its index line.

    Never overwrites history, never raises: recording a run must not be able to
    fail the run it is recording. Returns the file written, or None when the
    write was refused (the claim in `guard` is no longer ours) or impossible.

    `guard` is (event id, attempts, worker_id) from the claimed event. Without a
    conn it cannot be verified, so the write is refused rather than recorded
    unverified: a losing attempt's notes are worse than no notes.
    """
    if guard is not None and not _owns(conn, guard):
        return None

    ts = ts or _stamp()
    block = [f"## {ts}", f"- 判定：{verdict or '（未判定）'}"]
    files = files or []
    if files:
        shown = ", ".join(files[:8])
        if len(files) > 8:
            shown += f" 等 {len(files)} 个"
        block.append(f"- 改动：{shown}")
    if checks:
        block.append(f"- 自检：{checks}")
    block.append(f"- 遗留：{leftover or '无'}")
    if report:
        block.append("- 本轮报告（截断）：")
        block += [f"  {ln}" for ln in _clip(report).splitlines()]
    if patch:
        block.append(f"- 上一轮改动（patch 前 {PATCH_LINES} 行）：")
        block += [f"  {ln}" for ln in _patch_head(patch).splitlines()]

    path = path_for(task)
    try:
        PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
        fresh = not path.exists()
        with path.open("a", encoding="utf-8", errors="replace") as fh:
            if fresh:
                fh.write(f"# {task}\n\n")
            fh.write("\n".join(block) + "\n\n")
        _update_index(task, STATUS.get(verdict or "", "run"), ts)
    except OSError:
        return None
    return path


def load(task: str | None = None) -> tuple[str, str]:
    """(index_text, task_text) for a prompt: other tasks' lines, own last rounds.

    They are separate on purpose. The index says which OTHER tasks are in
    flight; it says nothing about this one. A first run therefore gets an empty
    task_text and the prompt's "first run" marker -- otherwise every new task
    would be told it is a continuation of work it never did.

    ("", "") when there is nothing to say or the files are unreadable.
    """
    index_text = "\n".join(_index_lines(_read(PROGRESS_DIR / INDEX_NAME)))
    task_text = ""
    if task:
        body = _read(path_for(task)).strip()
        if body:
            task_text = _tail(body)
    return index_text, task_text


# ---------------------------------------------------------------- selftest

def _selftest() -> int:
    """Self-check: id mapping / append keeps history / in-flight-only index /
    tail truncation / corrupt files degrade / fenced append / ts ordering."""
    import tempfile

    import db as dbmod

    global PROGRESS_DIR

    # 1. Task id is the file name, so it must be stable and never collide.
    ev = {"id": 12, "source_type": "github_issue",
          "payload": json.dumps({"number": 3})}
    assert task_id(ev) == "issue-3", task_id(ev)
    assert task_id({**ev, "payload": {"number": 3}}) == "issue-3"  # Row/dict payload
    assert task_id({**ev, "payload": "{}"}) == "event-12", "缺 number 不能撞成 issue-None"
    assert task_id({**ev, "payload": "not json"}) == "event-12", "坏 payload 不能崩"
    assert task_id({**ev, "source_type": "feishu"}) == "event-12"

    # 2. Outside the repo: an attempt's throwaway worktree must not own the file.
    assert _dir({}) == Path.home() / ".wick" / "progress", _dir({})
    assert _dir({"WICK_PROGRESS_DIR": "/tmp/p"}) == Path("/tmp/p")
    assert PROJECT_ROOT not in path_for("x").parents, "进度文件落在仓库里了"

    original = PROGRESS_DIR
    try:
        with tempfile.TemporaryDirectory() as td:
            PROGRESS_DIR = Path(td) / "progress"
            index = PROGRESS_DIR / INDEX_NAME

            # 3. Nothing on disk: no history, no error.
            assert load("issue-3") == ("", "") and parse_index("") == {}
            assert parse_index("随便写的\n\n- issue-3: fix（t）\n") == {"issue-3": "fix"}

            # 4. Append, then append again -- the first block must survive.
            assert append("issue-3", verdict="已修复", files=["src/db.py"],
                          checks="10/10 通过", ts="2026-11-22T12:00:00") is not None
            append("issue-3", verdict="部分修复", files=["src/agent.py", "src/progress.py"],
                   checks="9/10 通过；失败: src/db.py", leftover="eval 用例还没补",
                   ts="2026-11-22T13:00:00")
            body = path_for("issue-3").read_text(encoding="utf-8")
            assert "## 2026-11-22T12:00:00" in body and "## 2026-11-22T13:00:00" in body, body
            assert "src/db.py" in body and "eval 用例还没补" in body, body
            assert body.count("# issue-3") == 1, "文件头重复写了"

            # 5. In-flight task: one line, updated in place, format from the ticket.
            lines = index.read_text(encoding="utf-8").splitlines()
            assert lines == ["- issue-3: partial（2026-11-22T13:00:00）"], lines

            # 6. A 200-line log does not grow the index; a finished task leaves it.
            with path_for("issue-3").open("a", encoding="utf-8") as fh:
                fh.write("\n".join(f"- 填充行 {i}" for i in range(200)) + "\n")
            append("issue-3", verdict="已修复", ts="2026-11-22T14:00:00")
            after = index.read_text(encoding="utf-8")
            assert "填充行" not in after, "索引里混进了进度正文"
            assert after.strip() == "" and parse_index(after) == {}, after
            assert "## 2026-11-22T14:00:00" in path_for("issue-3").read_text(encoding="utf-8"), \
                "从索引删掉不等于删历史"

            # 7. load(): others' index lines, but only THIS task's log.
            append("event-9", verdict="可处理", leftover="等作者回", ts="2026-11-22T14:05:00")
            idx, own = load("issue-3")
            assert "填充行 0" in own, "本任务的历史没带上"
            assert "- event-9: plan（2026-11-22T14:05:00）" in idx, idx
            assert "# event-9" not in own, "把别的任务的正文也塞进 prompt 了"

            # 8. Only the last TASK_BLOCKS rounds are injected, with a marker.
            for i in range(10):
                append("issue-7", verdict="可处理", ts=f"2026-11-22T15:{i:02d}:00")
            _, tail_body = load("issue-7")
            assert tail_body.count("## ") == TASK_BLOCKS, tail_body
            assert "更早的 4 轮记录已省略" in tail_body, tail_body
            assert "2026-11-22T15:09:00" in tail_body, tail_body
            assert "2026-11-22T15:00:00" not in tail_body, tail_body

            # 9. Missing task file: index only, no raise.
            idx, own = load("issue-404")
            assert own == "" and "# issue-404" not in own
            assert "- event-9:" in idx

            # 10. Writing where we cannot: degrade to None, not an exception.
            blocked = Path(td) / "blocked"
            blocked.write_text("I am a file, not a directory", encoding="utf-8")
            PROGRESS_DIR = blocked
            assert append("issue-3", verdict="已修复") is None, "写不进去必须返回 None"
            assert load("issue-3") == ("", ""), "读不到就是空，不能抛"

        with tempfile.TemporaryDirectory() as td:
            PROGRESS_DIR = Path(td) / "progress"
            PROGRESS_DIR.mkdir(parents=True)
            index = PROGRESS_DIR / INDEX_NAME

            # 11. Corrupt index: bad bytes and junk lines are skipped BEFORE the
            # prompt, so a garbled line never becomes model input.
            index.write_bytes(
                b"\xff\xfe\x00 not utf8\n- issue-3: fix\xef\xbc\x882026-11-22T12:00:00\xef\xbc\x89\n")
            assert parse_index(_read(index)) == {"issue-3": "fix"}
            idx, _ = load("issue-9")
            assert "2026-11-22T12:00:00" in idx and "not utf8" not in idx, idx

            # 12. Corrupt progress file: replacement chars, still loadable.
            path_for("issue-3").write_bytes(b"# issue-3\n## 2026-11-22T12:00:00\n\xff\xfe garbage\n")
            assert "garbage" in load("issue-3")[1]

            # 13. A corrupt index does not block the next append.
            assert append("issue-3", verdict="部分修复", ts="2026-11-22T15:00:00") is not None
            idx_text = _read(index)
            assert "not utf8" not in idx_text and parse_index(idx_text)["issue-3"] == "partial", \
                idx_text

            # 14. Fenced append: once the event has moved on (attempts bumped, or
            # a different owner), the write is refused and the file is unchanged.
            conn = dbmod.connect(":memory:")
            eid, _ = dbmod.enqueue(conn, "manual", "guard-1", {"title": "守卫"})
            claimed = dbmod.claim_next(conn, "w-guard")
            guard = (claimed["id"], claimed["attempts"], claimed["worker_id"])
            assert append("issue-3", verdict="部分修复", leftover="主人写的",
                          ts="2026-11-22T16:00:00", conn=conn, guard=guard) is not None
            before = _read(path_for("issue-3"))
            conn.execute("UPDATE events SET attempts=attempts+1 WHERE id=?", (eid,))
            conn.commit()
            assert append("issue-3", verdict="已修复", leftover="僵尸写的",
                          ts="2026-11-22T17:00:00", conn=conn, guard=guard) is None, \
                "僵尸 attempt 的 append 必须被拒"
            assert _read(path_for("issue-3")) == before, "被拒之后文件变了"
            assert append("issue-3", verdict="已修复", conn=None, guard=guard) is None, \
                "没有 conn 时不能假装校验过"
            conn.close()

            # 15. A late writer must not overwrite a newer index line.
            append("issue-5", verdict="可处理", ts="2026-11-22T18:00:00")
            append("issue-5", verdict="部分修复", ts="2026-11-22T17:00:00")  # older, arrives later
            assert parse_index(_read(index))["issue-5"] == "plan", \
                "旧 ts 覆盖了新 ts，索引显示的不是最新状态"
    finally:
        PROGRESS_DIR = original

    print("progress.py selftest OK: 任务 id 映射 / 追加不覆盖 / 索引只在途 / 尾部截断 / "
          "只带本任务 / 缺文件降级 / 坏索引与坏进度文件降级 / 写入 fence / 索引按 ts 比较")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
