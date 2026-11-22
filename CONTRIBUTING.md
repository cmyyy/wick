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

wick 产出的 **commit 前缀**确实跟类型走（`src/actions.py` 的 `_commit_prefix()`：按 issue 的 `labels` 映射 `docs`/`chore`/`refactor`/`feat`/`fix`），**PR body 也强制三节**（改了什么 / 怎么验证的 / 遗留）。工具的产出必须符合本文档，否则规则只约束人等于没有规则。

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
python src/cli.py selftest
python src/outbox.py
python eval/run.py --selftest
```

九个全绿再提。新增模块时把它的自检加进这份清单，`src/agent.py` 里的 `CHECKS` 也要同步——那是 worker 跑 agent 改动后实际执行的门槛，不同步等于新模块坏了没人管。

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
- 真正的约束是两件事：① `src/actions.py` 里**没有 merge 路径**（只有 commit / push / PR），agent 即使想 merge 也没有那条代码；② 人 review 每个 PR。
- 要机制化，需要第二个账号（bot reviewer）或 CI 门禁——都超出当前范围。

**结论**：这条规则是**约定 + 能力缺失**，不是强制。任何文档都不该把它写成"已经强制"。等真有第二个账号时再改这一节。

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
