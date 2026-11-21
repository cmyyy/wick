# 贡献流程

## 铁律

1. **每个改动都有 issue。** 没有 issue 的改动不开工。
2. **派活给 bot 之前先发 ticket。** 缺陷清单、验收标准、执行者、分支写进 issue 评论，然后再派给 dev。先派后补等于没有依据，而且事后写不进当初的判断。
3. **每个改动都走分支 + PR。** 谁都不直接 push `main`，**包括项目自己的 agent**。
4. **自检全过才能提 PR。**
5. **PR 由人 review 后 merge。** agent 可以开 PR，不可以 merge。

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
