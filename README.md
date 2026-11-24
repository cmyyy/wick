# wick

> 烛芯。火从这里开始，一直烧到你回来。

一个**常驻的个人 Agent**：你不在时它自己醒来、自己干活、自己把结果放回你手上；遇到不可逆的动作，它停下来等你点头。

跑在 [Hermes](https://github.com/NousResearch/hermes-agent) 之上，**一行 Hermes 源码都不改**。

---

## 为什么要做这个

GitHub 上已经有 5 个同赛道项目（都叫 nightshift），它们解决的是"让 agent 多干活"：

| 它们 | wick |
|---|---|
| CLI 脚本直调 agent（`nightshift --recipe`） | **事件队列**：去重、状态机、崩溃后重跑 |
| 跑一晚的一次性长任务 | **持续常驻**：事件驱动，随时可进入 |
| 结果直接落到分支 / PR | **投递队列**：节流、失败重投、防刷屏 |
| 没有人工介入点 | **待批准面板**：不可逆动作停下来等你点 |

一句话：它们让 agent 干得更多，wick 让 agent **在你不管的时候不出错**。

---

## 边界：wick 是产品，不是在开发自己

容易混淆的一点，先说清楚：

| | 是什么 |
|---|---|
| **wick（这个仓库）** | 一个常驻 agent：事件进 `events` 表，worker 消费，结果进 `deliveries`，不可逆动作停下等人批准 |
| **wick 的开发过程** | 人工编排 + 三个 bot 分工：orchestrator（拆任务）、implementer（写代码）、reviewer（审 PR），人在环上拍板 |

**这两件事现在还没接上。** 开发 wick 时派活走固定的 `Bot Chat` 会话（Hermes 原生 bot mode），结果从该会话取，不经过本仓库的 `events` / `deliveries`。

所以下面这些说法是**错的**，不要用：

- ❌ "wick 在自己开发自己"
- ❌ "我们用 wick 的方式开发 wick"

正确的是：

- ✅ "wick 是产品形态；当前开发流程是人工编排 + bot 分工"

**术语警告**：`worker` 这个词在两个语境里指不同东西，读文档时别串：

| 写在哪 | 指什么 | 特征 |
|---|---|---|
| `src/worker.py`、架构图、可靠性验收 | **产品的 worker** | 常驻进程，消费 `events` 表，有租约/心跳/attempt fence |
| 「派活」「implementer」 | **开发时的 bot** | `developer`，人是它的调度器，经由固定 `Bot Chat` 会话 |

两者共用的只有"处理一件事"这个语义。产品的 worker 是代码，开发的 bot 是人手里的工具，**现在还没有任何一行代码把它们连起来**——那正是下面说的 D9 之后要做的事。

**什么时候接上**：D9 可靠性验收通过之后（重复投递 0、崩溃恢复可用），再把派活改成写 `events` 表、结果进 `deliveries`。在那之前拿半成品当开发工具，等于在沼泽地上盖房子。

详见 `DECISIONS.md` 决策 13。

---

## 五个原语

常驻 agent 不是"一直在跑"，而是**大部分时间在睡，周期性醒来干一段活**（一次 run），干完存状态继续睡。

| 原语 | 作用 | wick 的实现 |
|---|---|---|
| **事件收件箱** | 所有触发统一进入口 | `events` 表 + 唯一索引去重 + 单 worker 串行消费 |
| **定时器** | 到点叫醒 | `events.due_at`（ISO 时间）；`claim_next` 只领 `due_at IS NULL OR due_at <= now` 的行。agent 用「下次跟进」槽自排，人用 `cli.py schedule`。**只产生事件，不直接执行** |
| **记忆** | 跨 run 的连续性 | `~/.wick/progress/<task>.md` + `index.md`（**在仓库之外**，见决策 14）；注入最近几轮 + 一行索引（只列在途任务）；恢复点是上一轮**已完成**的记录 |
| **权限** | 无人值守的风险边界 | `approvals` 表：不可逆动作 → run 结束 → 等你批准 → 起新 run |
| **投递队列** | 结果怎么到你手上 | `deliveries` 表 + 节流 + 失败重投 |

---

## 架构

```
                   ┌──────────────┐
  GitHub issue ────▶│              │
  定时器 ──────────▶│  events 表   │  唯一索引去重
  手动投喂 ────────▶│  (inbox)     │  pending→running→done
                   └──────┬───────┘
                          │ 单 worker 串行取
                          ▼
                   ┌──────────────┐
                   │    worker    │  独立进程，开机自启
                   │  (独立进程)  │  起一次 run = `hermes -z` 子进程
                   └──────┬───────┘
                          │
          ┌───────────────┼───────────────┐
          ▼               ▼               ▼
   ┌────────────┐  ┌────────────┐  ┌────────────┐
   │progress/*.md│  │approvals 表│  │deliveries 表│
   │  记忆/进度  │  │  等你批准  │  │  节流/重投  │
   └────────────┘  └─────┬──────┘  └──────┬─────┘
                         │ 你点批准       │
                         ▼               ▼
                    起新 run 执行     飞书 / 桌面面板
```

**桌面面板**（Hermes 桌面插件，`~/.hermes/desktop-plugins/wick/`）展示：事件流、运行中任务、待批准动作、投递记录、健康度。

---

## 验收标准（三条硬指标）

| 指标 | 定义 | 目标 |
|---|---|---|
| **闭环成功率** | 一条 issue 进来，不人工干预能否产出「可批准的结果」——定义见下 | 先报 n/N，样本够之前不写百分比 |
| **重复处理率** | 同一条 issue 被处理两次的比例 | **0**（硬红线） |
| **投递误报率** | 它认为该做、你认为不该做的比例 | ≤ 20%（前 20 条人工标注） |

**「可批准的结果」的定义**（2026-11-24 定，见 issue #29）：

一次 run 走完**两段**才算一个闭环，两段不会同时发生：

| 段 | 判据 | 谁做 |
|---|---|---|
| **1. 产出** | run 结束、`approvals` 里**多出一行**（判「存在」，不判它的状态）、自检全绿（worker 实跑，非 agent 自报） | wick |
| **2. 落地** | 人批准后 `executed_at` 非空 **且** `detail.execution_error` 为空，**且** `deliveries` 里有一条带 PR URL 的行 | 人 → `process_approvals` |

> 为什么必须分两段：`handle_issue_fix` 的 docstring 写得很清楚——「A green suite is evidence, not permission: this never commits, pushes or opens a PR」。一次 run 只写 `waiting` 行，PR 是批准后 `process_approvals`（`db.py:366` 只捞 `status='approved' AND executed_at IS NULL`）才开。所以「PR 已开」和「行还 waiting」在同一时刻不可能同时成立——**两段是串联的，不是互斥的选项**，写成三条同时成立会让自动判据恒为 0。
>
> a 段为什么不能写 `status='waiting'`：b 段成功之后，那一行已经变成 `approved`。照字面写进 SQL，会把成功的那条判成没过 a 段——**又是一次恒为 0**。判「这一行存在」就够了，因为 `agent.py:355` 的 `if not checks["all_ok"]: raise` 在 `request_approval`（`:360`）之前，所以**行存在 ≡ 自检全绿**，不必去解析 `checks` 里的 "12/12" 字符串。
>
> b 段为什么不能只看 `executed_at`：`mark_executed` 在**执行失败时也写它**（`db.py:372-377`，同时把错误塞进 `detail.execution_error`）。所以只看非空会把失败算成成功，必须加 `execution_error` 为空。PR URL 只进 `deliveries` 文本（`worker.py:129-131`），没有列可查，所以第三项要查 `deliveries` 的行。

**分子钉死**：**闭环成功率 = a 和 b 都成功的条数 / 总条数**。只到 a（wick 干完、没人批）算失败——按「不人工干预」的口径，等人批不算介入，但如果一直没人批，这条就永远停在 a。

**只读分析不进样本**：它的 `checks` 是「—（只读分析，未改代码）」（`agent.py:296`），永远过不了 a 段。样本必须是走 fix 路径（改代码 + 跑自检）的 issue。

**判定方式**：自动判据筛上面两条，人用面板 approve 按钮触发第二段。

**人工介入的边界**：wick **没有自动重试**——失败即 `failed` 终态，重排要人跑 `cli.py retry`（`src/cli.py:113` 自称「The only retry the system has」）。所以按「不人工干预」的口径，**需要人跑 retry 的算失败**；**人手动解冲突也算失败**。

**样本**：4–6 条真实 issue（reviewer 发现的缺陷 + 现有真实缺口），**由 wick 的 worker 自己跑**，不是人工修。具体哪几条等 #30 定。

**空库怎么写**：`COUNT` 都是 0 时比率是 NULL（SQLite 的 0/0），不是 0%。**只能写 `0/0`，写 0% 是假的**。

**样本门槛**：n≥4 就报 n/N。要写百分比至少 n≥36 —— n/n 全通过的 95% 下界在 n=36 时才到 0.9026（n=28 是 0.8766）。写百分比时同时写 n 和下界。

**判定用的查询**（在真库上造 7 种终态验过：停在 a、b 成功、拒执行、执行异常、run 失败、人 reject、只读分析 → 返回 `1/7`）：

```sql
SELECT
  (SELECT COUNT(DISTINCT e.id) FROM events e
     JOIN approvals a ON a.event_id = e.id
    WHERE e.source_id IN (<样本>)        -- 手动列出那 4-6 条
      AND a.decided_at IS NOT NULL       -- a 段：行存在
      AND a.executed_at IS NOT NULL      -- b 段：执行过
      AND json_extract(a.detail,'$.execution_error') IS NULL   -- 且没失败
      AND EXISTS (SELECT 1 FROM deliveries d
                   WHERE d.event_id = e.id AND d.body LIKE '%已提 PR：http%'))
  || '/' ||
  (SELECT COUNT(*) FROM events e WHERE e.source_id IN (<样本>));
```

三个 b 段条件是**冗余的三重保险**，不是三道必需的门。`worker.py:112-131` 的三条路径写不同文案：成功写 `已提 PR：url`、拒执行写 `审批 #N 未执行`、执行异常写 `批准的动作执行失败`。所以任意两个条件都能判对；全写上是因为它们各自能扛住一种文案改动。实测：去掉 `execution_error` 那条，结果仍是 `1/7`（`deliveries` 的 URL 条件已经挡住了两条失败）。

**为什么先报 n/N 而不写 ≥90%**：4/4 全通过的 95% 置信区间约 40%–100%（`awk 'BEGIN{printf "%.4f", exp(log(0.025)/4)}'` → 0.3976，Clopper-Pearson 下界），宣称 ≥90% 是过度解读。样本够了再写百分比。

外加三条可靠性验收：崩溃恢复（kill worker 后同一条事件能重跑、不重复投递）、去重（同一事件灌两次只处理一次）、节流（超限自动压到次日）。

---

## 三分钟演示脚本

> **第一段（业务闭环）**
> GitHub 上开一条 issue → 5 分钟内面板出现这条事件 → 它读代码、定位、改、跑测试 → 停下来问"要不要提 PR" → 我点批准 → 投递记录出现 PR 链接。
>
> **第二段（可靠性）**
> 事件处理到一半，`taskkill` 掉 worker → 重启 → 同一条事件自动重跑，没有重复投递。面板上"重复处理率 0"。
>
> **第三段（自我进化）**
> 面板上这几条已经修好的 bug，是它开发自己时测出来的，也是它自己修的。提交记录在这里。

---

## 目录

```
wick/
├── README.md          项目门面（本文件）
├── DECISIONS.md       18 条关键决策 + 理由（防止后面改主意）
├── ROADMAP.md         10 个工作日排期
├── src/
│   ├── db.py          三张表 schema + 去重 + due_at 定时
│   ├── worker.py      inbox 消费者（独立进程）+ 租约（崩溃恢复 ≤120s）
│   ├── agent.py       一次 run：起 hermes 子进程、判定、写审批
│   ├── sandbox.py     一次性 worktree 的创建与 diff
│   ├── actions.py     审批通过后的真实动作（commit / push / 提 PR）
│   ├── outbox.py      投递队列（日上限节流 / 指数退避重投 / 真实发消息）
│   ├── cli.py         status / approvals / approve / reject / retry / schedule
│   ├── sources/       事件源（github.py = since 增量轮询）
│   └── prompts/       起 run 用的提示词
├── desktop/
│   ├── plugin.js      桌面面板
│   └── plugin_api.py  /api/plugins/wick/*
├── eval/
│   ├── cases.md       评测用例
│   └── run.py         硬指标评分器（已自检通过）
└── docs/
    └── PROCESS-code-agent-v1.2.md   早期 Code Agent 框架设计（参考资料）
```

---

## 投递队列（outbox）

事件处理的结果先写进 `deliveries`（`queued`），再由 outbox 真正发出去。**产生投递和发送是两步**：常驻 worker 每轮循环末尾自动 drain 一次，也可以手动发一轮：

```
python src/outbox.py --drain   # 发一轮，打印 {"sent":n,"throttled":n,"failed":n,"retrying":n}
python src/outbox.py           # 自检
```

状态机：`queued → sent`（送达，计入当日额度）、`queued → throttled`（超额，压到次日 0 点后自动回到 `queued`）、`queued → queued`（失败，退避后重试）、`→ failed`（重试用尽，终态不再发）。

并发安全：发送前先用一条 `UPDATE ... WHERE status='queued' AND next_attempt_at <= <now> RETURNING *` **原子占位**，占位后的行 10 分钟内对其他 drain 不可见。所以常驻 worker 和手动 `--drain` 同时跑不会重复发送；万一 drain 发到一半崩了，租约到期后这行会自己回来重发。

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `WICK_DAILY_LIMIT` | `20` | 每个本地自然日最多发几条，超出的压到次日 0 点 |
| `WICK_MAX_ATTEMPTS` | `4` | 一条投递尝试几次后判 `failed`（终态） |
| `WICK_BACKOFF_BASE` | `60` | 指数退避起始秒数：60、120、240…，单次等待上限 1 小时 |
| `WICK_SEND_TARGET` | 无 | **完整**投递目标，如 `feishu:oc_xxx`。投递行的 `channel` 只有平台名时必须设它 |
| `<PLATFORM>_HOME_CHANNEL` | 无 | 兜底目标，如 `FEISHU_HOME_CHANNEL=oc_xxx`；优先级低于 `WICK_SEND_TARGET` |
| `WICK_SEND_CMD` | `hermes send -t {channel}` | 换掉整条发送命令。**只有默认命令会替换 `{channel}`**，自定义命令原样切分 |
| `WICK_DB` | `~/.wick/wick.db` | 数据库路径；`--drain`、worker、`cli.py` 的 `--db` 缺省都读它 |

**默认命令必须能解析出完整 target**。`hermes send -t feishu` 在没配 home channel 时会打印 `No home channel set`、**什么都不发、退出码仍是 0**——所以 `resolve_target()` 会先按「显式 `platform:chat_id` → `WICK_SEND_TARGET` → `<PLATFORM>_HOME_CHANNEL`」补全，都取不到就直接报错并列出这三种修法，不让它变成静默重试。

单次发送超时固定 120 秒（`src/outbox.py` 里的 `SEND_TIMEOUT`，不读环境变量）。

`WICK_SEND_CMD` 按命令行规则切分：Windows 上反斜杠原样保留，`C:\Tools\send.cmd` 可以直接写；路径含空格要加引号，例如 `WICK_SEND_CMD="C:\Program Files\send.cmd" -t feishu`。正斜杠两边都能用。

---

## 定时器（due_at）

事件可以带一个到期时间：`events.due_at`，与 `db.now()` 同一种 ISO 格式，NULL = 立即到期（老行全是 NULL，行为不变）。`claim_next` 只领 `due_at IS NULL OR due_at <= now` 的行，所以「两小时后回来做」就是一条普通事件——没有 cron、时区处理、优先级。

```
python src/cli.py schedule 2026-11-23T09:00:00+08:00 "复查 outbox 节流"
```

agent 自己也能排：fix / analyze 两个模板的输出槽 `下次跟进：<ISO 时间> <做什么>`，run 结束后解析成 `source_type='followup'` 的事件。同一时间 + 同一说明靠去重只入一条（retry 重跑重复解析挡得住）；时间非法或已过期只记 warning，不会搞挂一次成功的 run。不带时区偏移的输入按本机时区解释，入库前统一成 UTC。

---

## 代码约定

- **注释和 docstring 一律英文**，包括行内注释和模块说明。代码是给所有读者看的，中文注释会把非中文读者挡在外面。
- **面向用户的字符串保持中文**：投递内容、错误提示、CLI `--help`、日志消息。这些是产品文案，不是代码注释。
- **一个 inbox 只允许一个活跃 worker**（见 `DECISIONS.md` #5）。要并行就按 `source_type` 分片，不要在 worker 里加线程。
- **每个非平凡模块留一个 `--selftest`**，用 `assert` 断言，不引入测试框架。全部自检用一条命令跑：`python src/agent.py --checks`（清单在 `CHECKS` 里，CI 调的也是这一条）。

## 状态

MVP 开发中。里程碑见 [ROADMAP.md](./ROADMAP.md)。
