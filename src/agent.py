"""Agent invocation: turn one event into one Hermes run.

Two modes, chosen by the event source:

- `handle_event` (D3): read-only analysis. `hermes -z` prints only the final
  response text, so no WebSocket client or control-ticket auth is needed.
- `handle_issue_fix` (D4): edit code inside a throwaway worktree, then verify by
  running the project's own self-checks. The verification is run by THIS module,
  never taken from the agent's own writeup: a self-report is not evidence.

ponytail: one subprocess per event, no pooling. Fine at a few events per hour;
if throughput ever matters, this is where a resident gateway client would go.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
HERMES_BIN = os.environ.get("WICK_HERMES_BIN", "hermes")
DEFAULT_TIMEOUT = 900      # seconds; a real code-reading run can take minutes
FIX_TIMEOUT = 1500         # editing plus testing takes longer than pure analysis
CHECK_TIMEOUT = 300

# The project's own self-checks double as its regression suite: four scripts,
# each assert-based, no test framework. Green here means nothing broke.
CHECKS = [
    ["src/db.py"],
    ["src/worker.py", "--selftest"],
    ["src/sources/github.py"],
    ["eval/run.py", "--selftest"],
]

VERDICTS_FIX = ("已修复", "部分修复", "无法修复")
VERDICTS_ANALYZE = ("可处理", "需补充信息", "不处理")
# The only irreversible action this version asks permission for.
_ACTION_LABEL = "提 PR"


def _render(template: str, payload: dict, **extra) -> str:
    tpl = (PROMPT_DIR / template).read_text(encoding="utf-8")
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
        **extra,
    )


def render_issue_prompt(payload: dict) -> str:
    """Read-only analysis prompt (D3)."""
    return _render("issue.md", payload)


def render_fix_prompt(payload: dict, worktree: Path, branch: str) -> str:
    """Fix prompt pointing the agent at its own worktree (D4)."""
    return _render("issue_fix.md", payload, worktree=str(worktree), branch=branch)


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


def handle_event(event) -> str:
    """D3 path: read-only analysis, no worktree, no file changes."""
    payload = json.loads(event["payload"] or "{}")
    return run_prompt(render_issue_prompt(payload))


def handle_issue_fix(conn, event) -> str:
    """D4/D5 path: edit inside a worktree, verify locally, then ask for approval.

    A green suite is evidence, not permission: this never commits, pushes or opens
    a PR. On success it writes a `waiting` approval row and returns a report
    telling the user how to approve it.

    Raises when the run failed or the self-checks did not all pass, so a broken
    run is recorded as failed even if the agent claims otherwise.
    """
    import db as dbmod
    import sandbox

    payload = json.loads(event["payload"] or "{}")
    number = payload.get("number", "?")
    worktree, branch = sandbox.create(PROJECT_ROOT, event["id"], number,
                                      attempt=event["attempts"])

    run_error: Exception | None = None
    text = ""
    try:
        text = run_prompt(render_fix_prompt(payload, worktree, branch),
                          cwd=worktree, timeout=FIX_TIMEOUT)
    except Exception as exc:  # still collect the diff before failing
        run_error = exc

    checks = run_checks(worktree)
    changes = sandbox.diff(PROJECT_ROOT, worktree)

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
        "worktree": str(worktree),
        "files": changes["files"],
        "diff": f"+{changes['lines_added']}/-{changes['lines_removed']}",
        # Which attempt this approval belongs to. process_approvals refuses an
        # approval whose attempt no longer owns the event: otherwise an attempt
        # that queued an approval and then lost its lease would still open a PR
        # from a stale worktree.
        "attempts": event["attempts"],
    }, worker_id=event["worker_id"], attempts=event["attempts"])
    if approval_id is None:
        # The lease was reclaimed while we worked: some other attempt owns the
        # event now. Queue nothing -- the worker drops the whole result.
        raise RuntimeError("事件已被回收并转交，本次结果作废（不写审批、不投递）")
    return compose_fix_report(payload, text, None, checks, changes, worktree, branch,
                              approval_id=approval_id)


# ---------------------------------------------------------------- selftest

def _selftest() -> int:
    """Self-check: template rendering, verdict parsing, check runner. No model call."""
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

    # Fix template carries the worktree contract.
    wt_path = Path("/tmp/wt-x")
    fix = render_fix_prompt(payload, wt_path, "wick/issue-7-run-3")
    assert str(wt_path) in fix and "wick/issue-7-run-3" in fix
    for must in ("git commit", "git push", "python src/db.py", "判定：<已修复"):
        assert must in fix, must

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

    # The check runner must actually detect a broken worktree.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        wt = Path(td)
        (wt / "src").mkdir()
        (wt / "eval").mkdir()
        good = "print('ok')\n"
        for rel in ("src/db.py", "src/worker.py", "src/sources/github.py", "eval/run.py"):
            (wt / rel).parent.mkdir(parents=True, exist_ok=True)
            (wt / rel).write_text(good, encoding="utf-8")
        checks = run_checks(wt)
        assert checks["all_ok"] and checks["passed"] == 4, checks

        (wt / "src" / "db.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
        checks = run_checks(wt)
        assert not checks["all_ok"] and checks["passed"] == 3, checks
        assert "src/db.py" in _checks_line(checks)

    print("agent.py selftest OK: 模板渲染 / 判定解析 / 自检真能失败")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
