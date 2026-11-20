"""Agent invocation: turn one event into one Hermes run.

D3 chose the CLI over the gateway JSON-RPC door: `hermes -z` already prints only
the final response text (no banner, spinner, tool previews or session_id), so
the worker needs no WebSocket client, no control-ticket auth and no event
parsing. See DECISIONS.md for the CLI-versus-RPC trade-off.

ponytail: one subprocess per event, no pooling. Fine at a few events per hour;
if throughput ever matters, this is the place to add a resident gateway client.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
HERMES_BIN = os.environ.get("WICK_HERMES_BIN", "hermes")
DEFAULT_TIMEOUT = 900  # seconds; a real code-reading run can take minutes


def render_issue_prompt(payload: dict) -> str:
    """Fill src/prompts/issue.md with one issue's fields."""
    tpl = (PROMPT_DIR / "issue.md").read_text(encoding="utf-8")
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
    )


def run_prompt(prompt: str, cwd: str | Path | None = None,
               timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run one Hermes prompt via `hermes -z` and return its final text.

    Raises RuntimeError on a non-zero exit or a timeout: a failed run must be
    recorded as failed, never delivered as an empty success.
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


def verdict_of(text: str) -> str | None:
    """Extract the first-line verdict the prompt template asks for.

    Returns one of '可处理' / '需补充信息' / '不处理', or None when the model did
    not follow the format. Callers must treat None as "unstructured output" and
    still deliver it rather than silently dropping the run.
    """
    for line in text.splitlines():
        line = line.strip().lstrip("*").strip()
        if line.startswith("判定"):
            for v in ("可处理", "需补充信息", "不处理"):
                if v in line:
                    return v
    return None


def handle_event(event) -> str:
    """Event -> prompt -> run -> delivery text."""
    payload = json.loads(event["payload"] or "{}")
    prompt = render_issue_prompt(payload)
    return run_prompt(prompt)


# ---------------------------------------------------------------- selftest

def _selftest() -> int:
    """Self-check: template rendering, verdict parsing. No network, no model call."""
    payload = {
        "repo": "cmyyy/wick", "number": 7, "title": "outbox 节流算错",
        "url": "https://github.com/cmyyy/wick/issues/7",
        "labels": ["bug", "reliability"], "author": "cmyyy",
        "state": "open", "body": "日上限算成了 0 条，全部被压到次日。",
    }
    prompt = render_issue_prompt(payload)
    assert "cmyyy/wick" in prompt and "#7" in prompt, prompt[:200]
    assert "outbox 节流算错" in prompt and "bug, reliability" in prompt
    assert "日上限算成了 0 条" in prompt

    # Missing optional fields must not raise.
    minimal = render_issue_prompt({"repo": "a/b", "number": 1})
    assert "(无标题)" in minimal and "(正文为空)" in minimal and "(无)" in minimal

    # Verdict parsing, including the tolerant paths.
    assert verdict_of("判定：可处理\n\n原因：...") == "可处理"
    assert verdict_of("**判定：需补充信息**") == "需补充信息"
    assert verdict_of("判定: 不处理") == "不处理"
    assert verdict_of("我先看看代码……\n判定：可处理") == "可处理"
    assert verdict_of("没有按格式输出") is None

    print("agent.py selftest OK: 模板渲染 / 缺字段容错 / 判定解析")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
