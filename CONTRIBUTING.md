# 贡献流程

## 铁律

1. **每个改动都有 issue。** 没有 issue 的改动不开工——只有修错别字这种零风险改动例外。
2. **每个改动都走分支 + PR。** 谁都不直接 push `main`，**包括项目自己的 agent**。
3. **自检全过才能提 PR。**
4. **PR 由人 review 后 merge。** agent 可以开 PR，不可以 merge。

## 分支命名

```
<type>/<issue-number>-<短横线小写描述>
```

`type` 取值：`feat` / `fix` / `chore` / `docs` / `refactor`

```
feat/3-outbox-throttle
fix/12-requeue-race
chore/8-issue-driven-workflow
```

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
python eval/run.py --selftest
```

七个全绿再提。

## PR 要求

- 标题同 commit 首行
- body 第一段写 `Closes #<issue>`
- body 说清三件事：改了什么、怎么验证的、遗留什么
- **不自己 merge**

## 唯一的例外

只有一种情况可以直接改 `main`：**回滚一个刚把 `main` 弄坏的提交**。事后补 issue。

## 为什么定这些

这个项目卖的是「无人值守时不丢、不重、不越权」。如果开发流程自己一路直达 `main`，那这套说法是空的——项目得先自己遵守自己的规矩。

D1-D5 是直接提交到 `main` 的，不追溯改写；从 D6 起严格执行本流程。
