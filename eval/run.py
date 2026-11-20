#!/usr/bin/env python3
"""Code Agent 评测评分器（v1.2）

只做**能自动算的硬指标**，rubric 打分 / judge 校准另做。

用法：
  python run.py --repo D:/AI/hermes-agent --worktree D:/tmp/wt --base main
  python run.py --selftest          # 自检

输出：JSON 报告（stdout + eval/reports/<时间戳>.json）

指标口径（对应 PROCESS.md v1.2）：
  - regression_failed : 原有测试是否有回归（红线：必须为空）
  - tests             : 通过/失败数
  - diff              : 改动文件、增删行数、是否碰到测试文件
  - hallucinated      : 报告里提到但仓库中不存在的文件/符号（红线：<2%）
  - trajectory        : edits_per_failure 等轨迹指标（需会话日志，第一版留接口）
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
    """跑命令，返回 (returncode, 合并输出)。不抛异常，失败也返回。"""
    try:
        p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT after {timeout}s"
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]}"


# 构建/缓存的产物不算"改动"（真实仓库通常有 .gitignore，这里再兜一层）
_NOISE = re.compile(
    r"(^|/)(__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|node_modules|\.venv|venv|dist|build|\.tox)/"
    r"|\.(pyc|pyo|log|tmp|swp)$"
)


def diff_stats(worktree: Path, base: str = "main") -> dict:
    """改动统计：文件清单、增删行数（含未跟踪的新文件）。"""
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

    # 未跟踪的新文件（git diff 不显示，但 agent 新建的文件必须算改动）
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
    """跑 pytest，解析结果。pytest 缺失或跑不起来时明确标记 unavailable，不能伪装成 0 失败。"""
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
    # 一条测试都没收集到，也算不可用（否则"0 失败"会被误判成通过）
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
    """从 agent 的说明文字里提取它声称改过/引用过的文件路径。"""
    pat = r"[\w./-]+\.(?:py|js|ts|md|yaml|yml|json|toml)"
    return sorted(set(re.findall(pat, text)))


def hallucination_check(text: str, repo: Path) -> dict:
    """幻觉检测：它提到的文件是否真实存在。"""
    mentions = extract_mentions(text)
    missing = [m for m in mentions if not (repo / m).exists()]
    return {
        "mentions": len(mentions),
        "missing": missing,
        "rate": round(len(missing) / len(mentions), 4) if mentions else 0.0,
    }


def trajectory_from_log(log_path: Path | None) -> dict:
    """轨迹指标。第一版留接口：需要会话日志里每步的动作序列。

    会话日志格式未定时返回 null，等接入 Hermes session log 后填。
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
    # 红线判定
    report["red_lines"] = {
        "tests_unavailable": not report["tests"].get("available", False),
        "regression_failed": report["tests"].get("failed", 0) > 0,
        "hallucination_over_2pct": bool(report["hallucination"] and report["hallucination"]["rate"] >= 0.02),
    }
    report["pass_red_lines"] = not any(report["red_lines"].values())
    return report


def selftest() -> int:
    """自检：临时 git repo 上验 diff_stats 与幻觉检测。"""
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        _run(["git", "init", "-q", "-b", "main"], repo)
        _run(["git", "config", "user.email", "t@t"], repo)
        _run(["git", "config", "user.name", "t"], repo)
        (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
        _run(["git", "add", "."], repo)
        _run(["git", "commit", "-qm", "init"], repo)

        # 改一个文件
        (repo / "a.py").write_text("x = 2\ny = 3\n", encoding="utf-8")
        st = diff_stats(repo, "HEAD")
        assert st["file_count"] == 1, st
        assert st["files"] == ["a.py"], st
        assert st["lines_added"] == 2 and st["lines_removed"] == 1, st
        assert st["touched_tests"] is False, st

        # 碰测试文件
        (repo / "test_a.py").write_text("def test_x(): assert True\n", encoding="utf-8")
        st2 = diff_stats(repo, "HEAD")
        assert st2["touched_tests"] is True, st2

        # 幻觉检测
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
