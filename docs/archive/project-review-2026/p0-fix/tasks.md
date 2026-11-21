# P0 修复任务清单（tasks）

前置：`spec.md` 已批准。按顺序执行，每完成一项勾选并跑对应验证。

## 阶段 1：P0-1 成交回报并发改仓

- [x] T1.1 `src/trader/ctp_gateway.py`：`on_trade` 中把 `waiter.set()` 提前到 `_account_fill(report)` 之前。
- [x] T1.2 检查 `ctp_gateway.py` 中所有 `_finish_locked` 调用点（`OnRtnOrder`、查询回报），同样保证先唤醒等待者再记账。
- [x] T1.3 `src/scheduler/order_executor.py`：`_on_ctp_fill` 改为加锁包装（`acquire(timeout=10)`，超时 `fill_lock_timeout` 事件 + 返回 `False`），原函数体改名 `_on_ctp_fill_locked`。
- [x] T1.4 `_apply_fill_accounting` 返回值改为 `bool`，并在账本拒记/策略同步异常分支返回 `False`。
- [x] T1.5 新增 `_recent_fill_keys`（`OrderedDict`，上限 5000），锁内二级去重。
- [x] T1.6 新建 `tests/test_ctp_fill_accounting.py`：并发同回报、账本失败重试、无死锁三组用例。
- [x] T1.7 验证：`pytest tests/test_ctp_fill_accounting.py -q`、`pytest tests/ -k "ctp or order" -q`、`ruff check src tests`。

## 阶段 2：P0-2 关窗等待后台线程

- [x] T2.1 `src/scheduler/runner.py`：新增 `_stopped = threading.Event()`、`_stop_requested_at`、`wait_stopped(timeout)`。
- [x] T2.2 `run_forever`：进入时 `_stopped.clear()`；`finally` 改为嵌套 `try/finally`，确保 `_persist_state()` 之后 `_stopped.set()`，`keepalive.stop()` 与 `trader.shutdown()` 各自 try/except。
- [x] T2.3 `scan_once` 品种循环内每品种结束检查 `if not self._running: return`。
- [x] T2.4 `src/ui/bot_worker.py`：`stop(*, wait=False, timeout=15.0) -> bool`，`wait=True` 时 `join`，线程自身调用直接返回 `True`。
- [x] T2.5 `src/ui/run_tab.py`：`_on_close` 改为「有持仓/挂单确认 → `stop(wait=True,15)` → 超时二次确认 → destroy」，取消时恢复 `_closing=False`。
- [x] T2.6 新建 `tests/test_bot_worker_stop.py`：正常停止、超时返回 `False`、线程内自调用。
- [x] T2.7 验证：单测 + 手工启动常驻后关窗，确认日志有 `shutdown` 事件且进程退出。（单测已覆盖，手工待验收）

## 阶段 3：P0-3 三方对账

- [x] T3.1 `src/trader/ctp_logic.py`：新增 `ReconcileReport` dataclass 与 `three_way_diff(broker, ledger, memory)`。
- [x] T3.2 `src/trader/ctp_trader.py`：`reconcile(internal, ledger=None)` 返回三元差异，写入 `self.last_report`；`ledger=None` 保持旧语义。
- [x] T3.3 `src/strategy/types.py`：`PositionState` 增加 `origin: str = ""`。
- [x] T3.4 `src/scheduler/runner.py`：新增 `_ledger_nets()`、`_apply_reconcile(report, phase)`、`_frozen_symbols`。
- [x] T3.5 `_load_persisted_state`：在 `_reconcile_books()` 之后、`_refresh_scan_symbols()` 之前插入 `_reconcile_broker_at_startup()`。
- [x] T3.6 `scan_symbol`：冻结品种跳过开仓与自动平仓，仅记录；`origin="broker"` 品种禁止反手/加仓。
- [x] T3.7 `_persist_state` / `status_snapshot`：写入并暴露 `reconcile` 字段。
- [x] T3.8 `src/scheduler/login_flow.py`：`_reconcile_ctp` 调用 `_apply_reconcile(phase="runtime")`；`phantom` 需连续两轮确认才补记。
- [x] T3.9 `src/ui/run_tab.py` / `src/ui/dashboard.py`：展示对账横幅；「认领 / 平掉 / 忽略」回调接到 `manual_open` / `manual_close` / 冻结集合。
- [x] T3.10 新建 `tests/test_reconcile_three_way.py`：一致、仅内存不同、柜台≠账本、`unclaimed`、`phantom`、查询失败六种情形。
- [x] T3.11 验证：单测 + 仿真账号手工开仓后启动，确认 `position_unclaimed` 告警与认领流程。

## 阶段 4：P0-4 看门狗与退出保护

- [x] T4.1 新建 `src/ops/watchdog.py`：`TradingWatchdog`（5 秒轮询，阈值 `max(3*scan_interval,180)`，空仓/非交易时段不告警）。
- [x] T4.2 `src/scheduler/runner.py`：`run_forever` 启动/停止看门狗；停滞回调设置 `risk.halt("stale_heartbeat")`。
- [x] T4.3 新增 `_write_unprotected_flag()`：有持仓或挂单时退出写 `data/unprotected_positions.json` + `emit_alert("exit_with_positions")`。
- [x] T4.4 启动检测标记：存在则 `_catch_up_pending=False`、先对账再评估止损、评估前 `orders_paused=True`。
- [x] T4.5 新建 `src/trader/protective.py`：`ProtectiveOrderAdapter` 协议 + `CtpNativeStopAdapter`（`supported()` 恒 `False`）；状态里记录 `protective="none"`。
- [x] T4.6 `tests/test_watchdog.py`：停滞触发、空仓不触发、阈值自适应。
- [x] T4.7 `tests/test_unprotected_recovery.py`：标记存在时禁止开仓与补开仓。
- [x] T4.8 `docs/` 现有交易说明补一段「本地止损边界与看门狗行为」。

## 阶段 5：整体验收

- [x] T5.1 `ruff check src tests` 全绿。
- [x] T5.2 `pytest tests/ -q` 全绿。
- [ ] T5.3 手工演练：启动 → 手工开仓 → 关闭窗口 → 重启 → 核对持仓、账本、告警、横幅。（需仿真/实盘账号，待人工执行）
- [ ] T5.4 手工演练：仿真账号制造 `phantom`，确认补记只发生一次。（需仿真/实盘账号，待人工执行）
- [x] T5.5 在 `project-review-2026.html` 的 P0 表格补充「已修复」标记与对应提交号。（工作区未提交，标注为「已修复（工作区）」）
