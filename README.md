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
| **wick 的开发过程** | 人工编排 + 多个 bot 分工（一个 orchestrator 拆任务、两个 implementer 写代码、一个 reviewer 审查），人在环上拍板 |

**这两件事现在还没接上。** 开发 wick 时派活是直接起子进程，结果走 stdout，不经过本仓库的 `events` / `deliveries`。

所以下面这些说法是**错的**，不要用：

- ❌ "wick 在自己开发自己"
- ❌ "我们用 wick 的方式开发 wick"

正确的是：

- ✅ "wick 是产品形态；当前开发流程是人工编排 + bot 分工"

**什么时候接上**：D9 可靠性验收通过之后（重复投递 0、崩溃恢复可用），再把派活改成写 `events` 表、结果进 `deliveries`。在那之前拿半成品当开发工具，等于在沼泽地上盖房子。

详见 `DECISIONS.md` 决策 13。

---

## 五个原语

常驻 agent 不是"一直在跑"，而是**大部分时间在睡，周期性醒来干一段活**（一次 run），干完存状态继续睡。

| 原语 | 作用 | wick 的实现 |
|---|---|---|
| **事件收件箱** | 所有触发统一进入口 | `events` 表 + 唯一索引去重 + 单 worker 串行消费 |
| **定时器** | 到点叫醒 | 定时器只**产生事件**，不直接执行 |
| **记忆** | 跨 run 的连续性 | `progress/<task>.md` 进度文件 + 索引按需加载 |
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
                   │  (独立进程)  │  调 gateway RPC 起一次 run
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
| **闭环成功率** | 一条 issue 进来，不人工干预能否产出"可批准的结果" | ≥ 90% |
| **重复处理率** | 同一条 issue 被处理两次的比例 | **0**（硬红线） |
| **投递误报率** | 它认为该做、你认为不该做的比例 | ≤ 20%（前 20 条人工标注） |

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
├── DECISIONS.md       13 条关键决策 + 理由（防止后面改主意）
├── ROADMAP.md         10 个工作日排期
├── src/
│   ├── db.py          三张表 schema + 去重
│   ├── worker.py      inbox 消费者（独立进程）
│   ├── outbox.py      投递队列（日上限节流 / 指数退避重投 / 真实发消息）
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

## 代码约定

- **注释和 docstring 一律英文**，包括行内注释和模块说明。代码是给所有读者看的，中文注释会把非中文读者挡在外面。
- **面向用户的字符串保持中文**：投递内容、错误提示、CLI `--help`、日志消息。这些是产品文案，不是代码注释。
- **一个 inbox 只允许一个活跃 worker**（见 `DECISIONS.md` #5）。要并行就按 `source_type` 分片，不要在 worker 里加线程。
- **每个非平凡模块留一个 `--selftest`**，用 `assert` 断言，不引入测试框架。九个自检：`python src/db.py`、`python src/worker.py --selftest`、`python src/sources/github.py`、`python src/agent.py`、`python src/sandbox.py`、`python src/actions.py`、`python src/cli.py selftest`、`python src/outbox.py`、`python eval/run.py --selftest`。

## 状态

MVP 开发中。里程碑见 [ROADMAP.md](./ROADMAP.md)。
