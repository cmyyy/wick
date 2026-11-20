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
