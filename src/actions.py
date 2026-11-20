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


def execute_approval(approval: dict, worktree: Path) -> dict:
    """Carry out one approved action. `approval` is the row's detail as a dict."""
    action = approval.get("action") or "提 PR"
    repo = approval["repo"]
    branch = approval["branch"]
    base = approval.get("base", "main")
    number = approval.get("number", "?")
    title = approval.get("title") or f"fix: issue #{number}"

    if action not in ("提 PR", "open_pr"):
        raise RuntimeError(f"不认识的动作: {action}")

    if not worktree.exists():
        raise RuntimeError(f"worktree 已不存在: {worktree}")

    sha = commit_and_push(
        worktree, branch,
        f"fix: {title} (#{number})\n\nAutomated by wick after human approval.\nApproved action: {action}",
    )
    url = open_pr(
        worktree, repo, branch, base,
        f"fix: {title} (#{number})",
        f"Closes #{number}\n\n"
        f"Automated by [wick](https://github.com/{repo}) after a human approved the action.\n\n"
        f"- Branch: `{branch}`\n- Commit: `{sha}`\n"
        f"- The project's four self-checks pass inside the worktree.\n",
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

    print("actions.py selftest OK: 空提交拒绝 / commit+push 落地 / 未知动作拒绝")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
