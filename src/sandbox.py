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


def branch_name(event_id: int, issue_number: int | str) -> str:
    return f"wick/issue-{issue_number}-run-{event_id}"


def create(repo: Path, event_id: int, issue_number: int | str, base: str = "HEAD") -> tuple[Path, str]:
    """Create one worktree on a fresh branch. Returns (worktree_path, branch)."""
    branch = branch_name(event_id, issue_number)
    path = WORKTREE_ROOT / branch.replace("/", "-")
    WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)

    if path.exists():
        remove(repo, path, branch)  # a retry of the same event must start clean

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
        m = re.match(r"wick-issue-(.+)-run-(\d+)$", name)
        branch = f"wick/issue-{m.group(1)}-run-{m.group(2)}" if m else None
        remove(repo, p, branch)
        removed.append(name)
    return removed


# ---------------------------------------------------------------- selftest

def _selftest() -> int:
    """Self-check: create -> edit -> diff -> remove, against a throwaway repo."""
    import tempfile

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
            path, branch = create(repo, event_id=7, issue_number=2)
            assert path.exists() and branch == "wick/issue-2-run-7", (path, branch)

            # The main tree must be untouched by an edit inside the worktree.
            (path / "a.py").write_text("x = 2\ny = 3\n", encoding="utf-8")
            (path / "new_file.py").write_text("z = 9\n", encoding="utf-8")
            assert (repo / "a.py").read_text(encoding="utf-8") == "x = 1\n"

            d = diff(repo, path, base="HEAD")
            assert d["file_count"] == 2, d["files"]
            assert set(d["files"]) == {"a.py", "new_file.py"}, d["files"]
            assert "+x = 2" in d["patch"] and "+z = 9" in d["patch"], d["patch"][:300]
            assert "new_file.py" in d["patch"], "untracked file missing from patch"

            # Re-creating the same event's worktree starts from a clean base.
            path2, _ = create(repo, event_id=7, issue_number=2)
            assert (path2 / "a.py").read_text(encoding="utf-8") == "x = 1\n"

            removed = prune(repo)
            assert removed, removed
            assert not path2.exists()
        finally:
            WORKTREE_ROOT = original_root

    print("sandbox.py selftest OK: 建 worktree / 主树隔离 / diff 含未跟踪文件 / 清理")
    return 0


if __name__ == "__main__":
    if "--prune" in sys.argv:
        target = Path(sys.argv[1]) if len(sys.argv) > 2 and not sys.argv[1].startswith("-") else PROJECT_ROOT
        print("已清理:", prune(target))
    else:
        raise SystemExit(_selftest())
