#!/usr/bin/env python3
"""Hard-metric grader for wick's own agent runs (v1.2).

Only computes what can be automated; rubric scoring and judge calibration are
handled separately.

Usage:
  python run.py --repo D:/AI/hermes-agent --worktree D:/tmp/wt --base main
  python run.py --selftest          # self-check

Output: a JSON report (stdout plus eval/reports/<timestamp>.json).

Metric definitions (see docs/PROCESS-code-agent-v1.2.md):
  - regression_failed : pre-existing tests broken by the change (red line: must be empty)
  - tests             : pass/fail counts
  - diff              : changed files, added/removed lines, whether tests were touched
  - hallucinated      : files named in the writeup that do not exist in the repo (red line: <2%)
  - trajectory        : edits_per_failure and friends; needs a session log, stubbed for now
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


def _run(cmd: list[str], cwd: Path | str, timeout: int = 600) -> tuple[int, str]:
    """Run a command, returning (returncode, combined output). Never raises."""
    try:
        p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT after {timeout}s"
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]}"


# Build and cache artifacts are not "changes". Real repos have a .gitignore for
# this, but the grader also runs on throwaway repos, so filter here as well.
_NOISE = re.compile(
    r"(^|/)(__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|node_modules|\.venv|venv|dist|build|\.tox)/"
    r"|\.(pyc|pyo|log|tmp|swp)$"
)


def diff_stats(worktree: Path, base: str = "main") -> dict:
    """Change stats: file list and line counts, including untracked new files."""
    rc, out = _run(["git", "diff", "--numstat", base], worktree)
    if rc != 0:
        rc, out = _run(["git", "diff", "--numstat", "HEAD"], worktree)
    files, added, removed = [], 0, 0
    for line in out.strip().splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        a, r, name = parts
        files.append(name)
        added += int(a) if a.isdigit() else 0
        removed += int(r) if r.isdigit() else 0

    # Untracked new files: git diff ignores them, but files an agent created are changes.
    rc_u, out_u = _run(["git", "ls-files", "--others", "--exclude-standard"], worktree)
    if rc_u == 0:
        for name in [x for x in out_u.strip().splitlines() if x.strip()]:
            if name in files or _NOISE.search(name):
                continue
            files.append(name)
            try:
                added += len((worktree / name).read_text(encoding="utf-8", errors="replace").splitlines())
            except OSError:
                pass

    return {
        "files": files,
        "file_count": len(files),
        "lines_added": added,
        "lines_removed": removed,
        "touched_tests": any(re.search(r"(^|/)tests?/|test_\w+\.py$|_test\.py$", f) for f in files),
    }


def run_tests(worktree: Path, python_exe: str = sys.executable, timeout: int = 900) -> dict:
    """Run pytest and parse the result.

    A missing pytest or an empty collection is reported as unavailable: silently
    returning 0 failed would look like a pass and turn a red line into a false green.
    """
    rc, out = _run([python_exe, "-m", "pytest", "-q", "--tb=no"], worktree, timeout=timeout)

    if "No module named pytest" in out or rc == 127:
        return {"available": False, "reason": "pytest 未安装（用 --py 指定带 pytest 的解释器）",
                "exit_code": rc, "raw_tail": out.strip().splitlines()[-3:]}
    if rc == 124:
        return {"available": False, "reason": "pytest 超时", "exit_code": rc,
                "raw_tail": out.strip().splitlines()[-3:]}

    passed = failed = 0
    m = re.search(r"(\d+) passed", out)
    if m:
        passed = int(m.group(1))
    m = re.search(r"(\d+) failed", out)
    if m:
        failed = int(m.group(1))
    # Zero collected tests also counts as unavailable; "0 failed" must not read as a pass.
    if passed == 0 and failed == 0:
        return {"available": False, "reason": "没有收集到任何测试用例", "exit_code": rc,
                "raw_tail": out.strip().splitlines()[-5:]}

    return {
        "available": True,
        "exit_code": rc,
        "passed": passed,
        "failed": failed,
        "failed_names": re.findall(r"^FAILED (\S+)", out, re.M),
        "raw_tail": out.strip().splitlines()[-5:] if out.strip() else [],
    }


def extract_mentions(text: str) -> list[str]:
    """Extract file paths the writeup claims to have changed or read."""
    pat = r"[\w./-]+\.(?:py|js|ts|md|yaml|yml|json|toml)"
    return sorted(set(re.findall(pat, text)))


def hallucination_check(text: str, repo: Path) -> dict:
    """Hallucination check: do the named files actually exist?"""
    mentions = extract_mentions(text)
    missing = [m for m in mentions if not (repo / m).exists()]
    return {
        "mentions": len(mentions),
        "missing": missing,
        "rate": round(len(missing) / len(mentions), 4) if mentions else 0.0,
    }


def trajectory_from_log(log_path: Path | None) -> dict:
    """Trajectory metrics. Stub for v1: needs a per-step action sequence.

    Returns unavailable until the Hermes session-log parser lands.
    """
    if not log_path or not Path(log_path).exists():
        return {"available": False, "note": "需要会话日志（每步动作序列），待接入"}
    return {"available": False, "note": "日志解析器待实现（edits_per_failure / revert_ratio）"}


def grade(repo: Path, worktree: Path, base: str, answer_text: str = "",
          log_path: Path | None = None, python_exe: str = sys.executable) -> dict:
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "repo": str(repo),
        "worktree": str(worktree),
        "base": base,
        "tests": run_tests(worktree, python_exe),
        "diff": diff_stats(worktree, base),
        "hallucination": hallucination_check(answer_text, repo) if answer_text else None,
        "trajectory": trajectory_from_log(log_path),
    }
    # Red-line evaluation.
    report["red_lines"] = {
        "tests_unavailable": not report["tests"].get("available", False),
        "regression_failed": report["tests"].get("failed", 0) > 0,
        "hallucination_over_2pct": bool(report["hallucination"] and report["hallucination"]["rate"] >= 0.02),
    }
    report["pass_red_lines"] = not any(report["red_lines"].values())
    return report


def selftest() -> int:
    """Self-check: diff_stats and hallucination detection against a throwaway git repo."""
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        _run(["git", "init", "-q", "-b", "main"], repo)
        _run(["git", "config", "user.email", "t@t"], repo)
        _run(["git", "config", "user.name", "t"], repo)
        (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
        _run(["git", "add", "."], repo)
        _run(["git", "commit", "-qm", "init"], repo)

        # Modify one tracked file.
        (repo / "a.py").write_text("x = 2\ny = 3\n", encoding="utf-8")
        st = diff_stats(repo, "HEAD")
        assert st["file_count"] == 1, st
        assert st["files"] == ["a.py"], st
        assert st["lines_added"] == 2 and st["lines_removed"] == 1, st
        assert st["touched_tests"] is False, st

        # Touch a test file.
        (repo / "test_a.py").write_text("def test_x(): assert True\n", encoding="utf-8")
        st2 = diff_stats(repo, "HEAD")
        assert st2["touched_tests"] is True, st2

        # Hallucination detection.
        h = hallucination_check("改了 a.py 和 b.py，还看了 nope/c.py", repo)
        assert h["mentions"] == 3, h
        assert sorted(h["missing"]) == ["b.py", "nope/c.py"], h
        assert h["rate"] > 0, h

        print("selftest OK: diff_stats / touched_tests / hallucination_check")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", help="目标仓库（真实代码所在）")
    ap.add_argument("--worktree", help="跑任务的 worktree")
    ap.add_argument("--base", default="main", help="对比基线分支")
    ap.add_argument("--answer", default="", help="agent 的说明文字（用于幻觉检测）")
    ap.add_argument("--log", default=None, help="会话日志路径（轨迹指标）")
    ap.add_argument("--py", default=sys.executable, help="跑测试用的解释器（需装 pytest）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not (args.repo and args.worktree):
        ap.error("需要 --repo 和 --worktree（或用 --selftest）")

    report = grade(Path(args.repo), Path(args.worktree), args.base,
                   args.answer, Path(args.log) if args.log else None, args.py)

    outdir = Path(__file__).parent / "reports"
    outdir.mkdir(exist_ok=True)
    outfile = outdir / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    outfile.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n报告已存：{outfile}", file=sys.stderr)
    return 0 if report["pass_red_lines"] else 1


if __name__ == "__main__":
    sys.exit(main())
