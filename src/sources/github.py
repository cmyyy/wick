"""GitHub issue 事件源：since 增量轮询。

设计要点：
- 只用 stdlib（urllib），不为几十行 HTTP 引一个依赖
- token 优先复用 gh CLI 的认证（`gh auth token`），其次读环境变量 GITHUB_TOKEN
- 水位线存 DB meta 表，用「本次 poll 的开始时刻」而不是「最后一条 issue 的 updated_at」：
  后者在有并发更新/分页交错时会漏事件，前者最坏只是多拉一次（去重会挡掉）
- PR 也会从 issues API 返回，必须按 `pull_request` 字段排除，否则会把 PR 当 issue 处理
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import db  # noqa: E402

API = "https://api.github.com"
GH_CANDIDATES = [
    os.environ.get("WICK_GH_BIN"),
    str(Path.home() / "AppData" / "Local" / "gh-cli" / "bin" / "gh.exe"),
    "gh",
]
BODY_LIMIT = 4000  # issue 正文可能很长，只留前面这段给 prompt


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_token() -> str | None:
    """优先用 gh CLI 的 token（不落盘、不打印），其次环境变量。"""
    for env in ("WICK_GITHUB_TOKEN", "GITHUB_TOKEN"):
        v = os.environ.get(env)
        if v:
            return v.strip()
    for gh in GH_CANDIDATES:
        if not gh:
            continue
        try:
            p = subprocess.run([gh, "auth", "token"], capture_output=True,
                               text=True, timeout=15)
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    return None


def _api_get(path: str, token: str | None, params: dict | None = None) -> tuple[list, dict]:
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "wick-agent")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r), dict(r.headers)
    except urllib.error.HTTPError as e:
        hint = {
            401: "token 无效或已过期",
            403: "权限不足或触发限流",
            404: "仓库不存在或无权限",
        }.get(e.code, "")
        raise RuntimeError(f"GitHub API {e.code} {hint}: {path}") from e


def fetch_issues(repo: str, since: str | None = None, token: str | None = None,
                 per_page: int = 100, max_pages: int = 10) -> list[dict]:
    """拉取 issue（排除 PR），按 updated_at 升序。"""
    out: list[dict] = []
    params = {
        "state": "all",
        "sort": "updated",
        "direction": "asc",
        "per_page": str(per_page),
    }
    if since:
        params["since"] = since

    path = f"/repos/{repo}/issues"
    for _ in range(max_pages):
        page, _headers = _api_get(path, token, {**params, "page": str(len(out) // per_page + 1)})
        if not isinstance(page, list):
            raise RuntimeError(f"意外的响应类型: {type(page)}")
        out.extend(page)
        if len(page) < per_page:
            break
    return out


def summarize(issue: dict, repo: str) -> dict:
    """把 GitHub 的 issue 压成 worker 需要的最小 payload。"""
    body = issue.get("body") or ""
    if len(body) > BODY_LIMIT:
        body = body[:BODY_LIMIT] + "\n\n…（正文过长已截断）"
    return {
        "number": issue["number"],
        "title": issue.get("title", ""),
        "body": body,
        "url": issue.get("html_url", ""),
        "state": issue.get("state"),
        "labels": [l["name"] for l in issue.get("labels", [])],
        "author": (issue.get("user") or {}).get("login"),
        "repo": repo,
    }


def poll(conn, repo: str, fetch=None, token: str | None = None,
         lookback_days: int = 7) -> dict:
    """增量轮询一次，把新 issue 写进 events。返回统计。

    fetch 可注入，便于自检时不打网络。
    """
    started = _now()  # 水位用开始时刻：最坏多拉一次，绝不漏
    meta_key = f"github:{repo}:last_poll"
    since = db.get_meta(conn, meta_key)
    if not since:
        since = _iso(started - timedelta(days=lookback_days))

    if fetch is None:
        token = token if token is not None else resolve_token()
        fetch = lambda **kw: fetch_issues(token=token, **kw)  # noqa: E731

    raw = fetch(repo=repo, since=since)
    stats = {"fetched": len(raw), "skipped_pr": 0, "created": 0, "duplicate": 0}

    for issue in raw:
        if "pull_request" in issue:  # PR 不是 issue 事件
            stats["skipped_pr"] += 1
            continue
        _eid, created = db.enqueue(
            conn, "github_issue", f"{repo}#{issue['number']}", summarize(issue, repo)
        )
        stats["created" if created else "duplicate"] += 1

    db.set_meta(conn, meta_key, _iso(started))
    stats["since"] = since
    stats["next_since"] = _iso(started)
    return stats


# --------------------------------------------------------------- selftest

def _selftest() -> int:
    """自检：PR 过滤、去重、水位推进、正文截断。不打网络。"""
    conn = db.connect(":memory:")
    repo = "cmyyy/wick"

    def fake_fetch(repo, since):
        return [
            {"number": 1, "title": "issue 一", "body": "正文", "html_url": "u1",
             "state": "open", "labels": [{"name": "tech-debt"}], "user": {"login": "cmyyy"}},
            {"number": 2, "title": "其实是个 PR", "body": "", "html_url": "u2",
             "state": "open", "labels": [], "user": {"login": "cmyyy"},
             "pull_request": {"url": "x"}},
            {"number": 3, "title": "长正文", "body": "x" * 5000, "html_url": "u3",
             "state": "open", "labels": [], "user": {"login": "cmyyy"}},
        ]

    s1 = poll(conn, repo, fetch=fake_fetch)
    assert s1["fetched"] == 3, s1
    assert s1["skipped_pr"] == 1, s1          # PR 被排除
    assert s1["created"] == 2, s1             # 只进 2 条
    assert s1["duplicate"] == 0, s1

    # payload 正确
    row = conn.execute("SELECT payload FROM events WHERE source_id=?",
                       (f"{repo}#1",)).fetchone()
    p = json.loads(row["payload"])
    assert p["title"] == "issue 一" and p["labels"] == ["tech-debt"], p
    long_row = conn.execute("SELECT payload FROM events WHERE source_id=?",
                            (f"{repo}#3",)).fetchone()
    assert "已截断" in json.loads(long_row["payload"])["body"]

    # 第二次 poll：同批数据不产生新事件
    s2 = poll(conn, repo, fetch=fake_fetch)
    assert s2["created"] == 0 and s2["duplicate"] == 2, s2
    assert conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 2

    # 水位推进了，且第二次传入的 since 是第一次的开始时刻
    assert db.get_meta(conn, f"github:{repo}:last_poll") is not None
    captured = {}

    def capture_fetch(repo, since):
        captured["since"] = since
        return []

    poll(conn, repo, fetch=capture_fetch)
    assert captured["since"].endswith("Z"), captured

    conn.close()
    print("github.py selftest OK: PR 过滤 / 去重 / 水位推进 / 正文截断")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
