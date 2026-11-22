"""Agent invocation: turn one event into one Hermes run.

Two modes, chosen by the event source:

- `handle_event` (D3): read-only analysis. `hermes -z` prints only the final
  response text, so no WebSocket client or control-ticket auth is needed.
- `handle_issue_fix` (D4): edit code inside a throwaway worktree, then verify by
  running the project's own self-checks. The verification is run by THIS module,
  never taken from the agent's own writeup: a self-report is not evidence.

Both paths carry cross-run memory (D7): the prompt gets the in-flight index and
this task's last rounds from `progress.load(task)` up front, and the run's
outcome is appended when it ends, so a second round on the same task continues
instead of re-analysing from zero. The append is fenced on the claim like every
other write a lease owns.

ponytail: one subprocess per event, no pooling. Fine at a few events per hour;
if throughput ever matters, this is where a resident gateway client would go.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
HERMES_BIN = os.environ.get("WICK_HERMES_BIN", "hermes")
DEFAULT_TIMEOUT = 900      # seconds; a real code-reading run can take minutes
FIX_TIMEOUT = 1500         # editing plus testing takes longer than pure analysis
CHECK_TIMEOUT = 300

log = logging.getLogger("wick.agent")

# The project's own self-checks double as its regression suite: four scripts,
# each assert-based, no test framework. Green here means nothing broke.
CHECKS = [
    ["src/db.py"],
    ["src/worker.py", "--selftest"],
    ["src/sources/github.py"],
    ["src/agent.py"],
    ["src/sandbox.py"],
    ["src/actions.py"],
    ["src/progress.py"],
    ["src/cli.py", "selftest"],
    ["src/outbox.py"],
    ["eval/run.py", "--selftest"],
]

VERDICTS_FIX = ("已修复", "部分修复", "无法修复")
VERDICTS_ANALYZE = ("可处理", "需补充信息", "不处理")
# Injected when the task has no history: the prompt must still render, and the
# agent should know a first run is a first run.
NO_PROGRESS = "（没有历史进度：这是这个任务的第一次运行）"
# The only irreversible action this version asks permission for.
_ACTION_LABEL = "提 PR"


def _render(template: str, payload: dict,
            progress: tuple[str, str] = ("", ""), **extra) -> str:
    """Fill one template. `progress` is (index_text, task_text) from progress.load.

    Two slots, not one: the index describes OTHER in-flight tasks and must not
    be mistaken for this task's history. When task_text is empty the template's
    "first run" marker goes in -- a new task is never told it is a continuation.
    """
    tpl = (PROMPT_DIR / template).read_text(encoding="utf-8")
    index_text, task_text = progress
    labels = ", ".join(payload.get("labels") or []) or "(无)"
    return tpl.format(
        repo=payload.get("repo", "?"),
        number=payload.get("number", "?"),
        title=payload.get("title", "(无标题)"),
        url=payload.get("url", ""),
        labels=labels,
        author=payload.get("author") or "(未知)",
        state=payload.get("state") or "(未知)",
        body=payload.get("body") or "(正文为空)",
        progress_index=index_text or "（没有其他进行中的任务）",
        progress_task=task_text or NO_PROGRESS,
        **extra,
    )


def render_issue_prompt(payload: dict, progress: tuple[str, str] = ("", "")) -> str:
    """Read-only analysis prompt (D3)."""
    return _render("issue.md", payload, progress)


def render_fix_prompt(payload: dict, worktree: Path, branch: str,
                      progress: tuple[str, str] = ("", "")) -> str:
    """Fix prompt pointing the agent at its own worktree (D4)."""
    return _render("issue_fix.md", payload, progress, worktree=str(worktree), branch=branch)


def run_prompt(prompt: str, cwd: str | Path | None = None,
               timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run one Hermes prompt via `hermes -z` and return its final text.

    Raises RuntimeError on a non-zero exit, a timeout, or empty output: a failed
    run must be recorded as failed, never delivered as an empty success.
    """
    try:
        p = subprocess.run(
            [HERMES_BIN, "-z", prompt],
            cwd=str(cwd or PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise RuntimeError(f"找不到 hermes 可执行文件（{HERMES_BIN}）") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"hermes 运行超时（{timeout}s）") from e

    out = (p.stdout or "").strip()
    if p.returncode != 0:
        err = (p.stderr or "").strip().splitlines()[-3:]
        raise RuntimeError(f"hermes 退出码 {p.returncode}: {' | '.join(err)}")
    if not out:
        raise RuntimeError("hermes 没有输出任何内容")
    return out


def verdict_of(text: str, kind: str = "analyze") -> str | None:
    """Extract the first-line verdict the prompt template asks for.

    Returns one of the template's allowed verdicts, or None when the model did
    not follow the format. Callers must treat None as "unstructured output".
    """
    allowed = VERDICTS_FIX if kind == "fix" else VERDICTS_ANALYZE
    for line in text.splitlines():
        line = line.strip().lstrip("*").strip()
        if line.startswith("判定"):
            for v in allowed:
                if v in line:
                    return v
    return None


def leftover_of(text: str) -> str:
    """The 遗留问题 / 遗留 / 下一步 line from a report, or 无.

    Progress must record what is still open -- that is the one part of the
    writeup the next run cannot reconstruct by itself.
    """
    for line in text.splitlines():
        m = re.match(r"\s*[-*#>]*\s*遗留(?:问题)?(?:\s*/\s*下一步)?\s*[:：]\s*(.+)", line)
        if m:
            return m.group(1).strip().strip("`*") or "无"
    return "无"


def run_checks(worktree: Path, timeout: int = CHECK_TIMEOUT) -> dict:
    """Run the project's self-checks inside the worktree. This is the real verdict."""
    results = []
    for args in CHECKS:
        cmd = [sys.executable, *args]
        try:
            p = subprocess.run(cmd, cwd=str(worktree), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout)
            ok = p.returncode == 0
            tail = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()[-3:]
        except subprocess.TimeoutExpired:
            ok, tail = False, [f"TIMEOUT after {timeout}s"]
        results.append({"cmd": " ".join(args), "ok": ok, "tail": tail})
    passed = sum(1 for r in results if r["ok"])
    return {"passed": passed, "total": len(results),
            "all_ok": passed == len(results), "results": results}


def _checks_line(checks: dict) -> str:
    if checks["all_ok"]:
        return f"{checks['passed']}/{checks['total']} 通过（本机实跑，非 agent 自报）"
    failed = [r["cmd"] for r in checks["results"] if not r["ok"]]
    return f"{checks['passed']}/{checks['total']} 通过；失败: {', '.join(failed)}（本机实跑）"


def compose_fix_report(payload: dict, text: str, run_error: Exception | None,
                       checks: dict, changes: dict, worktree: Path, branch: str,
                       approval_id: int | None = None) -> str:
    verdict = verdict_of(text, "fix") if text else None
    head = [
        f"针对 {payload.get('repo','?')}#{payload.get('number','?')} {payload.get('title','')}",
        f"判定：{verdict or '（agent 未按格式输出）'}",
        f"分支：{branch}",
        f"worktree：{worktree}",
        f"改动：{changes['file_count']} 个文件 +{changes['lines_added']}/-{changes['lines_removed']}",
        f"自检：{_checks_line(checks)}",
    ]
    if approval_id is not None:
        head.append(f"待批准：审批 #{approval_id}（{_ACTION_LABEL}）— 什么都没提交，等你点头")
    if changes["file_count"]:
        head.append("改动文件：")
        head += [f"- {f}" for f in changes["files"][:12]]
    body = [""]
    if run_error is not None:
        body += [f"**agent 运行失败**：{run_error}", ""]
    if text:
        body += ["—— agent 说明 ——", text]
    body.append("")
    if approval_id is not None:
        body += [f"批准: python src/cli.py approve {approval_id}",
                 f"拒绝: python src/cli.py reject {approval_id}"]
    else:
        body.append("审查后用 `python src/sandbox.py --prune` 清理 worktree")
    return "\n".join(head + body)


# `下次跟进：<ISO 时间> <做什么>`. The time may use T or a space and may carry a
# Z/±hh:mm offset; anything else is handed to db.schedule_followup(), which
# rejects it with ValueError rather than guessing.
_DUE_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?(?:Z|[+-]\d{2}:\d{2})?)\s*(.*)",
    re.S,
)


def next_followup(text: str) -> tuple[str, str] | None:
    """Split the output's `下次跟进` line into (when, what).

    None means "nothing to come back to": the line is absent, or the model wrote
    `无`. Parsing is deliberately forgiving -- validation (and dedupe) belong to
    db.schedule_followup().
    """
    for line in text.splitlines():
        line = line.strip().lstrip("*#>- ").strip()
        if not line.startswith("下次跟进"):
            continue
        for sep in ("：", ":"):
            if sep in line:
                value = line.split(sep, 1)[1].strip()
                break
        else:
            return None
        if not value or value.startswith("无"):
            return None
        m = _DUE_RE.match(value)
        if m is None:
            return value, "(未说明)"
        return m.group(1), m.group(2).strip() or "(未说明)"
    return None


def _queue_followup(conn, text: str) -> None:
    """Queue the followup slot the agent just wrote, if any.

    Never raises. The run has already succeeded by the time this is called; a
    malformed time or a DB hiccup costs one warning line, not a failed event.
    """
    slot = next_followup(text)
    if slot is None:
        return
    when, note = slot
    try:
        import db as dbmod
        eid, created = dbmod.schedule_followup(conn, when, note)
    except Exception as exc:
        log.warning("下次跟进没排上（%s）：%s", when, exc)
        return
    log.info("下次跟进 %s #%s（%s）：%s", "已排" if created else "已存在", eid, when, note)


def handle_event(conn, event) -> str:
    """D3 path: read-only analysis, no worktree, no file changes.

    The claim is threaded through to the progress write: an attempt that lost
    its lease must not append its verdict after the winner's.
    """
    import progress as progressmod

    payload = json.loads(event["payload"] or "{}")
    task = progressmod.task_id(event)
    guard = (event["id"], event["attempts"], event["worker_id"])
    learned = progressmod.load(task)
    try:
        text = run_prompt(render_issue_prompt(payload, learned))
    except Exception as exc:
        # Same asymmetry as the fix path: a failed round is exactly what the
        # next round needs to know about. No report -- there is none.
        progressmod.append(task, leftover=f"上次运行失败（{exc}）",
                           conn=conn, guard=guard)
        raise
    # An analysis leaves no diff but does leave a decision and a report. The
    # report body matters: the next round's input is "what did the last round
    # conclude and ask for", not just the verdict label.
    progressmod.append(task, verdict=verdict_of(text),
                       checks="—（只读分析，未改代码）", leftover=leftover_of(text),
                       report=text, conn=conn, guard=guard)
    _queue_followup(conn, text)
    return text


def handle_issue_fix(conn, event) -> str:
    """D4/D5 path: edit inside a worktree, verify locally, then ask for approval.

    A green suite is evidence, not permission: this never commits, pushes or opens
    a PR. On success it writes a `waiting` approval row and returns a report
    telling the user how to approve it.

    Raises when the run failed or the self-checks did not all pass, so a broken
    run is recorded as failed even if the agent claims otherwise.
    """
    import db as dbmod
    import progress as progressmod
    import sandbox

    payload = json.loads(event["payload"] or "{}")
    number = payload.get("number", "?")
    task = progressmod.task_id(event)
    guard = (event["id"], event["attempts"], event["worker_id"])
    worktree, branch = sandbox.create(PROJECT_ROOT, event["id"], number,
                                      attempt=event["attempts"])

    run_error: Exception | None = None
    text = ""
    try:
        text = run_prompt(render_fix_prompt(payload, worktree, branch,
                                            progressmod.load(task)),
                          cwd=worktree, timeout=FIX_TIMEOUT)
    except Exception as exc:  # still collect the diff before failing
        run_error = exc

    # The run is over, so whatever it wanted to come back to can be queued now.
    # Never raises: a scheduling hiccup must not fail a run that worked.
    _queue_followup(conn, text)

    checks = run_checks(worktree)
    changes = sandbox.diff(PROJECT_ROOT, worktree)

    # Write the trace before the raises below: a failed or half-done run is
    # exactly what the next round has to know about. The worktree is thrown
    # away, this file is not. The patch goes in because the next attempt starts
    # from a clean tree -- the note is the only way it can see what was changed.
    leftover = leftover_of(text)
    if run_error is not None:
        leftover = f"上次运行失败（{run_error}）；{leftover}"
    progressmod.append(task, verdict=verdict_of(text, "fix") if text else None,
                       files=changes["files"], checks=_checks_line(checks),
                       leftover=leftover, patch=changes["patch"],
                       conn=conn, guard=guard)

    if run_error is not None:
        raise RuntimeError(
            compose_fix_report(payload, text, run_error, checks, changes, worktree, branch)
        ) from run_error
    if not checks["all_ok"]:
        raise RuntimeError(
            compose_fix_report(payload, text, None, checks, changes, worktree, branch)
        )

    approval_id = dbmod.request_approval(conn, event["id"], _ACTION_LABEL, {
        "repo": payload.get("repo"),
        "number": number,
        "branch": branch,
        "base": "main",
        "title": payload.get("title"),
        "labels": payload.get("labels") or [],
        "worktree": str(worktree),
        "files": changes["files"],
        "diff": f"+{changes['lines_added']}/-{changes['lines_removed']}",
        # Which attempt this approval belongs to. process_approvals refuses an
        # approval whose attempt no longer owns the event: otherwise an attempt
        # that queued an approval and then lost its lease would still open a PR
        # from a stale worktree.
        "attempts": event["attempts"],
        # Enough context for the PR body to carry the three sections CONTRIBUTING
        # asks for, without re-reading the agent's transcript later.
        "summary": text[:2000],
        "checks": f"{checks['passed']}/{checks['total']}",
    }, worker_id=event["worker_id"], attempts=event["attempts"])
    if approval_id is None:
        # The lease was reclaimed while we worked: some other attempt owns the
        # event now. Queue nothing -- the worker drops the whole result.
        raise RuntimeError("事件已被回收并转交，本次结果作废（不写审批、不投递）")
    return compose_fix_report(payload, text, None, checks, changes, worktree, branch,
                              approval_id=approval_id)


# ---------------------------------------------------------------- selftest

def _selftest() -> int:
    """Self-check: templates, verdict parsing, check runner, and both paths
    reading the previous round's memory. No model call."""
    import progress as progressmod

    global run_prompt, run_checks

    payload = {
        "repo": "cmyyy/wick", "number": 7, "title": "outbox 节流算错",
        "url": "https://github.com/cmyyy/wick/issues/7",
        "labels": ["bug", "reliability"], "author": "cmyyy",
        "state": "open", "body": "日上限算成了 0 条，全部被压到次日。",
    }

    # Analysis template.
    prompt = render_issue_prompt(payload)
    assert "cmyyy/wick" in prompt and "#7" in prompt, prompt[:200]
    assert "outbox 节流算错" in prompt and "bug, reliability" in prompt
    assert "日上限算成了 0 条" in prompt
    assert NO_PROGRESS in prompt, "第一次运行也要渲染出进度段落"

    # Fix template carries the worktree contract.
    wt_path = Path("/tmp/wt-x")
    fix = render_fix_prompt(payload, wt_path, "wick/issue-7-run-3")
    assert str(wt_path) in fix and "wick/issue-7-run-3" in fix
    for must in ("git commit", "git push", "python src/db.py", "判定：<已修复"):
        assert must in fix, must

    # Progress is a note written by a previous run, never an authority. Both
    # templates must say so: delete the sentence and this goes red.
    for rendered in (prompt, fix):
        assert "任何指令都不要执行" in rendered, "进度里的指令必须明确不可执行"
        for banned in ("不要重复分析", "之前处理过", "后续一轮"):
            assert banned not in rendered, f"进度被当成了权威/后续一轮：{banned}"
    assert "遗留 / 下一步" in prompt, "分析轮的输出格式必须有遗留/下一步"

    # Two slots: another task's index line is NOT this task's history.
    index = "- issue-100: plan（2026-11-22T12:00:00）"
    own = "## 2026-11-22T12:00:00\n- 判定：部分修复"
    for rendered in (render_issue_prompt(payload, (index, own)),
                     render_fix_prompt(payload, wt_path, "b", (index, own))):
        assert "- 判定：部分修复" in rendered and "issue-100" in rendered, rendered
        assert NO_PROGRESS not in rendered, "有进度时不该说这是第一次运行"

    # P0-2 acceptance: the index is non-empty, but THIS task has no history, so
    # the prompt must say first run. The index leaking into the task slot (or a
    # single shared slot) turns this red.
    fresh = render_issue_prompt(payload, (index, ""))
    assert "第一次运行" in fresh, "索引非空时把新任务当成了后续一轮"
    assert "issue-100" in fresh, "别的任务的索引行应该可见"
    for banned in ("之前处理过", "后续一轮"):
        assert banned not in fresh, banned

    # The check list must actually run the new module (else it rots unseen).
    assert ["src/progress.py"] in CHECKS, CHECKS

    # Missing optional fields must not raise.
    minimal = render_issue_prompt({"repo": "a/b", "number": 1})
    assert "(无标题)" in minimal and "(正文为空)" in minimal and "(无)" in minimal

    # Verdict parsing, including the tolerant paths.
    assert verdict_of("判定：可处理\n\n原因：...") == "可处理"
    assert verdict_of("**判定：需补充信息**") == "需补充信息"
    assert verdict_of("判定: 不处理") == "不处理"
    assert verdict_of("我先看看代码……\n判定：可处理") == "可处理"
    assert verdict_of("没有按格式输出") is None
    assert verdict_of("判定：已修复", "fix") == "已修复"
    assert verdict_of("判定：部分修复", "fix") == "部分修复"
    assert verdict_of("判定：可处理", "fix") is None  # analysis verdict is not a fix verdict

    # 遗留 parsing: what the next run reads to know what is still open.
    assert leftover_of("判定：已修复\n遗留问题：无") == "无"
    assert leftover_of("**遗留：eval 用例还没补**") == "eval 用例还没补"
    assert leftover_of("- 遗留问题:`补 eval`") == "补 eval"
    assert leftover_of("遗留 / 下一步：等作者回复后重新判定") == "等作者回复后重新判定"
    assert leftover_of("没按格式写") == "无"

    # ---- analysis path: two rounds on one claimed event, no model involved.
    import tempfile

    import db as dbmod

    conn = dbmod.connect(":memory:")
    dbmod.enqueue(conn, "github_issue", "cmyyy/wick#4",
                  {"repo": "cmyyy/wick", "number": 4, "title": "跨 run 记忆"})
    event = dbmod.claim_next(conn, "w-agent")
    assert event is not None and event["attempts"] == 1, dict(event or {})

    round1 = ("判定：需补充信息\n\n"
              "原因：描述里没有复现步骤，定位不到具体代码路径。\n\n"
              "需要作者补充：\n1. 复现步骤\n2. 出错时的日志\n\n"
              "遗留 / 下一步：等作者回复后重新判定")

    with tempfile.TemporaryDirectory() as td:
        saved_dir, saved_run = progressmod.PROGRESS_DIR, run_prompt
        prompts: list[str] = []
        progressmod.PROGRESS_DIR = Path(td) / "progress"

        def fake_run(prompt, cwd=None, timeout=DEFAULT_TIMEOUT):
            prompts.append(prompt)
            return round1

        run_prompt = fake_run
        try:
            handle_event(conn, event)                       # round 1
            assert NO_PROGRESS in prompts[0], "第一轮不该有历史"
            assert "1. 复现步骤" not in prompts[0]

            handle_event(conn, event)                       # round 2, same task
            assert "- 判定：需补充信息" in prompts[1], "第二轮看不到第一轮判定了什么"
            assert "1. 复现步骤" in prompts[1], "第二轮看不到第一轮的报告正文（只剩判定标签）"
            assert "等作者回复后重新判定" in prompts[1], "第二轮看不到遗留"
            assert "issue-4: need-info" in prompts[1], "索引没带上"

            # A different task sees the index line, never this task's log.
            idx, other = progressmod.load("issue-99")
            assert "issue-4: need-info" in idx, idx
            assert other == "" and "等作者回复后重新判定" not in idx, "别的任务的历史泄漏了"
        finally:
            run_prompt, progressmod.PROGRESS_DIR = saved_run, saved_dir
    conn.close()

    # ---- fix path: through handle_issue_fix itself, because that is the default
    # WICK_MODE and therefore the main D7 path. The old test built its own render
    # call, so deleting the load() inside handle_issue_fix stayed green. Revert
    # that load() to ("", "") and the round-2 assertions here go red.
    import sandbox

    fconn = dbmod.connect(":memory:")
    dbmod.enqueue(fconn, "github_issue", "cmyyy/wick#12",
                  {"repo": "cmyyy/wick", "number": 12, "title": "outbox 重复投递"})
    fevent = dbmod.claim_next(fconn, "w-fix")
    assert fevent is not None

    fix_text = ("判定：部分修复\n\n"
                "根因：outbox.drain 在提交之后才标记 sent（src/outbox.py:42）\n\n"
                "改了什么：\n- `src/outbox.py` — 先标记 sent 再提交\n\n"
                "验证：自检通过\n\n"
                "遗留问题：eval 用例还没补")
    fix_prompts: list[str] = []
    checks_line = f"{len(CHECKS)}/{len(CHECKS)} 通过"

    with tempfile.TemporaryDirectory() as td:
        saved = (progressmod.PROGRESS_DIR, run_prompt, run_checks,
                 sandbox.create, sandbox.diff)
        progressmod.PROGRESS_DIR = Path(td) / "progress"
        wt = Path(td) / "wt"

        def fake_create(repo, event_id, issue_number, base="HEAD", *, attempt):
            return wt, f"wick/issue-{issue_number}-run-{event_id}-attempt-{attempt}"

        def fake_fix_run(prompt, cwd=None, timeout=DEFAULT_TIMEOUT):
            fix_prompts.append(prompt)
            return fix_text

        def fake_checks(worktree: Path, timeout: int = CHECK_TIMEOUT):
            total = len(CHECKS)
            return {"passed": total, "total": total, "all_ok": True, "results": []}

        def fake_diff(repo, path, base="HEAD"):
            return {"file_count": 1, "files": ["src/outbox.py"], "lines_added": 12,
                    "lines_removed": 3, "patch_lines": 2,
                    "patch": "diff --git a/src/outbox.py b/src/outbox.py\n+标记 sent 再提交\n"}

        run_prompt, run_checks = fake_fix_run, fake_checks
        sandbox.create, sandbox.diff = fake_create, fake_diff
        try:
            report = handle_issue_fix(fconn, fevent)
            assert "审批" in report, report[:200]
            assert NO_PROGRESS in fix_prompts[0], "第一轮不该有历史"

            handle_issue_fix(fconn, fevent)                 # round 2, same task
            second = fix_prompts[1]
            for must in ("src/outbox.py", checks_line, "eval 用例还没补",
                         "部分修复", "标记 sent 再提交"):
                assert must in second, (must, second[:600])
            assert NO_PROGRESS not in second, "第二轮还说这是第一次运行"
        finally:
            (progressmod.PROGRESS_DIR, run_prompt, run_checks,
             sandbox.create, sandbox.diff) = saved
    fconn.close()

    # Timer (#14): both templates carry the self-scheduling slot, and the slot
    # is parsed per the format the templates promise.
    for tpl, name in ((prompt, "issue.md"), (fix, "issue_fix.md")):
        assert "下次跟进：<ISO 时间>" in tpl, name
        assert "ISO 8601" in tpl, name

    assert next_followup("判定：可处理\n下次跟进：2026-11-23T09:00:00+08:00 复查 outbox 节流") == \
        ("2026-11-23T09:00:00+08:00", "复查 outbox 节流")
    assert next_followup("**下次跟进：无**") is None
    assert next_followup("判定：可处理\n\n原因：...") is None
    assert next_followup("下次跟进: 2026-11-23 09:00 复查") == ("2026-11-23 09:00", "复查")
    assert next_followup("- 下次跟进：2026-11-23 复查") == ("2026-11-23", "复查")
    assert next_followup("下次跟进：2026-11-23T09:00:00Z") == \
        ("2026-11-23T09:00:00Z", "(未说明)")

    # The real queue path: what the agent writes lands in events, deduped, and
    # a bad or past time is a warning instead of an exception.
    import db as dbmod
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    aconn = dbmod.connect(":memory:")
    later = (_dt.now(_tz.utc) + _td(hours=3)).isoformat(timespec="seconds")
    said = f"判定：可处理\n下次跟进：{later} 复查 outbox 节流"
    _queue_followup(aconn, said)
    _queue_followup(aconn, said)  # a retried run re-parses the same slot
    rows = aconn.execute("SELECT source_type, source_id, due_at FROM events").fetchall()
    assert len(rows) == 1, [dict(r) for r in rows]
    assert rows[0]["source_type"] == "followup" and rows[0]["due_at"] == later, dict(rows[0])
    assert rows[0]["source_id"].startswith(f"{later}:"), dict(rows[0])
    gone = (_dt.now(_tz.utc) - _td(minutes=5)).isoformat(timespec="seconds")
    _queue_followup(aconn, "下次跟进：明天下午三点 复查")   # unparseable
    _queue_followup(aconn, f"下次跟进：{gone} 已经过去的时间")  # expired
    _queue_followup(aconn, "下次跟进：无")
    _queue_followup(aconn, "判定：可处理")                    # no slot at all
    assert aconn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 1, \
        "非法/过期/无 的跟进不该入队，也不该抛异常"

    # handle_event itself must queue the slot -- not just the helper. Stub the
    # model call so this exercises the real handler.
    event = {"id": 9, "source_type": "github_issue",
             # #13 fences the progress write on the claim, so a bare dict has to
             # carry the same three fields claim_next would have stamped.
             "attempts": 1, "worker_id": "w-timer",
             "payload": json.dumps({"repo": "a/b", "number": 9})}
    said2 = f"判定：可处理\n下次跟进：{later} 复核完再跟进"
    real_run = globals()["run_prompt"]
    globals()["run_prompt"] = lambda prompt, **kw: said2
    try:
        assert handle_event(aconn, event) == said2
    finally:
        globals()["run_prompt"] = real_run
    assert aconn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 2, \
        "handle_event 跑完没有把「下次跟进」排进 events"
    aconn.close()

    # The check runner must actually detect a broken worktree.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        wt = Path(td)
        (wt / "src").mkdir()
        (wt / "eval").mkdir()
        good = "print('ok')\n"
        for rel in ("src/db.py", "src/worker.py", "src/sources/github.py", "src/agent.py",
                    "src/sandbox.py", "src/actions.py", "src/progress.py", "src/cli.py",
                    "src/outbox.py", "eval/run.py"):
            (wt / rel).parent.mkdir(parents=True, exist_ok=True)
            (wt / rel).write_text(good, encoding="utf-8")
        checks = run_checks(wt)
        total = len(CHECKS)
        assert checks["all_ok"] and checks["passed"] == total, checks

        (wt / "src" / "db.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
        checks = run_checks(wt)
        assert not checks["all_ok"] and checks["passed"] == total - 1, checks
        assert "src/db.py" in _checks_line(checks)

    print("agent.py selftest OK: 模板渲染 / 判定解析 / 进度两槽且不是权威 / 下次跟进入队与去重 / 自检真能失败 / "
          "分析路径两轮看到上一轮报告 / fix 路径经 handle_issue_fix 注入上一轮文件·自检·遗留·patch")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
