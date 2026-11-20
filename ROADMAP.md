# 排期：10 个工作日

原则：**每天结束时有一个能演示的东西**，哪怕很丑。不做"三天憋个大招"。

| 天 | 目标 | 产出 | 当日验收（跑得过才算完） |
|---|---|---|---|
| **D1** | 数据层 + worker 骨架 | `src/db.py`（三表 schema、`sha256(source_type+source_id)` 唯一索引、状态流转函数）<br>`src/worker.py`（轮询取 pending → 占位 → 处理 → 写回） | 手动 `sqlite3` 插一条事件 → worker 起来 → 状态变 `done` → `deliveries` 里有一条记录。**同一条插两次，只有一条被处理** |
| **D2** | GitHub 事件源 | `src/sources/github.py`：`GET /repos/{owner}/{repo}/issues?since=<last_run>&state=all` 增量轮询，5 分钟一次 | 跑一次能拉到自己仓库的真实 issue 写进 `events`；**连跑两次不产生重复**（`issue.id` 去重生效） |
| **D3** | 接通一次 run | `src/prompts/issue.md` + worker 通过 gateway RPC `prompt.submit` 起会话 | 一条 issue 事件真的起了一个 Hermes 会话，会话里能看到 issue 标题和正文；run 结束能拿到它的输出 |
| **D4** | issue 处理能力 | `src/prompts/issue.md` 完善：读代码 → 定位 → 改 → 跑测试 → 输出 diff 摘要 | 给一条真 issue，产出**可读的 diff + 测试结果**（用 `eval/run.py` 评分：回归失败必须为 0） |
| **D5** | 权限边界（deferred） | `approvals` 表接入：不可逆动作前写 waiting → run 结束 → CLI 里批准 → 起新 run | 提 PR 前**真的停下来**，`approvals` 有 waiting 记录；批准后新 run 完成提 PR |
| **D6** | 投递队列 | 节流（日上限）、失败重投（指数退避）、`sent / throttled / failed` 状态机 | 超过日上限的消息压到次日；人为制造一次投递失败，能自动重试成功 |
| **D7** | 跨 run 记忆 | `progress/<task_id>.md` + 索引加载；每个 run 开头注入 | kill 掉 run 再重跑，能读到上次进度并**接着干**，不重头来 |
| **D8** | 桌面面板 | `desktop/plugin.js`（事件流 / 待批准 / 投递记录 / 健康度）+ `plugin_api.py`（`/api/plugins/wick/*`） | 面板能加载出来，四块数据都是真的；点"批准"能真的触发执行 |
| **D9** | 可靠性验收 | 崩溃恢复测试脚本 + 三条硬指标跑一遍 | ① kill worker 后同一条事件重跑、**不重复投递**；② 重复处理率 0；③ 闭环成功率 ≥90%（攒够 10 条 issue） |
| **D10** | 收尾 | README 补快速开始、录 3 分钟 demo、整理评测报告 | 别人按 README 能装上并跑通一条事件 |

---

## Issue 映射（D6 起，每个阶段一条 ticket）

| 阶段 | Ticket | 主题 |
|---|---|---|
| D6 | #3 | outbox：投递、节流、失败重投 |
| D7 | #4 | 跨 run 记忆 |
| D8 | #5 | 桌面端面板 |
| D9 | #6 | 可靠性验收 + 三条硬指标 |
| D10 | #7 | 文档与三分钟 demo |

D1-D5 直接提交到 main（流程见 CONTRIBUTING.md，从 D6 起改正）。

---

## 里程碑

| 里程碑 | 天数 | 判定 |
|---|---|---|
| **M1 闭环打通** | D1-D3 | 事件进得来、run 起得来、结果出得去 |
| **M2 真能干活** | D4-D5 | 能改代码、能在危险动作前停下来 |
| **M3 无人值守** | D6-D7 | 不丢、不刷屏、崩了能续 |
| **M4 可演示** | D8-D10 | 有面板、有数字、有 demo |

---

## 期间要攒的东西（D1 就开始）

**开发中遇到的每个 bug / TODO，随手在自己仓库开一条 issue。** 这是 D9 演示的素材，也是第一条 dogfooding 记录。目标：D9 时仓库里有 **≥10 条真实 issue**。

---

## 明确不做（进 MVP 的门槛）

只有当它**能修一个已经发生过的故障**，或**能补一个已经量化过的缺口**，才允许加进 MVP。

| 不做 | 什么时候再说 |
|---|---|
| 薄 Router（三分类派活） | 真有 ≥3 个并行任务压着 |
| Hermes 上游 issue | MVP 完成后 |
| 语义去重 | 出现"同一件事换说法"被漏掉 |
| RSS 信息摄取（副场景） | 主场景稳定后 |
| 多租户 / 组织门禁 | 不做 |
| 记忆 dreaming | 不做 |
| 独立 Web UI | 不做（用桌面插件） |
