# 贡献流程

## 铁律

1. **每个改动都有 issue。** 没有 issue 的改动不开工。
2. **派活之前先发 ticket。** 验收标准、执行者、分支写进 issue 评论，然后再派。先派后补等于没有依据，而且事后写不进当初的判断。派活的 prompt **只给 issue 地址**，不复制 issue 内容（见「派活纪律」）。
3. **每个改动都走分支 + PR。** 谁都不直接 push `main`，**包括项目自己的 agent**。
4. **自检全过才能提 PR。**
5. **PR 由人 review 后 merge。** agent 可以开 PR，不可以 merge。

## 派活纪律

派活只给三样东西，不给背景：

```bash
hermes -p developer chat --in ~ -c "Bot Chat" --create-if-missing -Q -q   "实现 https://github.com/cmyyy/wick/issues/<n>。工作目录 <worktree>，分支 <branch>。只在 worktree 里改，不要 commit/push/开 PR。"
```

三条理由，对齐 x.ai 对 Grok Bot 的设计（https://x.ai/news/designing-grok-bot）：

- **记忆跟着 bot，不跟着任务。** `Bot Chat` 是固定会话名，同一个 bot 跨任务累积。`hermes -p <name> -z` 是一次性的，它不记得上一个任务的任何判断；`--continue` 也不用，它接「最近一次会话」，并发或换目录时会接错。
- **不复制上下文。** issue 是唯一事实源。把背景、约束、验收标准抄进 prompt，等于给 bot 发一份可能过期的副本 —— x.ai 原话：「Do not copy shared context into each Bot. That is the drift mechanism, and it fails silently.」**约束没写进 issue 就是没写**，下一个读 issue 的人看不到它。
- **一次一个任务。** Bot Chat 里混着多个在飞的 issue，bot 会串。做完一个再下一个。

**issue 里写什么**：做什么、怎么算做完、硬要求（不许做什么）。
**不写什么**：为什么做这件事 —— 它自己去代码、README、`DECISIONS.md` 里查得出来。

## 验证纪律

五条都来自实际失误（D7 / D8 / D9 / #31），不是预防性的空话。

### 结论必须能指认验证它的是什么

**合格**：
- 「浏览器里四块都渲染了、点批准该行消失，库里 `executed_at` 仍是 NULL」
- 「真子进程 + 真端口 + 真库，十二个自检全绿，三处变异都红」

**不合格**：
- 「插件被发现了」—— 哪个发现？进程内调函数，还是服务里真扫到？
- 「路由挂上了」—— 凭什么？dashboard 的 `auth_middleware` 对任意 `/api/` 路径在路由之前就回 401，**路由不存在也是 401**
- 「某处已经在做这件事了」—— 跑过 `git log -S "<符号>"` 吗？

**合格的说法要能复现，格式是三段：命令 + 原始输出片段 + 作用域（哪个进程 / 哪个端口 / 哪个库文件）。**

> 只"指认"是不够的。D8 那次确实指认了检查——「跑过 `_discover_dashboard_plugins()`」——错在没说作用域：那是在**进程内**调的，不是运行中的服务。加上作用域这一栏，当场就不合格。

**进程内调用不是集成证据。** 调一个函数拿到期望值，只证明那个函数在当前环境下正确，不证明真实进程会走到它。D8 的 dashboard 插件就是这么翻车的：进程内 `_discover_dashboard_plugins()` 返回它，运行中服务的 `/api/dashboard/plugins` 里没有，浏览器侧边栏只有 Kanban / Achievements。

#### 这类错长什么样

五条都是真实发生过的。共同点不在作者（有的是编排者写的，有的是实现者写的），在**写的时候都以为对**。每行第三列是当场能核的命令：

| 写下的 | 实际 | 怎么核 |
|---|---|---|
| 「那个 key 在全仓任何分支都没有写者」 | 当时确实没有；#25 合入之后有了写者（`src/db.py` 的 `requeue_stale` 调 `set_meta`） | `grep -n "set_meta" src/db.py` |
| 「预算 = `FIX_TIMEOUT + 4×CHECK_TIMEOUT`」 | 代码里没有这个预算 | `grep -rn "FIX_TIMEOUT + 4" src/` |
| 「心跳反复在 2.8–22.9s 之间」 | 手打进生成器的常量，不是采样 | 现在是采样值：`grep -n "由 check_zombie 采样算出" eval/reliability.py`；原始那版见 PR #27 的首个 commit |
| 「wick 内部不恢复」 | 子进程超时转 `failed`（fix 最坏 1500s + 11×`CHECK_TIMEOUT`）；重排要人手动 `cli.py retry` | `grep -n "finish_event.*failed" src/worker.py`（超时在 `agent.py:327-330` 的 try/except 里抛出，`worker.py:177` 接住写 failed） |
| 「手改 `report.md` 会被抓」 | PR #35 首版只比判定列，改数字和改正文全放过 | 现在整份逐字比：`grep -n "_VOLATILE" eval/check-report.py` |

写这类句子时，**先把第三列跑一遍**。跑不出所示输出的，句子就还没到能写的程度。

第 2 行的第三列是「证明它不存在」——`grep` 返回 exit 1、输出为空，那正是证据。其余四行都要有输出才算数。

**不要用 SHA 当证据**，除非那个 commit 在某个 ref 上活着：squash 合入的分支被删之后，原 commit 就成了悬空对象，在你本机能 `git show`，在别人的 clone 上是 `fatal: invalid object name`。本表第一版就犯过这个错。

### 每一轮改动都派 reviewer

不由编排者判断「这次改动小 / 逻辑没变 / 只是换壳，就不派了」。

D7 和 D8 都是编排者先自己下结论、后补 reviewer，而 reviewer 两次都抓到了编排者看不见的东西：

- D7：那个功能在生产里**没有消费者**（done/failed 是终态，唯一会重跑的路径恰恰写不了进度）
- D8：假死回收读的 meta key **全仓没有写者** —— 而这个前提是编排者自己在 ticket 里编出来的

reviewer 不是可选复核，它是唯一能对冲「编排者倾向于接受一个便宜信号」的机制。

**它的严重问题只有两种下场：改，或者把不改的理由写进 PR 的「遗留」节。** 没有第三种——「派了、被指出问题、照样 merge」不算合规。

这条是**约定**，不是机制：没有东西能证明 reviewer 真看过。它的唯一落地物就是 PR「遗留」节里那段结论。（见 § 这些规则怎么强制）

### ticket 里的技术断言要能指回出处

写进 issue 或 ticket 的断言（「某处已经在做 X」「某表已经有 Y 字段」）必须附一行证据：文件名加行号，或者一条能跑的命令。

对「**别的改动已经在做 X**」这类断言，**文件名加行号是不够的**——D8 那条 `plugin_api.py:158` 的行号确实存在，问题是**没人写那个 key**。必须给可执行的证据，三选一：

- `git log -S "<符号>"` 的输出
- 已合并进 `main` 的 commit
- 未合并，那就写 PR 号

### 写「做不到 X」之前，读一遍兜底路径

`except` 分支、超时处理、有没有另一条命令能补救。

2026-11-24 我写过「agent 不能 merge」，证据是 `grep merge src/actions.py` 零匹配。但 agent 是带 shell 的 `hermes -z` 子进程，环境里有 `gh`——拦住它的是 `src/prompts/issue_fix.md:60` 的提示词禁令，**不是代码里没有那条路**。证据能覆盖的范围比结论小，就是写反了。

### 「通过」要能点出机制

一个检查通过，要说得清是**哪个机制**让它通过的：唯一索引、fence、超时、还是上下文差异。点不出来就是没验。

两次栽在这上面。`--crash` 演练里「`deliveries` 仍 1 条」有一半是结构性保证——`add_delivery` 在 `handle()` 返回之后才调用，被 kill 的那次 attempt 根本没机会写投递，所以 fence 坏了它也会通过。`check-report.py` 抓到改数字是靠上下文差异，不是因为它比对了那个数字。

**「已经在落」这种时态不算证据。** 它正是让一个不存在的 key 看起来已定案的说法。

D8 的 ticket 里写「假死回收次数读 `meta['stats:stale_requeued']`（#10 已经在落这个计数）」—— 那个 key 当时在任何分支都不存在（`git log -S` 为空；#25 合入之后才有了写者）。developer 照它实现了，面板读到一个永远是 0 的假数字，最后单独开 #20 补数据源。**编排者编一个前提，实现者会照着它把错误固化下来。**

实现者有对称的义务：**开工前把 ticket 里每条可验证断言各跑一条**；不成立的停下回问，不要替它实现数据源。

## 分支命名

**人写的分支**：

```
<type>/<issue-number>-<短横线小写描述>
```

`type` 取值：`feat` / `fix` / `chore` / `docs` / `refactor`

```
feat/3-outbox-throttle
fix/12-requeue-race
chore/8-issue-driven-workflow
```

**wick 自动产出的分支**（`src/sandbox.py` 生成）是另一套：

```
wick/issue-<n>-run-<event_id>[-attempt-<k>]
```

这是它的 worktree 隔离键，承载 `issue + event + attempt` 三段信息，硬套 `<type>/<issue>-<slug>` 会丢掉 attempt 或长得离谱，所以不套。人 review 的是 PR，不是这个分支名。

wick 产出的 **commit 前缀**确实跟类型走（`src/actions.py` 的 `_commit_prefix()`：按 issue 的 `labels` 映射 `docs`/`chore`/`refactor`/`feat`/`fix`），**PR body 固定四节**（改了什么 / 怎么验证的 / 审了什么 / 遗留）。工具的产出必须符合本文档，否则规则只约束人等于没有规则。

## Commit 格式

```
<type>: <一句话总结> (#<issue>)

<可选正文：为什么这么改，而不是改了什么>
```

```
feat: throttle outbox deliveries per day (#3)

The desktop panel reads the counter from meta, so it has to survive
restarts; a dedicated table was overkill for one number.
```

## 提 PR 前跑什么

```bash
python src/db.py
python src/worker.py --selftest
python src/sources/github.py
python src/agent.py
python src/sandbox.py
python src/actions.py
python src/progress.py
python src/cli.py selftest
python src/outbox.py
python eval/run.py --selftest
python src/server.py --selftest
python eval/check-report.py --selftest
```

**代码层只有一个事实源**：`src/agent.py` 里的 `CHECKS`。`python src/agent.py --checks` 跑的就是它，CI 调的也是这一条，所以往 `CHECKS` 里加一项，CI 和 worker 的门槛都会跟着走。

**上面的列表是给人读的，CI 不读它，改 `CHECKS` 它也不会自动变**——要手工跟一次。这句话上一版写成"不用手工同步"，那是假的：`README` 里曾有一份十条的副本落后了两条没人发现，就是这个代价。所以：**加自检 = 改 `CHECKS`（必须）+ 顺手改这份列表（给下一个人看）。**

## 可靠性验收（改动了 worker / db / outbox 才跑）

```bash
python eval/reliability.py --dup      # 约 10s：同一事件灌 3 次只有 1 行
python eval/reliability.py --crash    # 约 2 分钟：kill 掉跑一半的 worker，等租约过期重跑
python eval/reliability.py --zombie   # 约 3.5 分钟：卡死的 handler 能否被回收（当前是已知上限，见 DECISIONS 17）
python eval/reliability.py --all      # 全跑，任一不通过就非零退出
```

这三条是**真进程、真库、真 skill**，不是 mock：`--crash` 会真起 worker 再 `taskkill /F`。所以它们比自检慢得多，只在动到 `worker.py` / `db.py` / `outbox.py` / `agent.py` 的租约或投递逻辑时跑。每条判定后面都跟着产生它的 SQL。

## 生成物不要手改

`eval/report.md` 是 `python eval/reliability.py --all` 的输出，它的判定列由脚本算出来。手改它会被抓出来：

```bash
python eval/check-report.py            # 约 6 分钟：重跑 --all 到临时路径，比对判定列
python eval/check-report.py --selftest # 1 秒：只验解析器本身
```

什么时候跑：

- 改动了 `eval/reliability.py` 的判定逻辑或报告生成
- 改动了 `eval/report.md`（本来就不该改 —— 跑了就知道）
- 改动了 `src/` 里影响可靠性结论的东西（worker 租约、`db.requeue_stale`、`outbox` 投递）

比对的是**整份文件逐字**，只把两样东西按设计打码：时间戳那一行，和心跳采样区间（`2.4–22.5s` 这种，每次跑都不同）。所以改了判定、改了实测数字、改了正文的一句话，都会红。

> 为什么单列一节：这条规矩来自一次真实的错。生成的报告里有一句「心跳年龄反复在 2.8–22.9s 之间」，那个区间是手打进生成器字符串的，不是采样出来的 —— 它和同一页表格里的数字对不上。
>
> 注意这个检查**抓不到这一类**：手写的区间如果正好长成「N–Ms」，会被当成易变部分打码放过。那一条靠的是生成器只印一次、且印的是真采样值，不靠事后比对。检查的边界写在这里，免得下一个人以为它管得更宽。

## 自动跑的部分

`.github/workflows/checks.yml` 在 PR 上跑全部自检（14 秒，无依赖、无网络）。不需要谁记得跑。

**没进 CI 的两个**：

- `eval/reliability.py --all` —— 约 6 分钟，要真起进程并 kill 子进程
- `eval/check-report.py`（不带 `--selftest`）—— 它跑前者，而且**依赖 Windows 的 `taskkill /F /T`**（`--crash` 那一条用它制造真崩溃）。所以它注定进不了 ubuntu runner，不要试图塞进去

两者都属于「改动了那几处才跑」的本机检查。

## PR 要求

- 标题同 commit 首行
- body 第一段写 `Closes #<issue>`
- body 说清三件事：改了什么、怎么验证的、遗留什么
- **不自己 merge**

## 这些规则怎么强制

写在这里的规则，一部分能被机制强制，一部分只能靠约定。**分清楚比假装都强制了更有用。**

**机制强制的（2026-11-21 起）**

- `main` 已开 branch protection：禁止 force push、禁止删除分支

**无法用 GitHub 机制闭环的**

- **"必须走 PR"和"agent 不 merge"**：GitHub 的 `required_pull_request_reviews` 要求至少 1 个 approval，而单人仓库里作者不能批准自己的 PR——开了等于把整个仓库锁死。所以没开。
- 真正的约束是两件事：① **wick 的审批执行路径**里没有 merge——`src/actions.py` 只有 commit / push / PR，2026-11-24 核过，`grep merge src/actions.py` 零匹配；② 人 review 每个 PR。
- ⚠️ ① 只覆盖审批路径，**agent 自己拦不住自己** —— 详见 § 验证纪律「写「做不到 X」之前，读一遍兜底路径」那条。
- 要机制化，需要第二个账号（bot reviewer）或 CI 门禁——都超出当前范围。

**结论**：这条规则是**约定（人 review + prompt 禁令）+ 审批路径里的能力缺失**，不是强制。任何文档都不该把它写成"已经强制"。等真有第二个账号时再改这一节。

**§ 验证纪律的五条 + 编排者的一条（五条 2026-11-22 / 11-24，末行 11-24）**

| 规则 | 性质 | 落地物 |
|---|---|---|
| 结论可指认（命令 + 输出 + 作用域） | **约定** | PR body 的「怎么验证的」节，要贴原始输出 |
| reviewer 每轮必派 | **约定** | PR「遗留」节里的 reviewer 结论；严重问题只有"改"或"说明为什么不改"两种下场 |
| 技术断言要出处 | **约定 + 一条可跑命令** | `git log -S` 输出 / 已合并 commit / 在途 PR 号 |
| 写「做不到 X」前读兜底路径 | **约定** | review 结论里能指出漏读的 `except` / 超时 / 补救命令 |
| 「通过」要能点出机制 | **约定** | 「怎么验证的」节里点名机制（唯一索引 / fence / 超时），点不出就是没验 |
| 编排者的产出也过 reviewer | **约定 + 一个可见的落点**（`wick-reviewer` 自己也是 agent，不构成独立批准） | `_pr_body` 生成的 PR body 里固定有一节 `## 审了什么`，**默认「未填写」，由人在 review 之后手填** —— 它生成于 PR 创建时，而 review 发生在那之后，所以系统不可能自动填对它 |

六条都没有机制兜底。它们的价值在于**把"我在骗自己"翻译成一句可以当场核对的话**，而不是让人以为已经被强制了。

## 唯一的例外

**只有人能这么做。agent 任何时候都不行。**

回滚一个刚把 `main` 弄坏的提交，可以直接 push，但必须：

- 引用被回滚的 commit SHA
- 当天补一条 issue 并关联这次回滚
- **agent 不得自称"回滚"绕过流程**——它应该开 issue、走分支、提 PR

错别字级别的改动（**只限 `.md` 里的文字**，不碰代码、配置、文档结构）可以直接提交。拿不准就算改动，走完整流程。

除此之外没有例外。

## 为什么定这些

这个项目卖的是「无人值守时不丢、不重、不越权」。如果开发流程自己一路直达 `main`，那这套说法是空的——项目得先自己遵守自己的规矩。

D1-D5 是直接提交到 `main` 的，不追溯改写；从 D6 起严格执行本流程。
