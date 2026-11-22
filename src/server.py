"""D8 panel: wick's own local page — the database's facts, in a browser.

    python src/server.py --port 9119      # 然后开 http://127.0.0.1:9119
    python src/server.py --selftest

stdlib only (`http.server` + `sqlite3` + `json`); no framework, no build step.
Why not a Hermes plugin: issue #5 — both plugin routes died on Hermes-internal
discovery/auth/version differences, none of which wick controls.

Five GETs (the page itself + four data blocks) and two POSTs (approve/reject).
A POST is accepted only from the panel page itself: it must carry
``X-Wick-Panel: 1`` (a non-simple header, so a cross-site page owes a preflight
this server never answers), say ``Sec-Fetch-Site: same-origin|none``, and
arrive with the Host we bound — see ``Handler._post_refusal``. Reads use a
``mode=ro`` connection, so opening the panel never creates a database, and a
missing table degrades to ``null`` ("cannot read") instead of an error page.
Approving only writes ``approvals.status``/``decided_at`` via
``db.decide_approval``; the one path that acts on an approval is
``worker.process_approvals`` (DECISIONS #7). This module never imports actions.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402

PANEL = Path(__file__).with_name("panel.html")
DEFAULT_PORT = 9119
LIMIT_DEFAULT = 50
LIMIT_MAX = 500
BODY_SUMMARY_CHARS = 120
STALE_KEY = "stats:stale_requeued"


def _ro(db_path) -> sqlite3.Connection:
    """Read-only connection. Refuses a missing file instead of creating one."""
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(db_path, sql, params: tuple = ()) -> list[dict]:
    conn = _ro(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _limit(query: dict) -> int:
    try:
        value = int(query.get("limit", [LIMIT_DEFAULT])[0])
    except (TypeError, ValueError):
        return LIMIT_DEFAULT
    return max(1, min(value, LIMIT_MAX))


# ------------------------------------------------------------------- 读四块
# Each reader folds its own failure into None ("cannot read"), so the page can
# tell "the queue is empty" ([]) apart from "the database is unreachable"
# (null). health() folds the same way, into {}.

def list_events(db_path, limit: int = LIMIT_DEFAULT) -> list[dict] | None:
    try:
        rows = _rows(
            db_path,
            "SELECT id, source_type, source_id, payload, status, attempts, due_at, "
            "created_at, updated_at, error FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    except Exception:
        return None
    for row in rows:
        try:
            row["payload"] = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            row["payload"] = {}
    return rows


def list_approvals(db_path) -> list[dict] | None:
    try:
        rows = _rows(
            db_path,
            "SELECT id, event_id, action, detail, status, created_at FROM approvals "
            "WHERE status='waiting' AND executed_at IS NULL ORDER BY id DESC",
        )
    except Exception:
        return None
    for row in rows:
        try:
            row["detail"] = json.loads(row["detail"] or "{}")
        except (TypeError, ValueError):
            row["detail"] = {}
    return rows


def list_deliveries(db_path, limit: int = LIMIT_DEFAULT) -> list[dict] | None:
    try:
        rows = _rows(
            db_path,
            "SELECT id, event_id, channel, status, attempts, body, created_at, sent_at "
            "FROM deliveries ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    except Exception:
        return None
    for row in rows:
        row["body"] = (row.get("body") or "")[:BODY_SUMMARY_CHARS]
    return rows


def health(db_path) -> dict:
    """Health numbers, computed from the tables (no cached counters).

    duplicate_rate = events with >1 status='sent' delivery ÷ events that were
    notified at all. attempts > 1 is crash-recovery retry — the normal case —
    and is deliberately not the numerator. stale_requeued is None when
    meta['stats:stale_requeued'] has no writer yet (issue #20), so the page can
    say 「未接入」 instead of showing a fake 0.
    """
    try:
        conn = _ro(db_path)
    except Exception:
        return {}
    try:
        today = db.now()[:10]
        row = conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM events) AS events_total, "
            "(SELECT COUNT(*) FROM events WHERE status='failed') AS events_failed, "
            "(SELECT COUNT(*) FROM (SELECT event_id FROM deliveries "
            "  WHERE status='sent' AND event_id IS NOT NULL "
            "  GROUP BY event_id HAVING COUNT(*) > 1)) AS duplicate_events, "
            "(SELECT COUNT(DISTINCT event_id) FROM deliveries "
            "  WHERE status='sent' AND event_id IS NOT NULL) AS events_with_sent_delivery, "
            "(SELECT COUNT(*) FROM deliveries WHERE created_at >= ?) AS deliveries_today",
            (today,),
        ).fetchone()
        stale_raw = db.get_meta(conn, STALE_KEY)
    except Exception:
        return {}
    finally:
        conn.close()
    sent = int(row["events_with_sent_delivery"] or 0)
    dup = int(row["duplicate_events"] or 0)
    try:
        stale = int(stale_raw) if stale_raw is not None else None
    except (TypeError, ValueError):
        stale = None
    return {
        "events_total": int(row["events_total"] or 0),
        "events_failed": int(row["events_failed"] or 0),
        "duplicate_events": dup,
        "events_with_sent_delivery": sent,
        "duplicate_rate": round(dup / sent, 4) if sent else 0,
        "stale_requeued": stale,
        "deliveries_today": int(row["deliveries_today"] or 0),
    }


# ---------------------------------------------------------------- 两个 POST

def decide(db_path, approval_id: int, approved: bool) -> dict:
    """Flip one waiting approval. Nothing is executed here, by design.

    Own connection instead of db.connect(): that one mkdirs, runs the schema
    script and flips journal_mode=WAL, so approving against an empty file would
    fabricate four tables. This touches exactly one row and nothing else.
    """
    path = Path(db_path)
    if not path.exists():
        return {"updated": 0, "id": approval_id}
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        db.decide_approval(conn, approval_id, approved)  # raises when not waiting
        return {"updated": 1, "id": approval_id}
    except Exception:
        return {"updated": 0, "id": approval_id}
    finally:
        conn.close()


# ------------------------------------------------------------------- HTTP

class PanelServer(ThreadingHTTPServer):
    """ThreadingHTTPServer plus the two facts a handler reads per request."""

    daemon_threads = True
    db_path: Path
    quiet: bool = False


class Handler(BaseHTTPRequestHandler):
    server_version = "wick-panel"
    protocol_version = "HTTP/1.1"

    def _panel(self) -> PanelServer:
        return cast(PanelServer, self.server)

    def log_message(self, format, *args):  # noqa: A002 — the base class's parameter name
        if not self._panel().quiet:
            sys.stderr.write("  " + (format % args) + "\n")

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", status)

    def _drain_body(self) -> None:
        """Read and discard the body: HTTP/1.1 keep-alive desyncs on leftover bytes."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length > 0:
            self.rfile.read(length)

    def _host_ok(self) -> bool:
        """Host must be exactly what we bound — a rebinding name cannot read or write."""
        host, port = self._panel().server_address[:2]
        return self.headers.get("Host") == f"{host}:{port}"

    def _post_refusal(self) -> str | None:
        """Why this POST is not from the panel page itself, or None when it is.

        Three independent layers, because the approval POST is the system's only
        human gate (worker commits/pushes the next tick) and browsers differ:
        - X-Wick-Panel is not a CORS-simple header, so a cross-site page must
          send a preflight first, and this server never answers OPTIONS;
        - Sec-Fetch-Site is the browser's own answer to "where did this come
          from" — absent means it is not a browser we trust (curl, an old
          browser), so it is refused too;
        - Host stops DNS rebinding: a name that resolves to 127.0.0.1 still
          carries its own Host.
        """
        if not self._host_ok():
            return "bad Host"
        if self.headers.get("X-Wick-Panel") != "1":
            return "missing X-Wick-Panel"
        if self.headers.get("Sec-Fetch-Site") not in ("same-origin", "none"):
            return "cross-site POST refused"
        return None

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's spelling
        if not self._host_ok():
            self._send_json({"error": "bad Host"}, 403)
            return
        url = urlparse(self.path)
        if url.path in ("/", "/index.html", "/panel.html"):
            try:
                self._send(PANEL.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._send(b"panel.html missing", "text/plain; charset=utf-8", 500)
            return
        db_path = self._panel().db_path
        limit = _limit(parse_qs(url.query))
        route = {
            "/events": lambda: list_events(db_path, limit),
            "/approvals": lambda: list_approvals(db_path),
            "/deliveries": lambda: list_deliveries(db_path, limit),
            "/stats": lambda: health(db_path),
        }.get(url.path)
        if route is None:
            self._send_json({"error": "not found"}, 404)
            return
        self._send_json(route())

    def do_POST(self):  # noqa: N802
        self._drain_body()  # before any early return, or the next request on this socket misparses
        refusal = self._post_refusal()
        if refusal is not None:
            self._send_json({"error": refusal, "updated": 0}, 403)
            return
        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "approvals" and parts[2] in ("approve", "reject"):
            try:
                approval_id = int(parts[1])
            except ValueError:
                self._send_json({"updated": 0, "error": "bad id"}, 400)
                return
            self._send_json(decide(self._panel().db_path, approval_id, parts[2] == "approve"))
            return
        self._send_json({"error": "not found"}, 404)


def make_server(port: int, db_path=None, quiet: bool = False) -> PanelServer:
    """Bind the panel on 127.0.0.1. port=0 lets the OS pick one (selftest)."""
    server = PanelServer(("127.0.0.1", port), Handler)
    server.db_path = Path(db_path or db.DEFAULT_DB)
    server.quiet = quiet
    return server


def serve(port: int = DEFAULT_PORT, db_path=None) -> int:
    server = make_server(port, db_path)
    host, bound = server.server_address[:2]
    print(f"wick 面板: http://{host}:{bound}   库: {server.db_path}", flush=True)
    print("Ctrl+C 退出", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


# ---------------------------------------------------------------- selftest

def _selftest() -> int:
    """Start the server on a temp db and port 0, hit it for real, shut it down."""
    import http.client
    import tempfile
    import threading
    import urllib.error
    import urllib.request

    def get(base: str, path: str) -> Any:
        with urllib.request.urlopen(base + path, timeout=10) as resp:
            assert resp.status == 200, (path, resp.status)
            ctype = resp.headers["Content-Type"]
            raw = resp.read().decode("utf-8")
        return raw if "text/html" in ctype else json.loads(raw)

    # The panel page's own request shape: the non-simple header plus the browser's
    # same-origin marker. Anything less is refused (see Handler._post_refusal).
    PANEL_HEADERS = {"X-Wick-Panel": "1", "Sec-Fetch-Site": "same-origin"}

    def raw(port: int, path: str, method: str = "POST", headers=None, body: bytes = b""):
        """One request with full header control; returns (status, json|None)."""
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            text = resp.read().decode("utf-8")
            return resp.status, (json.loads(text) if text else None)
        finally:
            conn.close()

    def post(base: str, path: str):
        status, payload = raw(int(base.rsplit(":", 1)[1]), path, headers=PANEL_HEADERS)
        assert status == 200, (path, status, payload)
        return payload

    def approval_state(db_file: Path, approval_id: int) -> tuple:
        conn = sqlite3.connect(str(db_file))
        try:
            row = conn.execute("SELECT status, executed_at FROM approvals WHERE id=?",
                               (approval_id,)).fetchone()
        finally:
            conn.close()
        return row

    # ignore_cleanup_errors: Windows may still hold the sqlite WAL/SHM briefly.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        path = Path(td) / "wick.db"
        conn = db.connect(path)
        ev_ok, _ = db.enqueue(conn, "github_issue", "cmyyy/wick#5", {"title": "面板"})
        ev_dup, _ = db.enqueue(conn, "github_issue", "cmyyy/wick#6", {"title": "重复通知"})
        db.add_delivery(conn, ev_ok, "一件事，通知一次")
        conn.execute("UPDATE deliveries SET status='sent' WHERE event_id=?", (ev_ok,))
        db.add_delivery(conn, ev_dup, "第一次")
        db.add_delivery(conn, ev_dup, "第二次")
        conn.execute("UPDATE deliveries SET status='sent' WHERE event_id=?", (ev_dup,))
        conn.commit()
        # request_approval fences on a running event, so claim it first (as cli.py does).
        claimed = db.claim_next(conn, "panel-selftest")
        assert claimed is not None and claimed["id"] == ev_ok
        aid = db.request_approval(
            conn, ev_ok, "pr_open",
            {"repo": "cmyyy/wick", "branch": "feat/5-panel", "title": "面板"},
            worker_id="panel-selftest", attempts=claimed["attempts"],
        )
        assert aid is not None
        fu_id, fu_created = db.schedule_followup(conn, "2099-01-01T09:00:00+00:00", "复查面板")
        assert fu_id is not None and fu_created, (fu_id, fu_created)
        conn.close()

        server = make_server(0, path, quiet=True)
        port = server.server_address[1]
        assert port != 0, "端口 0 应该由 OS 分配出真端口"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{port}"
        try:
            # 五个 GET
            html = get(base, "/")
            for block in ("事件流", "待批准", "投递记录", "健康度"):
                assert block in html, f"页面缺「{block}」块"
            # 空态判字段、假死回收说「未接入」——两次踩过的坑就守在这两行文本上。
            assert "events_total == null" in html, "空态要判字段，不能判 {} 真假"
            assert "未接入" in html, "假死回收没有写者，要显示「未接入」"
            # 批准按钮必须带那个非简单头，否则会被自己的 403 挡掉
            assert '"X-Wick-Panel": "1"' in html, "页面的批准请求要带 X-Wick-Panel"

            events = get(base, "/events")
            assert [e["source_type"] for e in events] == ["followup", "github_issue", "github_issue"], events
            fu = events[0]
            assert fu["payload"]["title"] == "复查面板", fu          # 定时器事件要有自己的标题
            assert fu["due_at"] == "2099-01-01T09:00:00+00:00", fu    # 到期时间要能渲染
            assert events[1]["source_id"] == "cmyyy/wick#6", events[1]
            assert events[1]["status"] == "pending" and events[1]["attempts"] == 0, events[1]
            assert len(get(base, "/events?limit=1")) == 1, "limit 参数没生效"

            approvals = get(base, "/approvals")
            assert len(approvals) == 1 and approvals[0]["id"] == aid, approvals
            assert approvals[0]["detail"]["repo"] == "cmyyy/wick", approvals[0]

            deliveries = get(base, "/deliveries")
            assert len(deliveries) == 3 and deliveries[0]["body"] == "第二次", deliveries
            assert {d["status"] for d in deliveries} == {"sent"}, deliveries

            stats = get(base, "/stats")
            assert stats["events_total"] == 3 and stats["events_failed"] == 0, stats
            assert stats["duplicate_events"] == 1, stats            # 只有 ev_dup
            assert stats["events_with_sent_delivery"] == 2, stats
            assert stats["duplicate_rate"] == 0.5, stats            # 1/2，不是 NaN
            assert stats["deliveries_today"] == 3, stats
            assert stats["stale_requeued"] is None, stats           # 没写者 = 未接入，不是 0

            # 写者上线后（issue #20）就显示真数字
            conn = db.connect(path)
            db.set_meta(conn, STALE_KEY, "3")
            conn.close()
            assert get(base, "/stats")["stale_requeued"] == 3, "meta 有值时应显示数字"

            # ---- 批准闸门：跨站来源必须被拒，且库里那行不能动 ----
            # reviewer 的原样复现：带恶意 Origin + cross-site 的 POST
            hostile = {"Origin": "http://evil.example",
                       "Sec-Fetch-Site": "cross-site",
                       "X-Wick-Panel": "1"}
            status, payload = raw(port, f"/approvals/{aid}/approve", headers=hostile, body=b"")
            assert status == 403, (status, payload)
            assert approval_state(path, aid)[0] == "waiting", "跨站 POST 把审批改了"
            # 每一层单独缺失都要被拒
            for label, headers in (
                ("没有 X-Wick-Panel（简单请求）", {"Sec-Fetch-Site": "same-origin"}),
                ("没有 Sec-Fetch-Site（curl 会缺）", {"X-Wick-Panel": "1"}),
                ("Sec-Fetch-Site: same-site", {"X-Wick-Panel": "1", "Sec-Fetch-Site": "same-site"}),
                ("Host 是 rebinding 域名", dict(PANEL_HEADERS, Host="wick.evil.example")),
            ):
                status, payload = raw(port, f"/approvals/{aid}/approve", headers=headers, body=b"")
                assert status == 403, (label, status, payload)
                assert approval_state(path, aid)[0] == "waiting", label
            # 读也挡 rebinding：GET 带陌生 Host 直接 403
            status, payload = raw(port, "/events", method="GET", headers={"Host": "wick.evil.example"})
            assert status == 403, (status, payload)
            # 带 body 的 POST 之后，同一条 keep-alive 连接上的下一个请求要能正常解析
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                conn.request("POST", f"/approvals/{aid}/approve", body=b'{"pad":"' + b"x" * 200 + b'"}',
                             headers=dict(PANEL_HEADERS, **{"Content-Type": "application/json"}))
                first = conn.getresponse()
                first.read()
                conn.request("GET", "/stats")
                second = conn.getresponse()
                kept = json.loads(second.read().decode("utf-8"))
            finally:
                conn.close()
            assert first.status == 200 and second.status == 200, (first.status, second.status)
            assert kept["events_total"] == 3, kept
            # 上一步已经把 aid 批准了（面板自己的合法请求）
            assert approval_state(path, aid)[0] == "approved"
            assert approval_state(path, aid)[1] is None, "面板不该执行任何动作"

            # 拒绝
            conn = db.connect(path)
            new_id = db.request_approval(conn, None, "push", {"repo": "cmyyy/wick"})
            conn.close()
            assert new_id is not None
            assert post(base, f"/approvals/{new_id}/reject")["updated"] == 1
            conn = sqlite3.connect(path)
            status_rej = conn.execute("SELECT status FROM approvals WHERE id=?", (new_id,)).fetchone()[0]
            conn.close()
            assert status_rej == "rejected", status_rej
            assert post(base, "/approvals/999/reject")["updated"] == 0, "不存在的 id 不该更新"

            # 没有重复处理时：0，不是 NaN，也不是抛异常
            conn = db.connect(path)
            conn.execute("DELETE FROM deliveries WHERE event_id=?", (ev_dup,))
            conn.commit()
            conn.close()
            clean = get(base, "/stats")
            assert clean["duplicate_rate"] == 0 and clean["events_with_sent_delivery"] == 1, clean
            assert "NaN" not in json.dumps(clean), "NaN 不是合法 JSON，浏览器会拒"
            # 一条 sent 投递都没有的分母为 0：还是 0，不是 NaN
            conn = db.connect(path)
            conn.execute("DELETE FROM deliveries")
            conn.commit()
            conn.close()
            none_sent = get(base, "/stats")
            assert none_sent["events_with_sent_delivery"] == 0, none_sent
            assert none_sent["duplicate_rate"] == 0, none_sent
            assert "NaN" not in json.dumps(none_sent), none_sent

            # 库不存在 / 表缺失：三块列表是 null（读不到），不是 []（队列确实是空的）
            server.db_path = Path(td) / "absent.db"
            assert get(base, "/events") is None, "缺库应是 null，不是 []"
            assert get(base, "/approvals") is None and get(base, "/deliveries") is None, "缺库应是 null"
            assert get(base, "/stats") == {}, "缺库应返回空对象"
            assert not server.db_path.exists(), "读接口不该创建数据库"

            bare = Path(td) / "bare.db"
            sqlite3.connect(str(bare)).close()
            server.db_path = bare
            assert get(base, "/events") is None and get(base, "/deliveries") is None, "缺表应是 null"
            assert get(base, "/stats") == {}, "缺表应返回空结构"
            # 空文件上批准：只跑那一条 UPDATE，不许建表、不许开 WAL
            assert post(base, "/approvals/1/approve")["updated"] == 0, "缺表时批准不该崩"
            conn = sqlite3.connect(str(bare))
            tables = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
            conn.close()
            assert tables == 0, f"批准不该给空文件建表（现在 {tables} 张）"
            assert journal != "wal", f"批准不该把空文件切成 WAL（现在 {journal}）"
            assert not (bare.parent / (bare.name + "-wal")).exists(), "批准不该留下 -wal 文件"

            # 反过来：库读得到但没有行 → []，不是 null
            empty = Path(td) / "empty.db"
            db.connect(empty).close()
            server.db_path = empty
            assert get(base, "/events") == [] and get(base, "/deliveries") == [], "空库应是 [] 不是 null"
            assert get(base, "/approvals") == [], "没有待批准应是 [] 不是 null"

            # 未知路径
            try:
                get(base, "/nope")
                raise AssertionError("未知路径应该 404")
            except urllib.error.HTTPError as exc:
                assert exc.code == 404, exc.code
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)
            assert not thread.is_alive(), "服务没关掉"

    print("server.py selftest OK: 五个 GET 真库读数 / 跨站 POST 403 且库不变 / 批准只改 status 且 "
          "executed_at 仍 NULL / 空文件批准不建表不开 WAL / 带 body 的 POST 不打乱 keep-alive / "
          "重复处理率 0 不是 NaN / 假死回收未接入 / 缺库缺表返 null、空库返 [] / 定时器事件带标题与 due_at / 关得掉")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="wick 本地面板")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"监听端口（默认 {DEFAULT_PORT}；0 = 让 OS 分配）")
    ap.add_argument("--db", default=None, help="数据库路径（默认 WICK_DB 或 ~/.wick/wick.db）")
    ap.add_argument("--selftest", action="store_true", help="自检")
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    return serve(args.port, args.db)


if __name__ == "__main__":
    raise SystemExit(main())
