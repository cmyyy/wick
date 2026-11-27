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
# github.com is unreachable without the local proxy on this machine; `git push`
# goes through it. api.github.com is the opposite: the proxy refuses to CONNECT
# there, so `gh` has to reach it directly (#40 T1). One HTTPS_PROXY cannot serve
# both, hence a per-program NO_PROXY instead of a global one.
# Empty string means "leave the env alone".
PROXY = os.environ.get("WICK_GITHUB_PROXY", "http://127.0.0.1:7890")
# The one host `gh` must bypass the proxy for. Only `gh` gets it: git needs the
# proxy for github.com, and excluding both would break the push instead.
GH_DIRECT_HOST = "api.github.com"


def _gh_bin() -> str:
    for gh in GH_CANDIDATES:
        if not gh:
            continue
        if gh == "gh" or Path(gh).exists():
            return gh
    raise RuntimeError("找不到 gh CLI（设 WICK_GH_BIN 或装 GitHub CLI）")


def _is_gh(argv0: str) -> bool:
    """Is this executable gh? The proxy rule differs per program, so the child's
    env is decided by what is being run, not by who called `_run`."""
    return Path(argv0).name.lower().removesuffix(".exe") == "gh"


def _child_env(argv0: str) -> dict:
    """Env for one child process, keyed on the program it runs.

    `git push` keeps the proxy (github.com is only reachable through it); `gh`
    gets NO_PROXY=api.github.com so it talks to the API directly (#40 T1). Both
    keep HTTPS_PROXY -- the fix is an exemption, not a removal.
    """
    env = {**os.environ}
    if not PROXY:
        return env
    env.setdefault("HTTPS_PROXY", PROXY)
    env.setdefault("HTTP_PROXY", PROXY)
    if _is_gh(argv0):
        # Merge with whatever NO_PROXY the caller already exported: clobbering it
        # would silently widen the exemption for everything else gh calls.
        hosts = [h.strip() for h in (env.get("NO_PROXY") or "").split(",") if h.strip()]
        if GH_DIRECT_HOST not in [h.lower() for h in hosts]:
            hosts.insert(0, GH_DIRECT_HOST)
        # gh is a Go program: it reads both spellings, so set both.
        for key in ("NO_PROXY", "no_proxy"):
            env[key] = ",".join(hosts)
    return env


def _run(args: list[str], cwd: Path | str, timeout: int = 180) -> str:
    p = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout,
                       env=_child_env(args[0]))
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])} 失败（{p.returncode}）: {out[-600:]}")
    return out


def _already_committed(worktree: Path, branch: str) -> bool:
    """Does HEAD already carry the commit this approval was meant to make?

    The "nothing staged" branch below is reached in three states, and only the
    last one is a real no-op (#40 T2):

    * `origin/<branch>` == HEAD: the commit AND the push landed and only
      `open_pr` died. This is the state the first real run got stuck in --
      re-running used to raise forever with the work already on the remote --
      so it must hand back that sha for the PR step.
    * HEAD ahead of `origin/<branch>` (or of every remote, when the push never
      landed): the commit exists, the push does not; finish it.
    * neither: nothing was committed here at all, and an empty commit would lie.

    A branch freshly cut from main has no `origin/<branch>` and no commit of its
    own, so it stays in the last bucket even though HEAD == origin/main. That is
    why the comparison is against the branch's own remote ref, not "any remote".
    """
    def git(*args: str) -> str | None:
        p = subprocess.run(["git", *args], cwd=str(worktree), capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        return (p.stdout or "").strip() if p.returncode == 0 else None

    head = git("rev-parse", "HEAD")
    remote = git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}")
    if remote is not None and remote == head:
        # Already pushed: `git push` will say "Everything up-to-date", and the
        # only thing left to redo is the PR.
        return True
    if remote is not None:
        counting = ["rev-list", "--count", f"origin/{branch}..HEAD"]
    else:
        # The push never landed, so there is no remote branch to compare against:
        # only a commit that no remote has at all is worth finishing.
        counting = ["rev-list", "--count", "HEAD", "--not", "--remotes"]
    count = git(*counting)
    return bool(count) and int(count) > 0


def commit_and_push(worktree: Path, branch: str, message: str) -> str:
    """Commit everything in the worktree and push the branch. Returns the commit sha.

    Idempotent after a half success (#40 T2): when an earlier attempt already
    committed -- with or without the push -- and only `open_pr` failed, re-running
    finishes the job and returns that commit instead of refusing. A clean worktree
    with no commit of its own is still a real no-op and still raises.
    """
    _run(["git", "add", "-A"], worktree)
    rc = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=str(worktree)).returncode
    if rc == 0:
        # "Nothing staged" is not "nothing happened": the first real run committed
        # and pushed, then died in `open_pr`, and every retry tripped over this
        # guard with the commit sitting on the remote (#40 T2).
        if not _already_committed(worktree, branch):
            raise RuntimeError("worktree 里没有可提交的改动")
        sha = _run(["git", "rev-parse", "--short", "HEAD"], worktree)
        # Pushing an already-pushed commit exits 0 ("Everything up-to-date"), so
        # the recovery path is safe to take whenever the commit exists: it either
        # closes the missing push or does nothing.
        _run(["git", "push", "-u", "origin", branch], worktree, timeout=300)
        return sha
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


def _ref_suffix(number) -> str:
    """' (#12)' when there is an issue number, '' for a hand-queued task.

    A manual task has no number, and `(#?)` in a commit subject or PR title is a
    reference to nothing (#38 F3). Placeholder values count as "no number".
    """
    return f" (#{number})" if number not in (None, "", "?") else ""


def _pr_body(detail: dict, branch: str, sha: str, number: int | str | None, repo: str) -> str:
    """PR body with the sections CONTRIBUTING requires."""
    files = detail.get("files") or []
    files_block = "\n".join(f"- `{f}`" for f in files[:20]) or "- （未记录）"
    summary = (detail.get("summary") or "").strip() or "（见 diff）"
    checks = detail.get("checks") or "?"
    # No number means a hand-queued task, not a missing value to paper over:
    # writing `Closes #?` would make the PR body claim a link it does not have
    # (#38 F3). Omit the line instead of printing a placeholder.
    closes = f"Closes #{number}\n\n" if number not in (None, "", "?") else ""
    return (
        f"{closes}"
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
    number = approval.get("number")
    ref = _ref_suffix(number)
    title = approval.get("title") or (f"issue #{number}" if number else "手派任务")
    prefix = _commit_prefix(approval.get("labels") or [])

    if action not in ("提 PR", "open_pr"):
        raise RuntimeError(f"不认识的动作: {action}")

    if not worktree.exists():
        raise RuntimeError(f"worktree 已不存在: {worktree}")

    sha = commit_and_push(
        worktree, branch,
        f"{prefix}: {title}{ref}\n\n"
        f"Automated by wick after a human approved the action.\n"
        f"Approved action: {action}\n",
    )
    url = open_pr(
        worktree, repo, branch, base,
        f"{prefix}: {title}{ref}",
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

        # T2 (#40): three clean-worktree states, and only one of them is a no-op.
        # Control 1 -- nothing happened at all: the branch is fresh, never pushed,
        # and has no commit of its own, so an empty commit would be a lie.
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

        # Control 2 -- the reported half success: commit AND push landed, only
        # `open_pr` died. Clean worktree, HEAD == origin/<branch>. This is the
        # state the first real run was stuck in: it used to raise "没有可提交的
        # 改动" forever. It must hand back the same sha so the PR step can run.
        # Drop the equality case in _already_committed and this goes red.
        pushed_sha = _run(["git", "rev-parse", "HEAD"], work)
        commits_before_retry = _run(["git", "rev-list", "--count", "HEAD"], work)
        assert _run(["git", "rev-parse", "origin/wick/issue-9-run-9"], work) == pushed_sha, \
            "前置条件：push 已经落地"
        retried = commit_and_push(work, "wick/issue-9-run-9", "半成功后重跑")
        assert retried == sha, f"重跑应该返回已有的 commit {sha!r}，而不是 {retried!r}"
        assert _run(["git", "rev-parse", "HEAD"], work) == pushed_sha, "恢复路径不能改动 HEAD"
        assert _run(["git", "rev-list", "--count", "HEAD"], work) == commits_before_retry, \
            "恢复路径不能凭空多造一个 commit"

        # Control 3 -- commit landed, push did not. Same clean worktree, but HEAD
        # is ahead of origin/<branch>: recover and finish the push.
        (work / "a.py").write_text("x = 3\n", encoding="utf-8")
        _run(["git", "commit", "-qam", "本地已提交，但 push 没成功"], work)
        ahead_sha = _run(["git", "rev-parse", "HEAD"], work)
        commits_before = _run(["git", "rev-list", "--count", "HEAD"], work)
        assert _already_committed(work, "wick/issue-9-run-9"), "HEAD 已领先 origin，应该认得出来"
        recovered = commit_and_push(work, "wick/issue-9-run-9", "恢复路径不该再提交")
        assert recovered == _run(["git", "rev-parse", "--short", "HEAD"], work), recovered
        assert _run(["git", "rev-list", "--count", "HEAD"], work) == commits_before, \
            "恢复路径不能凭空多造一个 commit"
        assert ahead_sha in _run(["git", "ls-remote", "--heads", str(bare)], tmp), \
            "恢复路径没有把已有的 commit push 上去，后面的 open_pr 还是会失败"

        # Control 4 -- the branch was never pushed at all, so there is no
        # origin/<branch> to compare against: the commit has to be found via
        # "no remote has it". Delete that fallback and this goes red.
        _run(["git", "checkout", "-q", "-b", "wick/issue-9-run-10"], work)
        (work / "a.py").write_text("x = 4\n", encoding="utf-8")
        _run(["git", "commit", "-qam", "分支还没推过"], work)
        never_pushed = _run(["git", "rev-parse", "HEAD"], work)
        assert _already_committed(work, "wick/issue-9-run-10"), \
            "从未 push 过的分支上已经有 commit，应该认得出来"
        assert commit_and_push(work, "wick/issue-9-run-10", "恢复路径不该再提交") == \
            _run(["git", "rev-parse", "--short", "HEAD"], work)
        assert never_pushed in _run(["git", "ls-remote", "--heads", str(bare)], tmp), \
            "从未 push 过的分支，恢复路径应该把已有的 commit 推上去"

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

    # The PR body must carry the sections CONTRIBUTING requires.
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

    # F3 (#38): a hand-queued task has no issue number. Omitting it must drop the
    # `Closes #?` line and every `(#?)` placeholder, not ship a link to nothing.
    no_number = _pr_body({"files": ["a.md"], "summary": "改了文档"},
                         "wick/issue-manual-run-1-attempt-1", "sha1", None, "cmyyy/wick")
    assert "Closes" not in no_number and "#?" not in no_number, no_number[:300]
    assert no_number.startswith("## 改了什么"), no_number[:80]
    assert _ref_suffix(None) == "" and _ref_suffix("?") == "" and _ref_suffix("") == ""
    assert _ref_suffix(12) == " (#12)"

    # T1 (#40): gh must reach api.github.com directly, git push must keep the
    # proxy. Two stub programs named exactly `gh` and `git` print the env their
    # child actually received -- asserting on _child_env() alone would stay green
    # if _run() stopped passing it down. No network: the question is env, not reach.
    import shutil

    def _stub_program(folder: Path, name: str) -> str:
        """An executable literally named `name` that dumps its own environment.

        The rule keys on the executable, so a stub with another name would prove
        nothing. Windows' printenv.exe prints the env whoever calls it, so a copy
        renamed `gh.exe` is still printenv -- that is the stub there.

        On POSIX that trick is wrong: printenv is a multi-call binary (GNU
        coreutils, busybox) that dispatches on argv[0]. Copied to `gh` it looks
        itself up as "gh", fails with `coreutils: unknown program 'gh'`, and the
        check ends up asserting coreutils' program table instead of the env --
        green on Windows, red on CI. A shebang script names printenv in its body,
        so what the file is called cannot matter. Written as bytes, not text: a
        CRLF after the shebang would break it on Linux.
        """
        if os.name == "nt":
            src = shutil.which("printenv")
            if not src:
                raise RuntimeError("selftest 需要 printenv（git 自带）")
            dst = folder / (name + ".exe")
            shutil.copy2(src, dst)
            return str(dst)
        dst = folder / name
        dst.write_bytes(b"#!/bin/sh\nexec printenv\n")
        dst.chmod(0o755)
        return str(dst)

    def _env_of(dump: str) -> dict:
        """Parse `printenv` output into env vars, uppercased.

        Windows env blocks are case-insensitive, so a child that was handed both
        NO_PROXY and no_proxy may only see one of them; comparing uppercase keys
        keeps the check about the value, not the spelling the OS happened to keep.
        """
        pairs = (line.split("=", 1) for line in dump.splitlines() if "=" in line)
        return {k.upper(): v for k, v in pairs}

    global PROXY
    saved_proxy = PROXY
    PROXY = "http://127.0.0.1:7890"  # pinned: this check is about the env shape
    saved_np = {k: os.environ.pop(k) for k in ("NO_PROXY", "no_proxy") if k in os.environ}
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            gh_env = _env_of(_run([_stub_program(tmp, "gh")], tmp))
            git_env = _env_of(_run([_stub_program(tmp, "git")], tmp))
        assert GH_DIRECT_HOST in gh_env.get("NO_PROXY", ""), (
            f"gh 子进程的 NO_PROXY 里没有 {GH_DIRECT_HOST}：{gh_env.get('NO_PROXY')!r}")
        assert "NO_PROXY" not in git_env, (
            f"git 子进程不该排除代理（git push 要走代理），却拿到 NO_PROXY={git_env.get('NO_PROXY')!r}")
        # Both still see the proxy: T1 is an exemption for one host, not "drop it".
        assert gh_env.get("HTTPS_PROXY") and git_env.get("HTTPS_PROXY"), (
            f"两边都应该拿到代理：gh={gh_env.get('HTTPS_PROXY')!r} git={git_env.get('HTTPS_PROXY')!r}")
    finally:
        PROXY = saved_proxy
        os.environ.update(saved_np)

    print("actions.py selftest OK: 空提交拒绝 / commit+push 落地 / 未知动作拒绝 / "
          "commit 前缀按 label / PR body 四节（含无 review 时的未填写） / 无编号时不写 Closes #? / "
          "半成功后重跑能恢复（干净且真没改动仍拒绝） / gh 的 NO_PROXY 含 api.github.com 而 git 不含")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
