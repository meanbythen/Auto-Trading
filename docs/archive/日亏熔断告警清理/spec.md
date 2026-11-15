# 日亏熔断告警反复删除问题：排查与修复规格

## 1. 现象

运行台反复出现同一条 INFO 日志，且只在有旧限额熔断行时出现：

```
13:01:03 | INFO | 已删掉 4 条限额已过期的日亏熔断告警，现限额 3000
13:08:17 | INFO | 已删掉 1 条限额已过期的日亏熔断告警，现限额 3000
13:08:23 | INFO | 已删掉 1 条限额已过期的日亏熔断告警，现限额 3000
```

这不是熔断本身，而是「清理过期熔断告警」的维护日志被 UI 刷新反复触发。

## 2. 已确认的事实（证据）

| 位置 | 事实 |
| --- | --- |
| `config/config.yaml` | `risk.max_daily_loss: 3000`，文件修改时间 2026-11-14 12:10:55 |
| `logs/auto_trading_2026-11-14.log` | 当天 12:03–13:08 共 19 次 prune 日志；`告警 [halt]` 与 `风控熔断` 一次都没有 |
| `logs/auto_trading_2026-11-14.log` | 全天只有 08:50:25 一次「机器人启动」；但 `data/alerts.jsonl` 里 12:16、12:22、12:42、12:43、12:44、12:58、12:59、13:08 各有一组 `pending_intent` + `reconcile` 告警 |
| `data/alerts.jsonl` | 当前已无 `halt` 行；剩余为 reconcile 38 条、strategy_health 97 条、pending_intent 82 条 |
| `data/bot_state.json` | `daily_pnl: 0.0`、`halted: false`、修改时间 08:53:34 |
| 进程 | 同时存在两个 python 进程：`.venv\Scripts\python.exe app_entry.py`（PID 28140，父进程是本机 PowerShell）与 `D:\Programs\Python\Python312\python.exe app_entry.py`（PID 8696，父进程为 28140，且只有它带主窗口），启动时间同为 13:00:57 |
| 账本 | `today_realized_pnl(load_book(root)) == 0.0`（当前） |

结论：`halt` 行是历史/旧限额遗留在文件里的，被 UI 每轮刷新删掉；同时有另一个限额与 3000 不一致的写入方在不断补写，所以删了又来。

## 3. 根因

### 根因 1：读接口带写副作用，且每轮刷新都调用两次
- `src/ops/alerts.py` 的 `last_alert()` 在被调用时先执行 `prune_stale_halt_alerts()`，删到东西就 `logger.info(...)`。
- 调用点有两处：`src/ui/dashboard.py:135`（界面刷新）和 `src/scheduler/runner.py:738`（`status_snapshot`）。
- 界面每次刷新都会同时触发这两处，所以同一次清理可能打印两条 INFO（13:08:17 与 13:08:23 就是这种成对出现）。
- `prune_stale_halt_alerts()` 会整文件读入再覆盖写回，等于每次刷新都可能改写 `data/alerts.jsonl`。

### 根因 2：运行中的风控限额不会随配置页更新
- `src/scheduler/runner.py:173` 在构造 `TradingBot` 时用当时的 `cfg` 生成 `RiskGuardian`，`max_daily_loss` 从此固定。
- 配置页保存只写 `config/config.yaml` 和 UI 变量，`worker.bot.risk.max_daily_loss` 不会变。
- 因此「配置页 3000 + 运行中机器人 1000（或更早的值）」可以长期并存：机器人继续按旧限额熔断并写 `halt` 行，界面按 3000 把它删掉。

### 根因 3：熔断告警的正文含当日盈亏，绕过了 10 分钟冷却
- `src/risk/guardian.py:61/76` 生成 `当日亏损 {daily_pnl:.2f} 已超过限额 {max_daily_loss}`。
- `src/ops/alerts.py:44` 的冷却 key 是 `code|message`，盈亏数字每变一次就是一条新告警。
- 于是同一个旧限额熔断可以反复写入 `alerts.jsonl`，每写一条，界面下一次刷新就删一条、打一条 INFO。

触发密度还受程序反复重启放大：日志显示当天十几次 `pending_intent` + `reconcile` 成组出现，每次重启都会重新执行 `set_daily_pnl()`（`runner.py:566`）并可能再写一条 `halt`。

## 4. 修复方案

### 4.1 `src/ops/alerts.py`
1. `last_alert()` 改为纯读：不再调用 `prune_stale_halt_alerts()`，只用 `is_stale_halt()` 过滤显示。
2. `prune_stale_halt_alerts()` 保留，供显式调用；删除成功日志从 `logger.info` 降为 `logger.debug`。
3. 不改告警文件格式、不改 `emit_alert` 的签名。

### 4.2 `src/risk/guardian.py`
1. 新增实例字段 `halt_alerted: bool = False`。
2. `_emit_halt()` 改为：同一段熔断只写一次告警（`halt_alerted` 为 True 直接返回；写完置 True）。
3. 新增 `set_max_daily_loss(value: float)`：
   - 更新 `max_daily_loss`；
   - 重新判断 `daily_pnl <= -abs(limit)`；
   - 若新限额下已不再超限，则清空 `halted/halt_reason` 并复位 `halt_alerted`（改了限额就按新限额重新算，符合配置页预期）；
   - 若仍然超限，则保持熔断，但不再重复写告警。
4. 新增 `clear_halt()`：清空 `halted/halt_reason/halt_alerted`，供对账确认和交易日切换复用（替换 `runner.py:634`、`order_executor.py:146` 的直接赋值）。

### 4.3 `src/scheduler/runner.py`
1. 新增 `TradingBot.sync_risk_limits(max_daily_loss: float | None = None)`，内部调用 `self.risk.set_max_daily_loss()`。
2. `status_snapshot()` 里 `_safe_last_alert(self.risk.max_daily_loss)` 保持传参（显示过滤仍按运行中限额），但不再产生写文件副作用。
3. 交易日切换（`runner.py:618-637`）改用 `self.risk.clear_halt()`；对账确认（`order_executor.py:146`）同样改用 `clear_halt()`。

### 4.4 UI 侧
1. `src/ui/run_tab.py` 的 `_on_save` 保存成功后：若 `worker.running`，调用 `worker.sync_risk_limits()`（`BotWorker` 新增同名转发方法，内部取 `self.bot.risk.set_max_daily_loss`）。
2. 程序启动时（`src/ui/app.py` `AutoTradingApp.__init__`，`load_config()` 之后）显式调用一次 `prune_stale_halt_alerts(root_dir, cfg.risk.max_daily_loss)`，把历史遗留的旧限额行清一次；此后只在「保存配置且限额变化」时再清一次。
3. `src/ui/dashboard.py:125-141`：改为纯读 `last_alert()`，不再依赖它会顺手清理。
4. `BotWorker` 新增 `sync_risk_limits()`；`run_once` 创建的临时机器人不受影响。

## 5. 行为变化（对用户可见）

- 运行日志里不再出现「已删掉 N 条限额已过期的日亏熔断告警」这类 INFO。
- 配置页改「日亏限额」并保存后，正在跑的机器人立即按新限额判断；若新限额下当日亏损已不超限，熔断解除。
- 同一段日亏熔断只会在告警文件里留一条记录，不会因为盈亏数字变化刷多条。
- 「最近告警」红字仍会按当前配置过滤旧限额的 halt 行。

## 6. 不做的事

- 不新增单实例锁。当前 `app_entry.py` 出现「.venv python 派生子进程（同一脚本）」的现象，加锁可能把正常启动挡掉；只在本文档记录风险，后续单独确认。
- 不清理 `alerts.jsonl` 里 reconcile/pending_intent/strategy_health 历史行。
- 不改 `emit_alert` 的冷却时长，不改告警文件结构。

## 7. 验证

1. `python -m pytest tests/test_alerts.py tests/test_guardian.py -q`（如 guardian 无独立测试文件则补在 `tests/test_alerts.py`）。
2. 新增用例：
   - `last_alert()` 调用前后 `alerts.jsonl` 内容与 mtime 不变；
   - 旧限额 halt 行不出现在返回值里；
   - 熔断后连续改 `daily_pnl` 只触发一次 `emit_alert("halt", ...)`；
   - `set_max_daily_loss(3000)` 在 `daily_pnl=-1500` 时解除 `halted`，在 `daily_pnl=-3500` 时保持熔断。
3. 全量：`ruff check src tests`、`mypy src`、`pytest -q`。
4. 手工：启动界面 → 保存一次 3000 → 观察运行日志 10 分钟内无 prune INFO；把限额改成更大值 → 熔断状态立即解除。

## 8. 版本

- 行为修复，按补丁号升到 `44.3.1`：`VERSION`、`README.md` 三处版本号、`CHANGELOG.md` 顶部新增一节。
