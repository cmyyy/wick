# wick 可靠性验收（D9 / issue #6）

实测生成：2026-11-25 02:12（本机实跑，非 agent 自报）。每条数字都能用第三列的复现命令重新跑出来。

| 验收项 | 实测值 | 判定 | 复现命令 |
| --- | --- | --- | --- |
| 重复处理率 0（同一事件灌 3 次） | events=1 行（status=done，attempts=1），deliveries=1 条，实际发出=1 次 | ✅ 通过 | `python eval/reliability.py --dup` |
| 崩溃恢复（kill /F 后重跑、不重复投递） | 最终 status=done，attempts=2（1 次被 kill + 1 次重跑），deliveries=1 条，实际发出=1 次 | ✅ 通过 | `python eval/reliability.py --crash` |
| 假死恢复（不重启，3 分钟内放回 pending） | 200s 内状态一直是 running；心跳年龄在 2.0–22.1s 之间反复归零（每 30s 续一次，租约 90s），从未被回收，事件没回到 pending | ⚠️ **已知上限** | `python eval/reliability.py --zombie` |
| 闭环成功率 | 待测：n/0 ｜a 段 = waiting approval + 自检全绿（worker 实跑）；b 段 = 批准后 executed_at 非空且 detail.execution_error 为空且 deliveries 有 PR URL；分子 = a 且 b 都成功；只读分析不进样本；无自动重试（定义见 README，issue #29） | 待测 | — |
| 投递误报率 ≤20% | 待补（前 20 条人工标注明细） | 待测 | — |

## 假死恢复：为什么 3 分钟做不到（计算过程）

这条已按 DECISIONS 17 降级为**已知上限**（不在 wick 内部解决）。不是「预算比 3 分钟长一点」，是这条路径根本不存在。算一遍：

1. 续租没有上限：`worker._renew_lease` 是 `while not stop.wait(HEARTBEAT_INTERVAL)`，`HEARTBEAT_INTERVAL=30s`，只要进程活着就一直续。心跳年龄因此永远落在 [0,30)s 内。
2. 回收条件是心跳 **< now-90s**（`db.requeue_stale`）。年龄永远 < 30s，所以 `requeue_stale` 对卡死的事件永远命中 0 条 —— 实测区间见上表（由 check_zombie 采样算出，不是常量），一次都没逼近 90s。
3. 而且 `requeue_stale` 只在 worker 自己的循环里被调用（`worker.serve` 启动时与每次空闲时），而 handler 卡住时那个线程正阻塞在 `handle()` 里 —— 即使租约过期也没人来收。
4. 唯一的兜底是子进程超时：analyze 路径 `DEFAULT_TIMEOUT=900s`，fix 路径 `FIX_TIMEOUT=1500s`（外加最多 11 × `CHECK_TIMEOUT=300s` 的自检）。超时后走的是 `finish_event('failed')`，终点是 **failed**，不是 pending。

结论：**3 分钟回收 = 0 次**。这条口径要成立需要两件事，成本差一个量级：一是**进度信号**（心跳带 phase，才能区分「慢」和「死」）—— 贵；二是**机制**（handler 跑独立线程、主线程按预算 kill 子进程树并继续消费）—— 便宜，且顺带解决队头阻塞。两者都在 #26。原文曾把两者混为一谈并整体否决，这个判断在 review 时被纠正。旧的「只加续租预算」写法（`_renew_lease` 超过 `FIX_TIMEOUT + N×CHECK_TIMEOUT` 就停止续租，让行自然变 stale），那属于改 `src/`，本次按 ticket 要求只报告不改。

另外记一笔：编排者 ticket 里写的「预算 = `FIX_TIMEOUT + 4×CHECK_TIMEOUT`」在代码里不存在，证据：

```bash
$ grep -rn "FIX_TIMEOUT + 4\|watchdog\|看门狗" src/
（无输出）
```

## 口径与计算（供复核）

- 租约：`db.LEASE_SECONDS = 90`s，worker 每 30s 续租一次（`worker.HEARTBEAT_INTERVAL`）。
  崩溃后的回收延迟上界 = 最后一拍 + 租约 = **≤120s**（心跳每 30s 一拍），不是 kill 的瞬间，且前提是有人把 worker 再起来。
- 崩溃恢复：假 hermes 卡住 → `taskkill /F /T` → 心跳停在最后一拍 → 重启的 worker 在
  `serve()` 启动和每次空闲时调用 `db.requeue_stale()`，租约过期后把行放回 `pending`，
  第二次尝试用会立刻返回的假 hermes 跑完 → `done`。
  注意「deliveries 仍 1 条」有一半是**结构性保证**：`add_delivery` 在 `handle()` 返回之后才调用，被 kill 的 attempt 根本没机会写投递。fence 本身（旧 attempt 晚到被拒）由 `db.py` 的 selftest 覆盖，**本演练没有覆盖它** —— 要覆盖需慢 handler + 第二个 worker 抢租约。
- 假死恢复：见 `--zombie` 输出；当前代码没有「续租预算」，卡死的进程会一直持有租约。
- 重复灌入：`events.dedupe_key` 是 `UNIQUE` + `INSERT OR IGNORE`，三次 enqueue 只有第一次落行。

```sql
-- 任意时刻自查（把 wick.db 换成你的库）
SELECT id, source_type, source_id, status, attempts, heartbeat_at FROM events ORDER BY id;
SELECT event_id, COUNT(*) FROM deliveries GROUP BY event_id HAVING COUNT(*) > 1;  -- 空 = 没有重复投递
SELECT key, value FROM meta WHERE key='stats:stale_requeued';
```
