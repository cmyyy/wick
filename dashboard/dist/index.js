/**
 * Wick — Hermes dashboard plugin.
 *
 * Four panels over the wick database, polled every 5s: event stream, pending
 * approvals, delivery log, health. Backend: dashboard/plugin_api.py, mounted
 * at /api/plugins/wick/.
 *
 * Plain IIFE, no build step, no imports (host injects React + UI primitives
 * through window.__HERMES_PLUGIN_SDK__). The approval buttons only move a row
 * out of `waiting`; worker.process_approvals is what actually runs one, so
 * nothing here touches git.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  const h = SDK.React.createElement;
  const { useState, useEffect } = SDK.hooks;
  const { Card, CardHeader, CardTitle, CardContent, Badge, Button } = SDK.components;

  const API = "/api/plugins/wick";
  const POLL_MS = 5000;

  const table = { width: "100%", borderCollapse: "collapse", fontSize: 12 };
  const th = { textAlign: "left", padding: "4px 8px", opacity: 0.65, fontWeight: 500 };
  const td = { padding: "4px 8px", borderTop: "1px solid rgba(128,128,128,0.2)", verticalAlign: "top" };

  function at(ts) {
    if (!ts) return "—";
    return String(ts).replace("T", " ").slice(0, 19);
  }

  function short(value, max) {
    const text = typeof value === "string" ? value : JSON.stringify(value);
    if (!text) return "—";
    return text.length > max ? text.slice(0, max) + "…" : text;
  }

  function Stat(props) {
    const { label, value, hint } = props;
    return h("div", { style: { minWidth: 120 } },
      h("div", { style: { fontSize: 11, opacity: 0.6 } }, label),
      h("div", { style: { fontSize: 20, fontWeight: 600 } }, value),
      hint ? h("div", { style: { fontSize: 11, opacity: 0.5 } }, hint) : null,
    );
  }

  function Section(props) {
    return h(Card, null,
      h(CardHeader, null, h(CardTitle, null, props.title, props.count != null
        ? h("span", { style: { fontSize: 12, opacity: 0.5, marginLeft: 6 } }, "(" + props.count + ")")
        : null)),
      h(CardContent, null, props.children),
    );
  }

  function Empty(props) {
    return h("div", { style: { fontSize: 12, opacity: 0.5, padding: "8px 0" } }, props.text);
  }

  function HealthPanel(props) {
    const s = props.stats;
    // 后端读不到库时返回 {}（truthy！），所以判定看字段而不是看对象本身，
    // 否则空态会一路算到 duplicate_rate == null，显示成 NaN%。
    if (!s || s.duplicate_rate == null) return h(Empty, { text: "无数据（数据库读不到）" });
    const rate = s.duplicate_rate;
    return h("div", { style: { display: "flex", flexWrap: "wrap", gap: 24 } },
      h(Stat, { label: "事件总数", value: s.events_total, hint: "失败 " + s.events_failed }),
      h(Stat, {
        label: "重复处理率",
        value: (rate * 100).toFixed(rate === 0 ? 0 : 1) + "%",
        hint: s.duplicate_events + " / " + s.events_with_sent_delivery + " 个已通知事件",
      }),
      // 假死回收：统计的写者还没落库（issue #20），null = 未接入，不显示假 0。
      h(Stat, {
        label: "假死回收",
        value: s.stale_requeued == null ? "未接入" : s.stale_requeued,
        hint: "stats:stale_requeued",
      }),
      h(Stat, { label: "今日投递", value: s.deliveries_today, hint: "UTC 当日" }),
    );
  }

  function EventsPanel(props) {
    if (!props.events.length) return h(Empty, { text: "暂无事件" });
    return h("table", { style: table },
      h("thead", null, h("tr", null,
        ["ID", "来源", "状态", "尝试", "创建时间"].map(function (t) {
          return h("th", { key: t, style: th }, t);
        }))),
      h("tbody", null, props.events.map(function (e) {
        return h("tr", { key: e.id },
          h("td", { style: td }, e.id),
          h("td", { style: td }, e.source_type + " · " + short(e.source_id, 40)),
          h("td", { style: td }, h(Badge, null, e.status)),
          h("td", { style: td }, e.attempts),
          h("td", { style: td }, at(e.created_at)),
        );
      })),
    );
  }

  function ApprovalsPanel(props) {
    if (!props.approvals.length) return h(Empty, { text: "没有待批准的请求" });
    return h("table", { style: table },
      h("thead", null, h("tr", null,
        ["动作", "目标 / 详情", "创建时间", ""].map(function (t) {
          return h("th", { key: t, style: th }, t);
        }))),
      h("tbody", null, props.approvals.map(function (a) {
        const busy = props.busy === a.id;
        return h("tr", { key: a.id },
          h("td", { style: td }, h(Badge, null, a.action)),
          h("td", { style: td }, short(a.detail, 90)),
          h("td", { style: td }, at(a.created_at)),
          h("td", { style: td },
            h("div", { style: { display: "flex", gap: 6 } },
              h(Button, { size: "sm", disabled: busy, onClick: function () { props.decide(a.id, true); } },
                busy ? "…" : "批准"),
              h(Button, { size: "sm", disabled: busy, onClick: function () { props.decide(a.id, false); } },
                "拒绝"),
            )),
        );
      })),
    );
  }

  function DeliveriesPanel(props) {
    if (!props.deliveries.length) return h(Empty, { text: "暂无投递" });
    return h("table", { style: table },
      h("thead", null, h("tr", null,
        ["渠道", "状态", "尝试", "创建时间", "内容"].map(function (t) {
          return h("th", { key: t, style: th }, t);
        }))),
      h("tbody", null, props.deliveries.map(function (d) {
        return h("tr", { key: d.id },
          h("td", { style: td }, d.channel),
          h("td", { style: td }, h(Badge, null, d.status)),
          h("td", { style: td }, d.attempts),
          h("td", { style: td }, at(d.created_at)),
          h("td", { style: td }, short(d.body, 80)),
        );
      })),
    );
  }

  function WickPage() {
    const [data, setData] = useState({ events: [], approvals: [], deliveries: [], stats: null });
    const [busy, setBusy] = useState(null);
    const [note, setNote] = useState("");

    useEffect(function () {
      let alive = true;
      function load() {
        if (document.hidden) return; // 后台标签页不轮询
        Promise.all(
          ["/events", "/approvals", "/deliveries", "/stats"].map(function (path) {
            return SDK.fetchJSON(API + path).catch(function () { return null; });
          })
        ).then(function (res) {
          if (!alive) return;
          setData({
            events: res[0] || [],
            approvals: res[1] || [],
            deliveries: res[2] || [],
            stats: res[3] || null,
          });
        });
      }
      load();
      const timer = setInterval(load, POLL_MS);
      return function () {
        alive = false;
        clearInterval(timer);
      };
    }, []);

    function decide(id, approve) {
      setBusy(id);
      setNote("");
      SDK.authedFetch(API + "/approvals/" + id + (approve ? "/approve" : "/reject"), { method: "POST" })
        .then(function (res) { return res.ok ? res.json() : { updated: 0 }; })
        .then(function (out) {
          // 只改了状态：真的执行要等 worker 下一个循环
          if (!out.updated) setNote("该审批已不是 waiting，刷新后不可再操作");
          return SDK.fetchJSON(API + "/approvals");
        })
        .then(function (fresh) {
          setData(function (prev) {
            return Object.assign({}, prev, { approvals: fresh || [] });
          });
        })
        .catch(function (err) { setNote("操作失败：" + err.message); })
        .then(function () { setBusy(null); });
    }

    return h("div", { style: { display: "flex", flexDirection: "column", gap: 16, padding: 16 } },
      h("div", { style: { fontSize: 18, fontWeight: 600 } }, "Wick"),
      note ? h("div", { style: { fontSize: 12, opacity: 0.7 } }, note) : null,
      h(Section, { title: "健康度" }, h(HealthPanel, { stats: data.stats })),
      h(Section, { title: "待批准", count: data.approvals.length },
        h(ApprovalsPanel, { approvals: data.approvals, busy: busy, decide: decide })),
      h(Section, { title: "事件流", count: data.events.length }, h(EventsPanel, { events: data.events })),
      h(Section, { title: "投递记录", count: data.deliveries.length },
        h(DeliveriesPanel, { deliveries: data.deliveries })),
    );
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("wick", WickPage);
  }
})();
