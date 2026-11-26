"""Throwaway git worktrees: one per run, so a run can never touch the main tree.

A D4 run edits files. Letting it edit the live checkout would mean one bad run
can wreck the project mid-development, so every run gets its own worktree on its
own branch. The main tree stays clean; a human reviews the branch and decides.

ponytail: worktrees are kept after a run (not auto-deleted) because the point of
D4 is inspecting the diff. Cleanup is explicit: `python sandbox.py --prune`.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKTREE_ROOT = Path(
    os.environ.get("WICK_WORKTREES") or Path.home() / ".wick" / "worktrees"
)

# Reuse the grader's diff logic instead of writing a second, subtly different one.
sys.path.insert(0, str(PROJECT_ROOT / "eval"))
import run as grader  # noqa: E402


def _git(repo: Path, *args: str, timeout: int = 120) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _ref_token(value: int | str) -> str:
    """Turn a value into a token git accepts inside a branch name.

    A hand-queued task (#30) has no issue number, so this arrives as "?" -- and
    git rejects "?" in a ref. `..` is rejected too, so collapsing dot runs is not
    cosmetic: the old regex kept dots, and `wick/issue-..-run-7-attempt-2` fails
    `git check-ref-format` (#38 S3). Everything outside [0-9A-Za-z._-] becomes
    "-", consecutive dots collapse to one, and leading/trailing dots are dropped.
    """
    safe = re.sub(r"[^0-9A-Za-z._-]+", "-", str(value))
    safe = re.sub(r"\.{2,}", ".", safe)
    return safe.strip("-.") or "manual"


def branch_name(event_id: int, issue_number: int | str, attempt: int) -> str:
    """Branch for one attempt of one event.

    `attempt` is part of the name on purpose. git refuses to check out the same
    branch in two worktrees ("a branch named ... already exists"), and a retry
    must never land on the worktree a stalled attempt is still editing -- so the
    attempt gets its own path AND its own branch (issue #1).
    """
    # Sanitize here rather than at each caller: sandbox.create() is the only
    # producer of refs in the system, and the selftest proves the result against
    # `git check-ref-format` for hostile inputs, not against this regex.
    safe = _ref_token(issue_number)
    return f"wick/issue-{safe}-run-{event_id}-attempt-{attempt}"


def create(repo: Path, event_id: int, issue_number: int | str, base: str = "HEAD",
           *, attempt: int) -> tuple[Path, str]:
    """Create one worktree on a fresh branch. Returns (worktree_path, branch)."""
    branch = branch_name(event_id, issue_number, attempt)
    path = WORKTREE_ROOT / branch.replace("/", "-")
    WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)

    # Only this same attempt's leftovers can be here: a different attempt has a
    # different path, so cleanup can never delete a worktree another worker is in.
    if path.exists():
        remove(repo, path, branch)

    rc, out = _git(repo, "worktree", "add", "-b", branch, str(path), base)
    if rc != 0:
        raise RuntimeError(f"建 worktree 失败: {out.strip()[-500:]}")
    return path, branch


def remove(repo: Path, path: Path, branch: str | None = None) -> None:
    """Drop a worktree (and optionally its branch). Missing targets are not errors."""
    if path.exists():
        _git(repo, "worktree", "remove", "--force", str(path))
    _git(repo, "worktree", "prune")
    if branch:
        _git(repo, "branch", "-D", branch)


def diff(repo: Path, worktree: Path, base: str = "HEAD") -> dict:
    """Change stats plus the actual patch text, for review and scoring."""
    stats = grader.diff_stats(worktree, base)
    rc, patch = _git(worktree, "add", "-A")  # stage so untracked files show up in the diff
    if rc == 0:
        rc, patch = _git(worktree, "diff", "--cached", base, timeout=180)
    stats["patch"] = patch if rc == 0 else ""
    stats["patch_lines"] = stats["patch"].count("\n")
    return stats


def list_worktrees(repo: Path) -> list[str]:
    rc, out = _git(repo, "worktree", "list", "--porcelain")
    if rc != 0:
        return []
    return [line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("worktree ")]


def prune(repo: Path, keep_pattern: str | None = None) -> list[str]:
    """Delete wick-owned worktrees (optionally only those matching a pattern)."""
    removed = []
    root = WORKTREE_ROOT.resolve()
    for path_str in list_worktrees(repo):
        # git prints forward slashes on Windows; resolve before comparing.
        try:
            p = Path(path_str).resolve()
        except OSError:
            continue
        if p.parent != root:  # never touch a human's own worktree
            continue
        if keep_pattern and not re.search(keep_pattern, p.name):
            continue
        name = p.name
        m = re.match(r"wick-issue-(.+)-run-(\d+)(?:-attempt-(\d+))?$", name)
        if m:
            branch = f"wick/issue-{m.group(1)}-run-{m.group(2)}"
            if m.group(3):
                branch += f"-attempt-{m.group(3)}"  # else the branch outlives the directory, silently
        else:
            branch = None
        remove(repo, p, branch)
        removed.append(name)
    return removed


# ---------------------------------------------------------------- selftest

def _selftest() -> int:
    """Self-check: create -> edit -> diff -> remove, against a throwaway repo."""
    import subprocess as sp
    import tempfile

    # A hand-queued task (#30) has no issue number: it arrives as "?" and git
    # rejects "?" in a ref. #38 S3: the old regex kept dots, so ".." produced
    # `wick/issue-..-run-7-attempt-2`, which git rejects. Check every hostile
    # input against git itself rather than against our own regex.
    for odd_input in ("?", "..", "a..b", ".", "...", "?..?", "12.5", "a b/c"):
        odd = branch_name(7, odd_input, 2)
        chk = sp.run(["git", "check-ref-format", "--branch", odd],
                     capture_output=True, text=True)
        assert chk.returncode == 0, f"branch_name({odd_input!r}) 产出了 git 不接受的名: {odd}"
        assert ".." not in odd and "?" not in odd, odd
    assert branch_name(7, 123, 1) == "wick/issue-123-run-7-attempt-1"

    global WORKTREE_ROOT
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        repo = tmp / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "t@t")
        _git(repo, "config", "user.name", "t")
        (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "init")

        original_root = WORKTREE_ROOT
        WORKTREE_ROOT = tmp / "wt"
        try:
            path, branch = create(repo, event_id=7, issue_number=2, attempt=1)
            assert path.exists() and branch == "wick/issue-2-run-7-attempt-1", (path, branch)
            assert path.name == "wick-issue-2-run-7-attempt-1", path  # attempt 也进路径

            # The main tree must be untouched by an edit inside the worktree.
            (path / "a.py").write_text("x = 2\ny = 3\n", encoding="utf-8")
            (path / "new_file.py").write_text("z = 9\n", encoding="utf-8")
            assert (repo / "a.py").read_text(encoding="utf-8") == "x = 1\n"

            d = diff(repo, path, base="HEAD")
            assert d["file_count"] == 2, d["files"]
            assert set(d["files"]) == {"a.py", "new_file.py"}, d["files"]
            assert "+x = 2" in d["patch"] and "+z = 9" in d["patch"], d["patch"][:300]
            assert "new_file.py" in d["patch"], "untracked file missing from patch"

            # Issue #1 缺陷3：下一次尝试要落在另一个路径和另一个分支上。同一个分支
            # 不能同时 checkout 在两个 worktree（git 直接 rc=255），而共用路径会让
            # 接手的那次静默删掉前一次还在用的 worktree。
            path_b, branch_b = create(repo, event_id=7, issue_number=2, attempt=2)
            assert path_b != path and branch_b == "wick/issue-2-run-7-attempt-2", (path_b, branch_b)
            assert path.exists(), "attempt 2 把 attempt 1 的 worktree 删了"
            assert (path / "a.py").read_text(encoding="utf-8") == "x = 2\ny = 3\n", "两个 attempt 共用了同一个 worktree"
            listed = _git(repo, "branch", "--list", "wick/*")[1]
            assert "wick/issue-2-run-7-attempt-1" in listed and branch_b in listed, listed

            # Re-creating the same attempt starts from a clean base.
            path2, _ = create(repo, event_id=7, issue_number=2, attempt=1)
            assert path2 == path and (path2 / "a.py").read_text(encoding="utf-8") == "x = 1\n"

            # Cleanup must take the branches too, attempt suffix included: a prune
            # that only deletes directories leaves branches behind, silently.
            removed = prune(repo)
            assert set(removed) == {"wick-issue-2-run-7-attempt-1",
                                    "wick-issue-2-run-7-attempt-2"}, removed
            assert not path2.exists() and not path_b.exists()
            assert _git(repo, "branch", "--list", "wick/*")[1].strip() == "", _git(repo, "branch", "--list", "wick/*")[1]
        finally:
            WORKTREE_ROOT = original_root

    print("sandbox.py selftest OK: 建 worktree / 主树隔离 / diff 含未跟踪文件 / attempt 隔离 / 清理连分支")
    return 0


if __name__ == "__main__":
    if "--prune" in sys.argv:
        target = Path(sys.argv[1]) if len(sys.argv) > 2 and not sys.argv[1].startswith("-") else PROJECT_ROOT
        print("已清理:", prune(target))
    else:
        raise SystemExit(_selftest())
