# P0 修复验收清单（checklist）

逐条核对代码与测试；通过后勾选。任一项不通过，在 `tasks.md` 追加修复任务。

## P0-1 成交回报并发改仓

- [x] `ctp_gateway.on_trade` / `on_order_status` / `on_reject` 均为「先 `waiter.set()`，再 `_account_fill(report)`」
- [x] `order_executor._on_ctp_fill` 内部 `_trade_lock.acquire(timeout=10)`，超时记 `fill_lock_timeout` 并返回 `False`
- [x] 原回调逻辑完整保留在 `_on_ctp_fill_locked`，无删改
- [x] `_apply_fill_accounting` 返回 `bool`：账本拒记、策略同步异常返回 `False`；dry_run、去重命中返回 `True`
- [x] `_recent_fill_keys` 为惰性初始化的 `OrderedDict`，上限 5000，成功入账后写入
- [x] `tests/test_ctp_fill_accounting.py` 覆盖并发同回报、账本失败重试、锁超时、网关先唤醒、无死锁

## P0-2 关窗等待后台线程

- [x] `TradingBot` 有 `_stopped` 事件、`_stop_requested_at`、`wait_stopped(timeout)`
- [x] `run_forever` 进入时 `clear()`，`finally` 中 `_persist_state()` 之后必定 `_stopped.set()` 并记 `shutdown`
- [x] `run_forever` 的 `keepalive.stop()` 与 `trader.shutdown()` 各自独立 try/except
- [x] `scan_once` 每个品种结束后检查 `_running` 并提前返回
- [x] `BotWorker.stop(*, wait, timeout) -> bool`，`wait=True` 时 join，线程内自调用返回 `True`
- [x] `run_tab._on_close`：持仓/挂单确认 → `stop(wait=True, 15)` → 超时二次确认（默认否） → 取消时 `_closing=False` 且不销毁窗口
- [x] `tests/test_bot_worker_stop.py` 覆盖正常等待、超时、自调用、无 bot、`run_forever` 集成

## P0-3 三方对账

- [x] `ctp_logic` 提供 `ReconcileReport`（含 `queried/ok/diffs/unclaimed/phantom/extra`）与 `three_way_diff`
- [x] `CtpTrader.reconcile(internal, ledger=None)` 兼容旧调用，写入 `last_report`；查询失败时 `queried=False`
- [x] `PositionState.origin` 存在且 `to_dict`/`from_dict` 往返一致
- [x] `runner` 有 `_ledger_nets/_apply_reconcile/_frozen_symbols`，启动对账插在 `_reconcile_books` 之后
- [x] `scan_symbol` 对冻结品种直接返回；`origin="broker"` 的持仓屏蔽策略开平/反手，只保留通用止损
- [x] `_persist_state` 与 `status_snapshot` 暴露 `reconcile`、`frozen_symbols`
- [x] `login_flow` 的启动同步与运行时对账分别以 `phase="startup"/"runtime"` 调用 `_apply_reconcile`
- [x] 幽灵仓在 runtime 需连续两轮确认，startup 立即补记且只补一次
- [x] 界面有对账横幅与「处理对账」入口，可认领/平掉/忽略
- [x] `tests/test_reconcile_three_way.py` 覆盖一致、仅内存不同、柜台≠账本、未认领、幽灵、查询失败

## P0-4 看门狗与退出保护

- [x] `src/ops/watchdog.py` 每 5 秒轮询，空仓/非交易时段不告警，单次停滞只回调一次
- [x] 停滞阈值等于 `max(3 × 扫描间隔, 180s)`
- [x] 停滞时暂停报单、`risk.set_halt("stale_heartbeat", ...)`、写退出保护标记并告警
- [x] `run_forever` 启停看门狗，退出顺序为「停看门狗 → 写保护标记 → 关通道 → 落盘」
- [x] 带仓退出写 `data/unprotected_positions.json`，空仓退出会清除标记
- [x] 启动检测到标记时 `_orders_paused=True` 且禁止补开仓；对账成功后自动解除并删除标记
- [x] `src/trader/protective.py` 提供适配器协议，`CtpNativeStopAdapter.supported()` 恒 `False`，状态记录 `protective`
- [x] `tests/test_watchdog.py`、`tests/test_unprotected_recovery.py` 覆盖规定场景
- [x] 交易说明文档补充「本地止损的边界与看门狗」

## 全局约束

- [x] 未修改风控阈值、手数算法、策略语义（`src/risk/`、`src/sizing/`、`src/strategy/` 无行为性改动）
- [x] `ruff check src tests` 通过
- [x] `pytest tests/ -q` 全绿
- [x] `project-review-2026.html` 的 P0 表格标注已修复
