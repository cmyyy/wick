"""Cross-run memory: one progress file per task, plus a one-line index.

A task that takes several runs (a big issue fix, an analysis that needs another
pass) must start where the last run stopped instead of re-reading everything
from zero. Two files carry that:

    <home>/progress/index.md       one line per in-flight task: status + when
    <home>/progress/<task_id>.md   append-only log: verdict, files, checks, left

Every run loads the index and its own task log, never anyone else's log. Growth
is one block per run, so a task file stays tens of lines while the index stays
one line per task -- prompt size never scales with history.

The directory lives OUTSIDE the worktree on purpose: sandbox.py hands every
attempt its own throwaway tree, so progress written inside one would disappear
with it (that is exactly what cross-run memory must not do).

ponytail: plain files, no locking. The worker is serial by design (see db.py),
so there is one appender; a kill in the middle of a write costs one torn line
in an append-only log, not the file.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INDEX_NAME = "index.md"

# `- issue-3: fix（2026-11-22T12:00:00）`; anything else in the file is ignored,
# so a hand-edited or half-written index degrades instead of raising.
INDEX_LINE = re.compile(r"^-\s+([^\s:]+):\s*([^\s（(]+)")

# Verdicts from both prompt templates, shortened for the index line.
STATUS = {
    "已修复": "fix", "部分修复": "partial", "无法修复": "blocked",
    "可处理": "plan", "需补充信息": "need-info", "不处理": "skip",
}


def _dir(env: Mapping[str, str] | None = None) -> Path:
    """Resolved at import for PROGRESS_DIR, and callable for the selftest."""
    env = os.environ if env is None else env
    return Path(env.get("WICK_PROGRESS_DIR") or Path.home() / ".wick" / "progress")


PROGRESS_DIR = _dir()


def task_id(event) -> str:
    """`issue-<n>` for a GitHub issue, `event-<id>` for anything else.

    Pure: takes the event row, touches no disk. One task = one file is the
    whole mechanism, so this must be stable across runs and attempts.
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
    """Replace this task's line in place, or append it. Other lines untouched.

    The index is derived, so a rewrite drops anything that is not an index line
    -- that is how a corrupt index heals itself on the next append.
    """
    path = PROGRESS_DIR / INDEX_NAME
    lines = [ln for ln in _read(path).splitlines() if INDEX_LINE.match(ln.strip())]
    new = index_line(task, status, ts)
    for i, line in enumerate(lines):
        m = INDEX_LINE.match(line.strip())
        if m and m.group(1) == task:
            lines[i] = new
            break
    else:
        lines.append(new)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def append(task: str, *, verdict: str | None = None, files: list[str] | None = None,
           checks: str = "", leftover: str = "无", ts: str | None = None) -> Path | None:
    """Append one run's outcome to the task log and refresh its index line.

    Never overwrites history, never raises: recording a run must not be able to
    fail the run it is recording. Returns the file written, or None when the
    write was impossible.
    """
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


def load(task: str | None = None) -> str:
    """The memory block for a prompt: the whole index plus this task's log.

    "" when there is nothing to say or the files are unreadable -- a first run
    then gets the same prompt it got before this module existed.
    """
    parts = []
    index = _read(PROGRESS_DIR / INDEX_NAME).strip()
    if index:
        parts.append("### 所有进行中的任务（索引，一行一个）\n" + index)
    if task:
        body = _read(path_for(task)).strip()
        if body:
            parts.append(f"### 本任务（{task}）的历史进度，从旧到新\n" + body)
    return "\n\n".join(parts)


# ---------------------------------------------------------------- selftest

def _selftest() -> int:
    """Self-check: id mapping / append keeps history / index stays one line /
    missing and corrupt files degrade instead of raising."""
    import tempfile

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
            assert load("issue-3") == "" and parse_index("") == {}
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

            # 5. Index: one line, updated in place, format from the ticket.
            lines = index.read_text(encoding="utf-8").splitlines()
            assert lines == ["- issue-3: partial（2026-11-22T13:00:00）"], lines

            # 6. A 200-line progress file must leave the index exactly as small.
            with path_for("issue-3").open("a", encoding="utf-8") as fh:
                fh.write("\n".join(f"- 填充行 {i}" for i in range(200)) + "\n")
            append("issue-3", verdict="已修复", ts="2026-11-22T14:00:00")
            after = index.read_text(encoding="utf-8")
            assert len(after.splitlines()) == 1, after
            assert "填充行" not in after, "索引里混进了进度正文"
            assert after.strip() == "- issue-3: fix（2026-11-22T14:00:00）", after
            assert parse_index(after) == {"issue-3": "fix"}

            # 7. load(): the whole index, but only THIS task's full log.
            append("event-9", verdict="可处理", leftover="等作者回", ts="2026-11-22T14:05:00")
            prompt_block = load("issue-3")
            assert "填充行 0" in prompt_block, "本任务的历史没带上"
            assert "- event-9: plan（2026-11-22T14:05:00）" in prompt_block, prompt_block
            assert "# event-9" not in prompt_block, "把别的任务的正文也塞进 prompt 了"

            # 8. Missing task file: index only, no raise.
            assert "# issue-404" not in load("issue-404")
            assert "- issue-3:" in load("issue-404")

            # 9. Writing where we cannot: degrade to None, not an exception.
            blocked = Path(td) / "blocked"
            blocked.write_text("I am a file, not a directory", encoding="utf-8")
            PROGRESS_DIR = blocked
            assert append("issue-3", verdict="已修复") is None, "写不进去必须返回 None"
            assert load("issue-3") == "", "读不到就是空，不能抛"

        with tempfile.TemporaryDirectory() as td:
            PROGRESS_DIR = Path(td) / "progress"
            PROGRESS_DIR.mkdir(parents=True)

            # 10. Corrupt index: bad bytes and junk lines are skipped, the rest works.
            (PROGRESS_DIR / INDEX_NAME).write_bytes(
                b"\xff\xfe\x00 not utf8\n- issue-3: fix\xef\xbc\x882026-11-22T12:00:00\xef\xbc\x89\n")
            assert parse_index(_read(PROGRESS_DIR / INDEX_NAME)) == {"issue-3": "fix"}
            assert "2026-11-22T12:00:00" in load("issue-9"), "坏索引应该降级，不该抛"

            # 11. Corrupt progress file: replacement chars, still loadable.
            path_for("issue-3").write_bytes(b"# issue-3\n## 2026-11-22T12:00:00\n\xff\xfe garbage\n")
            assert "garbage" in load("issue-3")

            # 12. A corrupt index does not block the next append.
            assert append("issue-3", verdict="部分修复", ts="2026-11-22T15:00:00") is not None
            idx = _read(PROGRESS_DIR / INDEX_NAME)
            assert "not utf8" not in idx and parse_index(idx)["issue-3"] == "partial", idx
    finally:
        PROGRESS_DIR = original

    print("progress.py selftest OK: 任务 id 映射 / 追加不覆盖 / 索引只有一行 / "
          "只带本任务全文 / 缺文件降级 / 坏索引降级 / 坏进度文件降级 / 写失败不抛")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
