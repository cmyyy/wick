"""Irreversible actions, only ever reached through an approved approval row.

Everything here changes something outside the worktree: it commits, pushes, and
opens a pull request. The gate is the caller's job -- `worker.process_approvals`
only runs rows the user has marked approved -- so these functions stay dumb and
just refuse to improvise: no branch guessing, no force push, no PR without a
pushed branch.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GH_CANDIDATES = [
    os.environ.get("WICK_GH_BIN"),
    str(Path.home() / "AppData" / "Local" / "gh-cli" / "bin" / "gh.exe"),
    "gh",
]
# github.com is unreachable without the local proxy on this machine; the API and
# `gh pr create` both go through it. Empty string means "leave the env alone".
PROXY = os.environ.get("WICK_GITHUB_PROXY", "http://127.0.0.1:7890")


def _gh_bin() -> str:
    for gh in GH_CANDIDATES:
        if not gh:
            continue
        if gh == "gh" or Path(gh).exists():
            return gh
    raise RuntimeError("找不到 gh CLI（设 WICK_GH_BIN 或装 GitHub CLI）")


def _run(args: list[str], cwd: Path | str, timeout: int = 180) -> str:
    env = {**os.environ}
    if PROXY:
        env.setdefault("HTTPS_PROXY", PROXY)
        env.setdefault("HTTP_PROXY", PROXY)
    p = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout, env=env)
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])} 失败（{p.returncode}）: {out[-600:]}")
    return out


def commit_and_push(worktree: Path, branch: str, message: str) -> str:
    """Commit everything in the worktree and push the branch. Returns the commit sha."""
    _run(["git", "add", "-A"], worktree)
    rc = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=str(worktree)).returncode
    if rc == 0:
        raise RuntimeError("worktree 里没有可提交的改动")
    _run(["git", "commit", "-m", message], worktree)
    sha = _run(["git", "rev-parse", "--short", "HEAD"], worktree)
    _run(["git", "push", "-u", "origin", branch], worktree, timeout=300)
    return sha


def open_pr(worktree: Path, repo: str, branch: str, base: str,
            title: str, body: str) -> str:
    """Open a PR for an already-pushed branch. Returns its URL."""
    out = _run([
        _gh_bin(), "pr", "create",
        "--repo", repo, "--head", branch, "--base", base,
        "--title", title, "--body", body,
    ], worktree, timeout=300)
    for line in out.splitlines():
        if line.strip().startswith("https://"):
            return line.strip()
    raise RuntimeError(f"gh pr create 没有返回 PR 链接: {out[-300:]}")


def _commit_prefix(labels: list[str]) -> str:
    """Map an issue's labels to a conventional commit prefix.

    Ordered by specificity: a docs-only issue labelled both `docs` and `chore`
    should read `docs:`. Defaults to `fix` because most issues are reports.
    """
    for label, prefix in (
        ("docs", "docs"), ("documentation", "docs"),
        ("chore", "chore"), ("refactor", "refactor"),
        ("enhancement", "feat"), ("feature", "feat"),
        ("bug", "fix"), ("reliability", "fix"), ("tech-debt", "fix"),
    ):
        if any(label == str(x).lower() for x in labels):
            return prefix
    return "fix"


def _pr_body(detail: dict, branch: str, sha: str, number: int | str, repo: str) -> str:
    """PR body with the three sections CONTRIBUTING requires."""
    files = detail.get("files") or []
    files_block = "\n".join(f"- `{f}`" for f in files[:20]) or "- （未记录）"
    summary = (detail.get("summary") or "").strip() or "（见 diff）"
    checks = detail.get("checks") or "?"
    return (
        f"Closes #{number}\n\n"
        f"## 改了什么\n\n{summary}\n\n"
        f"改动文件（{len(files)} 个，{detail.get('diff', '?')}）：\n\n{files_block}\n\n"
        f"## 怎么验证的\n\n"
        f"wick 在 worktree 里跑了项目的自检，{checks} 项全绿。"
        f"自检由 worker 实际执行，不是 agent 自报。\n\n"
        # A section that always exists, even when nothing was reviewed. CONTRIBUTING
        # says the orchestrator's prose goes through review too; a rule whose only
        # landing spot is a sentence in a doc leaves no visible trace when skipped.
        # This is the trace: skip the review and "未填写" is what ships.
        f"## 审了什么\n\n"
        f"{detail.get('review') or '未填写 —— 若这条跳过 reviewer，在这里写理由。'}\n\n"
        f"## 遗留\n\n"
        f"无。若 review 中发现，补在评论里。\n\n"
        f"---\n\n"
        f"由 [wick](https://github.com/{repo}) 在人工批准后自动提交。\n"
        f"- 分支 `{branch}`\n- commit `{sha}`\n"
    )


def execute_approval(approval: dict, worktree: Path) -> dict:
    """Carry out one approved action. `approval` is the row's detail as a dict."""
    action = approval.get("action") or "提 PR"
    repo = approval["repo"]
    branch = approval["branch"]
    base = approval.get("base", "main")
    number = approval.get("number", "?")
    title = approval.get("title") or f"issue #{number}"
    prefix = _commit_prefix(approval.get("labels") or [])

    if action not in ("提 PR", "open_pr"):
        raise RuntimeError(f"不认识的动作: {action}")

    if not worktree.exists():
        raise RuntimeError(f"worktree 已不存在: {worktree}")

    sha = commit_and_push(
        worktree, branch,
        f"{prefix}: {title} (#{number})\n\n"
        f"Automated by wick after a human approved the action.\n"
        f"Approved action: {action}\n",
    )
    url = open_pr(
        worktree, repo, branch, base,
        f"{prefix}: {title} (#{number})",
        _pr_body(approval, branch, sha, number, repo),
    )
    return {"commit": sha, "url": url}


# ---------------------------------------------------------------- selftest

def _selftest() -> int:
    """Self-check: local commit/push against a bare repo; no network, no PR."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        bare = tmp / "origin.git"
        work = tmp / "work"
        _run(["git", "init", "-q", "--bare", str(bare)], tmp)
        _run(["git", "clone", "-q", str(bare), str(work)], tmp)
        _run(["git", "config", "user.email", "t@t"], work)
        _run(["git", "config", "user.name", "t"], work)
        (work / "a.py").write_text("x = 1\n", encoding="utf-8")
        _run(["git", "add", "-A"], work)
        _run(["git", "commit", "-qm", "init"], work)
        _run(["git", "push", "-q", "-u", "origin", "HEAD:refs/heads/main"], work)
        _run(["git", "checkout", "-q", "-b", "wick/issue-9-run-9"], work)

        # No changes yet: committing must refuse rather than create an empty commit.
        try:
            commit_and_push(work, "wick/issue-9-run-9", "empty")
        except RuntimeError as e:
            assert "没有可提交的改动" in str(e), e
        else:
            raise AssertionError("空 worktree 应该拒绝提交")

        (work / "a.py").write_text("x = 2\n", encoding="utf-8")
        sha = commit_and_push(work, "wick/issue-9-run-9", "fix: 测试提交")
        assert len(sha) >= 7, sha

        # The branch really landed on the remote.
        out = _run(["git", "ls-remote", "--heads", str(bare)], tmp)
        assert "wick/issue-9-run-9" in out, out

        # Unknown action is refused before touching anything.
        try:
            execute_approval({"action": "删库", "repo": "a/b", "branch": "x"}, work)
        except RuntimeError as e:
            assert "不认识的动作" in str(e), e
        else:
            raise AssertionError("未知动作应该被拒绝")

        # A missing worktree is refused, not guessed at.
        try:
            execute_approval({"action": "提 PR", "repo": "a/b", "branch": "x"},
                             tmp / "does-not-exist")
        except RuntimeError as e:
            assert "worktree 已不存在" in str(e), e
        else:
            raise AssertionError("缺失 worktree 应该被拒绝")

    # Commit prefix follows the issue's labels instead of a hardcoded `fix:`.
    assert _commit_prefix(["docs"]) == "docs"
    assert _commit_prefix(["documentation"]) == "docs"
    assert _commit_prefix(["chore"]) == "chore"
    assert _commit_prefix(["enhancement"]) == "feat"
    assert _commit_prefix(["bug"]) == "fix"
    assert _commit_prefix(["tech-debt"]) == "fix"
    assert _commit_prefix([]) == "fix"
    assert _commit_prefix(["Docs"]) == "docs", "label matching must be case-insensitive"
    assert _commit_prefix(["reliability", "docs"]) == "docs", "docs wins over a fix-ish label"

    # The PR body must carry the three sections CONTRIBUTING requires.
    body = _pr_body(
        {"files": ["a.py", "b.py"], "diff": "+10/-2", "summary": "改了 a.py 的连接泄漏",
         "checks": "9/9"},
        "wick/issue-1-run-1", "abc1234", 1, "cmyyy/wick",
    )
    for section in ("Closes #1", "## 改了什么", "## 怎么验证的", "## 遗留"):
        assert section in body, f"PR body 缺 {section}"
    assert "改了 a.py 的连接泄漏" in body and "9/9" in body
    assert "`a.py`" in body and "`b.py`" in body
    assert "abc1234" in body and "wick/issue-1-run-1" in body

    # Empty detail must still produce a valid, sectioned body.
    bare = _pr_body({}, "b", "sha", 2, "a/b")
    assert "## 改了什么" in bare and "（见 diff）" in bare, bare[:200]

    # "审了什么" is always present. CONTRIBUTING says the orchestrator's prose goes
    # through review; the section is the only visible trace when that is skipped, so
    # it must not be conditional on detail carrying a review field.
    assert "## 审了什么" in bare, "PR body 少了审了什么这一节"
    assert "未填写" in bare, "没写 review 时该显式说未填写，而不是安静地省略"
    with_review = _pr_body({"review": "reviewer: 2 条严重，已改"}, "b", "sha", 3, "a/b")
    assert "reviewer: 2 条严重，已改" in with_review
    assert "未填写" not in with_review, "填了 review 就不该再出现未填写"
    # And it sits between 怎么验证的 and 遗留, which is where a reviewer of the PR looks.
    assert bare.index("## 怎么验证的") < bare.index("## 审了什么") < bare.index("## 遗留")

    print("actions.py selftest OK: 空提交拒绝 / commit+push 落地 / 未知动作拒绝 / "
          "commit 前缀按 label / PR body 四节（含无 review 时的未填写）")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
