# 任务清单：日亏熔断告警反复删除

> 对应规格：[spec.md](./spec.md)。按顺序执行，每完成一项勾选。

- [x] T1 `src/ops/alerts.py`：`last_alert()` 去掉 `prune_stale_halt_alerts()` 调用，改为纯读（仅用 `is_stale_halt` 过滤）；`prune_stale_halt_alerts()` 的删除日志由 INFO 降为 DEBUG。
- [x] T2 `src/risk/guardian.py`：新增 `halt_alerted` 字段；`_emit_halt()` 同一段熔断只写一次；新增 `set_max_daily_loss()`（更新限额并按新限额重新判断，未超限则解除熔断并复位标记）；新增 `clear_halt()`。
- [x] T3 `src/scheduler/runner.py`：新增 `TradingBot.sync_risk_limits()`；交易日切换改用 `self.risk.clear_halt()`；`status_snapshot()` 读取告警保持纯读。
- [x] T4 `src/scheduler/order_executor.py`：对账确认处的 `self.risk.halted = False` 改用 `risk.clear_halt()`，同时清掉 `halt_reason` 与告警标记。
- [x] T5 `src/ui/bot_worker.py`：新增 `sync_risk_limits(max_daily_loss=None)`，转发到运行中的 `bot.risk.set_max_daily_loss()`。
- [x] T6 `src/ui/config_support.py`：`_on_save` 保存成功后，若常驻在跑，调用 `bot.sync_risk_limits()`；限额变化时再显式清理一次旧告警。
- [x] T7 `src/ui/app.py`：`AutoTradingApp.__init__` 在 `load_config()` 后显式调用一次 `prune_stale_halt_alerts(self.root_dir, 当前限额)`，异常只打 debug。
- [x] T8 `src/ui/dashboard.py`：刷新分支改为纯读 `last_alert()`，保留按当前限额过滤显示。
- [x] T9 `tests/test_alerts.py`：补 4 个用例——`last_alert` 不写文件、旧限额行不返回、熔断只告警一次、`set_max_daily_loss` 解除/保持熔断。
- [x] T10 全量验证：`ruff check src tests`、`mypy src`、`pytest -q` 全部通过。
- [x] T11 版本与文档：`VERSION` 升 `44.3.1`；`README.md` 三处版本号；`CHANGELOG.md` 顶部新增「44.3.1 修复：日亏熔断告警反复删除」。
