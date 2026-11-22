/**
 * Render check for dashboard/dist/index.js — no framework, no jsdom, no build.
 *
 * Loads the real IIFE in a vm sandbox with a stubbed plugin SDK (React is a
 * createElement that returns plain objects, hooks return a canned state), then
 * flattens the tree to text and asserts what the 健康度 panel says.
 *
 * Usage: node dashboard/tests/render_check.js [bundle.js]
 *   (the optional arg points the same checks at another bundle, e.g. a copy
 *    with a fix reverted, to prove the check goes red)
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const target = process.argv[2] || path.join(__dirname, "..", "dist", "index.js");
const code = fs.readFileSync(target, "utf8");

function renderText(stats) {
  const h = function (type, props) {
    const children = Array.prototype.slice.call(arguments, 2);
    return { type: type, props: Object.assign({}, props, { children: children }) };
  };
  // WickPage's three useState slots: data, busy, note.
  const queue = [{ events: [], approvals: [], deliveries: [], stats: stats }, null, ""];
  const sdk = {
    React: { createElement: h },
    hooks: {
      useState: function () { return [queue.shift(), function () {}]; },
      useEffect: function () {},
    },
    components: {
      Card: "Card", CardHeader: "CardHeader", CardTitle: "CardTitle",
      CardContent: "CardContent", Badge: "Badge", Button: "Button",
    },
    fetchJSON: function () { return Promise.resolve({}); },
    authedFetch: function () { return Promise.resolve({}); },
    utils: {},
  };

  let captured = null;
  const sandbox = {
    window: {
      __HERMES_PLUGIN_SDK__: sdk,
      __HERMES_PLUGINS__: { register: function (_name, component) { captured = component; } },
    },
    document: { hidden: true },
    console: console,
    setInterval: setInterval,
    clearInterval: clearInterval,
    Promise: Promise,
  };
  vm.runInNewContext(code, sandbox);
  if (!captured) throw new Error("bundle 没有调用 __HERMES_PLUGINS__.register()");

  const out = [];
  (function walk(node) {
    if (node === null || node === undefined || node === false) return;
    if (Array.isArray(node)) { node.forEach(walk); return; }
    if (typeof node === "string" || typeof node === "number") { out.push(String(node)); return; }
    if (typeof node.type === "function") { walk(node.type(node.props || {})); return; }
    walk(node.props && node.props.children);
  })(captured({}));
  return out.join(" ");
}

function check(label, stats, want, banned) {
  const text = renderText(stats);
  want.forEach(function (needle) {
    if (text.indexOf(needle) === -1) {
      throw new Error(label + "：缺「" + needle + "」\n  实际渲染：" + text.slice(0, 400));
    }
  });
  banned.forEach(function (needle) {
    if (text.indexOf(needle) !== -1) {
      throw new Error(label + "：不该出现「" + needle + "」\n  实际渲染：" + text.slice(0, 400));
    }
  });
  console.log("ok - " + label);
}

const filled = {
  events_total: 3,
  events_failed: 1,
  duplicate_events: 0,
  events_with_sent_delivery: 3,
  duplicate_rate: 0,
  stale_requeued: null,
  deliveries_today: 2,
};

// 1) 后端读不到库时返回 {}：truthy 的空态，必须落成「无数据」，不能算成 NaN%。
check("空态 {} 显示无数据", {}, ["无数据"], ["NaN", "未接入"]);
// 2) 初始 state 是 null：同样无数据。
check("初始 null 显示无数据", null, ["无数据"], ["NaN"]);
// 3) 真数据 + 假死回收没有写者：这一格是「未接入」，重复处理率 0 显示 0%。
check("假死回收 null 显示未接入", filled, ["未接入", "0%"], ["NaN", "undefined"]);
// 4) 写者上线后（issue #20）应显示真数字。
check("假死回收有值显示数字", Object.assign({}, filled, { stale_requeued: 3 }), ["3"], ["未接入", "NaN"]);

console.log("render_check OK: " + path.relative(process.cwd(), target));
