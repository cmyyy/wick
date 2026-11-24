# 关键决策记录

两轮 grilling 的产出。**每条都写了理由和否决掉的选项**——目的是三个月后回头看，能知道当时为什么这么定，而不是重新纠结一遍。

改动任何一条，请在这里追加一条新记录，不要直接覆盖。

---

## 决策总表

| # | 决策 | 内容 |
|---|---|---|
| 1 | **项目身份** | Personal Agent 主线；GitHub issue 处理是它接的第一个任务域 |
| 2 | **侵入方式** | 纯插件，一行 Hermes 源码不改 |
| 3 | **演示场景** | GitHub issue 自动处理（求职场景已废弃） |
| 4 | **issue 来源** | 自己仓库，开发中真实积累 ≥10 条；Hermes 上游二期 |
| 5 | **worker 形态** | 独立 Python 进程，开机自启 |
| 6 | **数据模型** | 三张表：events / deliveries / approvals |
| 7 | **deferred 实现** | 土办法：写 approvals → run 结束 → 批准后起新 run |
| 8 | **MVP 范围** | 砍掉薄 Router |
| 9 | **UI** | Hermes 桌面插件面板，能批准 / 拒绝 |
| 10 | **评测** | 三条硬指标（闭环成功率 / 重复处理率 / 投递误报率） |
| 11 | **工期** | 10 个工作日 |
| 12 | **开发流程** | 所有改动走 issue → branch → PR；agent 可开 PR，不可 merge |
| 13 | **产品 vs 开发流程** | wick 是产品（五原语）；当前开发是人工编排 + bot 分工，两者未接上。自举等 D9 通过 |
| 14 | **进度文件位置** | `~/.wick/progress/`，在仓库之外。写在 worktree 里会随 attempt 一起被销毁 |
| 15 | **定时器只做一次性绝对时间** | 不做 cron / 时区 / 优先级；要周期就每次执行时再排一次 |
| 16 | **派活走固定 Bot Chat** | 不用 `-z`（无记忆）、不用 `--continue`（接最近会话会错）；issue 是唯一事实源 |
| 17 | **降级的是「3 分钟」，不是整个机制** | 「3 分钟」区分不了「慢」和「死」（要进度信号），口径降级为已知上限；但「独立线程 + 超时 kill + attempts 上限」很便宜，作为 #26 第一步 |
| 18 | **闭环 = 两段串联，不是一次 run** | a 段：run 产出 `waiting` approval + 自检绿；b 段：人批准后 `executed_at` 非空 **且 `detail.execution_error` 为空** 且 `deliveries` 有 PR URL 行。两段在同一时刻不可能同时成立（互斥），所以必须串联判，写成一次 run 会恒为 0。无自动重试，人跑 `retry` 或解冲突都算失败 |

---

## 展开说明

### 2. 为什么是纯插件

Hermes 是活跃维护的上游仓库。fork 之后 `hermes update` 永久失效，项目变成"维护一个过时的 Hermes"。纯插件能跟随上游升级，还能作为独立第三方插件发布。

唯一代价：有些钩子要绕（比如 cron 硬编码了执行路径）。所以先探边界，**只在证明插件做不到时才动源码**。

### 3. 为什么放弃求职场景

数据源确实不存在可用的：

- **JobSpy**（4k stars）：只覆盖 LinkedIn / Indeed / Glassdoor 等海外平台，**中国岗位一条都拿不到**
- **boss-zhipin-scraper**（1.5k stars，CDP 方案）：靠模拟真人，翻页等 12-22s、详情间隔 10-25s，做 demo 就是自找翻车
- **V2EX 酷工作有 RSS**：能订阅，但岗位量少、方向匹配度低

GitHub REST API 是当时评估的所有选项里最好的：免费、稳定、无限量、`since` 参数天然支持增量、`issue.id` 天然去重。

### 4. 为什么 issue 来自自己仓库

- **真实**：开发中遇到的 bug / TODO 就是真任务，不用造数据
- **可控**：自己的代码最懂，演示成功讲得清、翻车不丢人
- **dogfooding**：处理完项目真的变好了

Hermes 上游 issue 放到二期——处理大仓库要先满足它的 AGENTS.md 约束，是独立工作量。

### 5. 为什么 worker 是独立进程

| 选项 | 否决理由 |
|---|---|
| 插件后端线程 | 桌面端一关就死，不是 always-on |
| 靠 cron 每分钟触发 | 浪费 LLM 调用，延迟最多 1 分钟，且不是事件驱动 |
| **独立进程** ✅ | 只有一个进程要管，日志独立，崩了重启即可，和网关同样的开机自启方式 |

### 6. 为什么三张表而不是一张

状态机不同，塞一张表会得到一堆互斥的空字段：

- `events`：pending → running → done / failed
- `deliveries`：queued → sent / throttled / failed
- `approvals`：waiting → approved / rejected / expired

去重键统一：`sha256(source_type + source_id)` 建唯一索引。

### 7. 为什么 deferred 用土办法

真正的实现是"中断正在跑的 turn、保存 session、等答复再恢复"——要深挖 Hermes 的 turn 中断语义，风险高。

土办法效果对用户体验一致（真要动手前一定停下来问），但不用碰 turn 语义：

1. skill 层约束：任何不可逆动作（提 PR、push、合并）必须先往 `approvals` 写一条，然后 run 结束
2. 结果投递到 outbox：「我要给 X 提 PR，批准吗？」
3. 你批准 → 状态变 approved → worker 收到事件 → **起一个新 run 执行**

代价：批准后是新会话，上下文靠记忆文件接上，不是同一条 transcript。这个代价可以接受。

### 8. 为什么砍掉薄 Router

原计划里"薄 Router"（三分类：续任务 / 单 Worker / 拆多 Worker）挪到二期。

理由：正在投递中，**先有能演示的东西比架构完整重要**；没有 Router 时 worker 是单任务串行，正好符合上游建议"一个 inbox 一个活跃 worker"。Router 一周内能补回来。

面试被问 Super-agent 时答："做了一版薄 Router 在分支里，但没合入主线——我的负载还没到需要它。"比硬塞一个用不上的架构更有说服力。

### 10. 为什么只保留三条硬指标

早期的四层 grader（硬门槛 + 质量 rubric + 轨迹指标 + 描述一致性）来自 Code Agent 框架设计，对 MVP 太重。

**前 20 条人工标注**撑不起一个总分制，但能撑起"3/20 误报"这种可核实的事实——符合项目红线"引用数字必可核实原文"。rubric 和 judge 校准全部挪到二期。

---

### 12. 为什么所有改动都要走 issue → branch → PR

D1-D5 是直接提交到 `main` 的：没有 issue 当 ticket、没有分支、没有 PR review。这是错的，而且错得很难看——**项目本身在做「危险动作要人批准」，开发流程却一路直达 main**。

从 D6 起强制：

- 每个改动先有 issue（issue 就是 ticket，验收标准写在里面）
- 每个改动走 `<type>/<issue>-<slug>` 分支
- PR body 写 `Closes #N`，七个自检全绿
- **agent 可以开 PR，不可以 merge**——merge 是人的动作，和 approvals 表同一个道理

细节见 `CONTRIBUTING.md`。D1-D5 的历史提交不追溯改写。

---

### 13. 产品形态 ≠ 当前开发流程（别混着讲）

**wick 是产品**：一个 agent 盯事件队列，无人值守干活（五原语）。
**我们现在做的事**：多个 bot 分工开发 wick 本身，人在环上拍板。

这两件事**今天还没接上**，差在这里：

| 五原语 | wick 的目标形态 | 当前开发流程 |
|---|---|---|
| 事件驱动 | 触发写进 `events` 表，worker 自己取 | orchestrator 直接 `hermes -p <bot> -z` 起子进程派活 |
| 投递 | 结果进 `deliveries`，dispatcher 投出去 | 结果通过 subprocess stdout 回到 orchestrator |
| 审批 | `approvals` 表 + deferred | 一致（人和 orchestrator 都走这张表） |
| reviewer | 无此角色（Anthropic 对应的是轻量分类器） | 独立的 `wick-reviewer` 完整会话 |

截至写下这条时，四个 bot 协作产生的 `deliveries` 记录是 **0 条**——这就是"没接上"的实证。

**为什么现在这样**：wick 还没通过 D9 可靠性验收（重复投递 0、崩溃恢复可用）。拿一个连"不重复投递"都保证不了的半成品当开发工具，是在沼泽地上盖房子。人在环上才抓得出并发 bug——D6 那两个 bug 就是这么抓到的。

**禁止的表述**（这是防三个月后自己骗自己，也防面试被问穿）：

- ❌ "wick 在自己开发自己"
- ❌ "我们用 wick 的方式开发 wick"
- ✅ "wick 是产品形态；当前开发流程是人工编排 + bot 分工"

**收敛时机**：**D9（可靠性验收）通过之后**，把派活从 subprocess 改成写 `events` 表、结果进 `deliveries`。到那时才算自举。在那之前不许提前接。

---

### 14. 为什么进度文件放在仓库之外

`~/.wick/progress/`，不是 `<repo>/progress/`。

#10 给每次 attempt 一块独立的 worktree，失败时整棵树连同半成品一起销毁——这正是我们要的隔离。但进度文件的作用恰好相反：它必须在 attempt 之间活下来，否则"下一轮接着上一轮"无从谈起。写进树里，它就是第一批被销毁的东西。

自检里有一条断言 `PROJECT_ROOT not in path_for("x").parents`，专门盯着这件事别退化。

### 15. 为什么定时器只做一次性绝对时间

`due_at` 是一个具体时刻，不是规则。要周期任务，就在每次执行时再排下一次。

理由：cron 表达式要引入语法、时区、"这次算不算错过"的补算语义，而 MVP 只需要"过一会儿回来做这件事"。等真出现"每小时扫一次"这种稳定需求再加，那时也知道该用哪种语义，不用此刻猜。

时区一律本地时间。`due_at` 与 `db.now()` 用同一种 ISO 格式，所以字符串比较等价于时间比较——省掉一个解析器，代价是两边格式必须永远同步，代码注释里写明了。

### 16. 为什么派活走固定 Bot Chat

第一版是 `hermes -p <bot> -z "<超长 prompt>"`——一次性的，而且把 issue 正文、ticket、补充说明全抄进 prompt。

三个后果，都实际发生过：

1. **bot 没有记忆**。它不记得上一个任务的判断，只能靠 issue；而 issue 里没写的东西（比如"不要用 cron 表达式"）就永久丢了。#14 的六条设计约束当初只在 prompt 里，事后才补进 issue。
2. **同一份事实存两处**。issue 更新后，prompt 里的旧副本成了错误信息来源。x.ai 在 Grok Bot 的设计文章里点名这是反模式：「Do not copy shared context into each Bot. That is the drift mechanism, and it fails silently.」
3. prompt 大半是 bot 自己一条 `curl` 就能拿到的东西。

改成 Hermes 原生 bot mode 的固定会话名 `Bot Chat`（见 `apps/desktop/src/plugins/hermes-bots/soul.ts`），prompt 只说三件事：issue 地址、worktree、分支。记忆跟着 bot，事实跟着 issue。

`--continue` 也不用：它解析"最近一次会话"，并发或换目录会接错；`Bot Chat` 按名字定位。

### 17. 为什么降级的是「3 分钟」，不是整个机制

D9 的验收原本有一条「制造卡死的 handler，看门狗 3 分钟内放回队列」。实测做不到，而且不是差一点。

**这条路径当前不存在**（两项叠加）：

1. `_renew_lease` 是 `while not stop.wait(30)`，没有预算 —— 心跳年龄永远落在 [0,30)s，`LEASE_SECONDS=90` 的门槛永远碰不到。
2. `requeue_stale` 只在 `serve()` 启动时和每次空闲时调用 —— handler 卡住时主线程正阻塞在 `handle()` 里，走不到那一行。

**「3 分钟」这个数字本身站不住**：合法的 fix run 要 25 分钟（`FIX_TIMEOUT=1500s`），卡死的 run 也是 25 分钟，从外部看完全一样。心跳证明 liveness，证明不了 progress。要区分「慢」和「死」，必须有进度信号 —— 这半省不掉。

**但「3 分钟做不到」不等于「整个机制不做」。** 两件事曾被一起否决，分开看：

| 要什么 | 成本 |
|---|---|
| 区分「慢」和「死」 | 进度信号（心跳带 `phase`）—— 贵，要 agent 每个阶段报到 |
| 别让一条卡死拖垮整条队列 | handler 跑独立线程 + 主线程按预算 kill 子进程树、继续 `requeue_stale`、继续消费下一个 —— **便宜**：worker 一个循环加一个线程，`finish_event` 的 fence 和 attempt 级 worktree 隔离都是现成的 |

第二件事跟「3 分钟」无关，它解决的是**队头阻塞**：串行架构下一条卡死，整条队列停摆最长 15/25 分钟。这是真实的可用性代价，不该因为「3 分钟做不到」被一起否掉。

还有一条更便宜的：子进程超时后终点是 `failed` 而不是 `pending`，因为无脑 requeue 会无限重跑。但 `attempts` 是现成计数器 —— `attempts >= 2 就 failed，否则 requeue` 是一条 if，能把恢复终点从 `failed` 变成 `pending`。

**现在的边界**：

| 故障 | 表现 | 恢复 |
|---|---|---|
| 进程死了（kill / 断电） | 心跳停 | **≤120s 自动回收**（= 最后一拍 + `LEASE_SECONDS` 90s；已验证） |
| 进程活着，handler 卡死 | 心跳照跳 | 不会自动回 `pending`。15/25 分钟子进程超时后转 `failed`，用 `cli.py retry` 重排。**这段时间整条队列停摆（队头阻塞）** —— 由 #26 解决 |

**`≤120s` 而不是 `≤90s`**：上界是「最后一拍 + 租约」，而心跳每 30s 一拍，所以从崩溃到被回收最多 120s，且前提是有人把 worker 再起来。

### 18. 为什么闭环要拆成两段

「闭环成功率」原来的判据是「PR 开出来 + 自检全绿 + 有待批 approval」，听着合理，但和状态机矛盾：

- `handle_issue_fix` 的 docstring：*A green suite is evidence, not permission: this never commits, pushes or opens a PR.* 一次 run 成功时写的是 `status='waiting'` 的 approval 行。
- PR 由 `execute_approval` 里的 `open_pr` 开，入口是 `process_approvals`，它只捞 `status='approved' AND executed_at IS NULL`（`db.py:366`）。

所以「PR 已开」和「行还 waiting」在同一时刻不可能同时成立。三条一起当判据，自动判定恒为 0，指标永远测不出来。

拆成两段之后各自可判：

| 段 | 判据 | 谁做 |
|---|---|---|
| a 产出 | run 结束 + `waiting` approval + 自检全绿 | wick |
| b 落地 | `executed_at` 非空 + PR 真开出来 | 人批准 → `process_approvals` |

**顺带修正两处过强的话**：

- **没有自动重试**。失败是 `failed` 终态，重排要人跑 `cli.py retry`（它自己注释写着 "The only retry the system has"）。所以「允许自动重试一次」是假的，按不人工干预的口径，需要 retry 就算失败。
- **n=4 不能支撑 ≥90%**。4/4 的 95% 置信区间约 40%–100%（Clopper-Pearson 下界 `exp(log(0.025)/4)` ≈ 0.398）。所以 README 与 ROADMAP 都改成报 n/N。

## 被否决的候选（避免重复讨论）

| 候选 | 否决理由 |
|---|---|
| 求职自动化场景 | 国内岗位无可用数据源（见决策 3） |
| fork Hermes | `hermes update` 永久失效 |
| 事件收件箱用 cron 顶替 | cron 是"定时即执行"，没有队列语义，无法去重 / 恢复 |
| 语义去重（MVP） | `source_id` 硬去重覆盖 90% 场景；语义去重二期 |
| 200-agent 组织门禁 | 需要规模才有意义，单 agent 做出来是空壳，且无法证伪 |
| Web UI（独立） | 桌面插件用官方 UI kit，视觉一致且零构建 |

---

## 项目命名

**wick** = 烛芯。火从这里开始，一直烧到你回来；灯芯吸油对应"事件被吸进来"，是 inbox 的准确隐喻。

撞名检查（GitHub search API）：无纯名 `wick` 占用，只有 WickedEngine / wicked_pdf / wicked 等 `wicked*` 变体，不构成混淆。

否决过的：`nightshift`（5 个同赛道项目占用）、`amber` / `ember`（ember.js 22k 星）、`glow`（charmbracelet/glow 28k 星）、`spark`（Apache Spark）、`lantern` / `firefly` / `lumen` / `cinder` / `flick`（均有 3k+ 同名项目）。
