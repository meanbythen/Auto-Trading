# P0 资金与数据安全修复规格（44.6.3）

- 依据：`docs/项目审阅报告.md` 第二节 8 项 P0
- 范围：只修 P0，不顺手重构 P1/P2；保持现有业务语义
- 约束：dry_run 缺省必须 True；崩溃/对账熔断必须人工解除；下单与状态变更需异常安全；日志用 loguru；不新增无关文档

## 验收总则

1. 8 项 P0 全部落地，且每项有对应断言。
2. `python -m ruff check .` 通过。
3. `python -m mypy src` 通过。
4. 全量 `python -m pytest -q` 通过；因语义修正需同步更新的旧测试必须一并改。
5. 不提交 commit。

---

## P0-1 CTP dry_run 默认 True

**文件**
- `src/trader/ctp_trader.py`
- `src/utils/config_loader.py`

**改动**
1. `CtpTrader.connect()`、`reconcile()`、`place_order()` 三处
   `self.cfg.get("dry_run", False)` → `self.cfg.get("dry_run", True)`。
2. `load_config()` 在 `yaml.safe_load` 之后、其它分支之前执行
   `cfg.setdefault("dry_run", True)`，作为最后一道保险；配置里已显式写 False 的不覆盖。

**验收**
- 缺 `dry_run` 键时 `connect/reconcile/place_order` 均不连柜台、不发单。
- `load_config` 返回的 dict 一定含 `dry_run` 键。

---

## P0-2 进程级单实例 + run_once 复用 busy 标志

**文件**
- 新增 `src/utils/single_instance.py`
- `app_entry.py`
- `src/ui/bot_worker.py`

**改动**
1. `single_instance.py`：
   - `acquire(name="AutoTrading_SingleInstance") -> bool`
   - `release() -> None`
   - Windows 用 `ctypes.windll.kernel32.CreateMutexW` + `GetLastError()==183` 判重；
     非 Windows 用 `tempfile.gettempdir()/name.lock` 的 `os.O_CREAT|os.O_EXCL` 占位文件。
   - 同进程重复 `acquire` 返回 True；`atexit` 自动释放；提供 `release()` 供测试。
2. `app_entry.main()`：
   - `--pick-coords` 分支保持最前，**不参与**单实例拦截。
   - 之后调用 `acquire()`；失败时 GUI 用 `tkinter.messagebox.showerror` 提示、命令行写 stderr，返回 2。
3. `BotWorker.run_once()`：
   - 通过 `running` 检查后立即 `self._alive = True`，后台线程 `finally` 里 `self._alive = False`。
   - 与 `start()` 共用同一 busy 标志，连点两次只会起一套。
   - 线程创建失败时回滚 `_alive`。

**验收**
- `run_once` 执行期间 `worker.running is True`；结束后恢复 False。
- 第二次 `run_once` 在运行期间返回 False。
- 单实例 `acquire()` 第一次 True、第二次（同一进程已持有）True、释放后再取仍 True；独立进程语义由命名互斥保证。

---

## P0-3 熔断原因分类持久化，日切只清日亏

**文件**
- `src/risk/guardian.py`
- `src/utils/state_store.py`
- `src/scheduler/runner.py`
- `src/scheduler/order_executor.py`

**改动**
1. `RiskGuardian`：
   - 新增字段 `halt_kind: str = ""`，取值 `daily_loss / pending_intent / ledger / reconcile / intent_write_failed`。
   - 新增 `set_halt(kind, reason)`：置 `halted/halt_kind/halt_reason`，`logger.error`，并复用 `_emit_halt()`。
   - `_check_daily_loss()` 改调 `set_halt("daily_loss", ...)`；同类重复只更新原因，不重复告警。
   - `clear_halt(kind: str | None = None) -> bool`：
     - `kind is None` 表示人工确认，无条件清除；
     - 否则仅当 `halt_kind in ("", kind)` 时清除，返回是否成功。
   - `set_max_daily_loss()` 改调 `clear_halt("daily_loss")`，不得清掉崩溃/意图类熔断。
2. `state_store.load_state()`：
   - 空骨架增加 `"halt_kind": ""`。
   - 换日时 `daily_pnl/orders_today/open_orders_today` 照旧清零；
   - **仅当** `halt_kind == "daily_loss"` 时把 `halted=False`、`halt_kind=""`、`halt_reason=""`；
     缺失 `halt_kind` 的旧文件按“未知原因”保留熔断，交人工处理。
3. `runner.py`：
   - `_load_persisted_state()` 恢复 `self.risk.halt_kind`。
   - `_persist_state()` 写入 `halt_kind`。
   - `_rollover_trading_day()`：`self.risk.clear_halt()` → `self.risk.clear_halt("daily_loss")`；
     清零后调用 `self._check_pending_intent()` 复查意图文件，再 `_persist_state()`。
4. `order_executor.py`：
   - `_check_pending_intent()` 改用 `self.risk.set_halt("pending_intent", message)`。
   - `confirm_positions_reviewed()` 依据 `halt_kind == "pending_intent"`（或旧文案前缀）调用 `clear_halt()` 人工解除。

**测试同步**
- `tests/test_state_store.py`：原“旧熔断被清”用例补 `halt_kind="daily_loss"`；新增“crash/pending_intent 跨交易日仍 halted”断言。
- `tests/test_crash_recovery.py`：新增“新交易日复查意图文件会重新熔断”。

**验收**
- 日亏熔断跨夜自动解除；崩溃/意图熔断跨夜仍保持。
- 有未清理意图文件时，任何日切路径都不会解除熔断。

---

## P0-4 账本失败 fail-closed，平仓无价回退

**文件**
- `src/scheduler/order_executor.py`

**改动**
1. `_apply_fill_accounting()`：
   - 在 `balance = self._record_signal_account(...)` 之后增加：
     非 dry_run 且 `balance is None` → `logger.error`、`self._ledger_failed = True`、
     记录 `ledger_block_position_update` 事件，**直接 return**，不改策略/风控仓位。
   - `self.strategy.on_filled(...)` 单独 `try/except`；异常时
     `logger.exception`、`_ledger_failed = True`、记录 `fill_apply_error`，不中断后续账本逻辑。
   - `self.risk.on_order_filled(...)` 也单独 `try/except`，异常只记录，不抛出。
2. `_send_and_book()` 平仓定价（约 931-945 行）：
   - `self.kline.fetch(...)` 放独立 `try`；`df is None` 或空表时同样进入回退。
   - 取到正数收盘价才覆盖 `fill_price`。
   - 仍 `<= 0` 时回退 `self._position_for(symbol).entry_price`。
   - 仍 `<= 0` → `logger.error` + `order_no_price` 事件 + `return False`，不得清策略仓位。

**验收**
- 账本拒记时策略仓位、风控净仓保持不变，且 `_ledger_failed=True` 阻止后续开仓。
- 时钟清仓在 K 线为空/异常时使用持仓开仓价记账，不产生 `fill_price=0`。

---

## P0-5 手工单先校验价格、先记账后改内存、共用锁

**文件**
- `src/scheduler/order_executor.py`
- `src/scheduler/runner.py`

**改动**
1. `TradingBot.__init__` 新增 `self._trade_lock = threading.RLock()`。
2. `OrderExecutorMixin._execute()` 改为薄包装：
   ```python
   def _execute(self, decision):
       lock = getattr(self, "_trade_lock", None)
       with (lock if lock is not None else nullcontext()):
           return self._execute_locked(decision)
   ```
   原方法体整体迁入 `_execute_locked()`；内部递归仍调用 `self._execute()`（RLock 可重入）。
3. `manual_open()`：
   - 保持 `volume>0`、`entry_price>0`、品种存在校验。
   - 先计算 fee 并调用 `_record_signal_account`；
     非 dry_run 且 `balance is None` → `logger.error`、`_ledger_failed=True`、返回 `(None, None)`。
   - 记账成功后才 `strategy_position_open`、`risk.on_order_filled`、`_persist_state`、事件。
4. `manual_close()`：
   - 入口增加 `float(exit_price) <= 0` → 返回 `(None, None)`。
   - 先读取 pos 的 side/volume/opened_at（不清仓），计算 fee 并记账；
     非 dry_run 且 `balance is None` → `logger.error`、`_ledger_failed=True`、返回 `(None, None)`。
   - 记账成功后才 `flatten_strategy_position`、`risk.on_order_filled`、持久化。
5. `manual_open()/manual_close()` 全段包在 `self._trade_lock` 中，与扫描 `_execute` 互斥。

**验收**
- 手工单价格无效时账本与内存都不变。
- 账本写失败时手工单返回失败且内存仓位不变。
- 手工单与后台扫描不会并发改同一份策略仓位。

---

## P0-6 账本读取失败 fail-closed

**文件**
- `src/risk/dynamic.py`
- `src/scheduler/order_executor.py`
- `src/scheduler/runner.py`

**改动**
1. `dynamic.py` 新增：
   ```python
   def live_equity_and_peak_or_error(root) -> tuple[float | None, float | None, str]
   ```
   - 账本不存在：`(None, None, "")`，视为新账，不拦开仓。
   - 读取异常：`logger.error(...)`，返回 `(None, None, "读取账本权益失败: ...")`。
   - 正常：返回 `(shown, peak, "")`，保留 CTP 分支。
2. `live_equity_and_peak()` 改为其薄包装：异常/缺失返回 `(None, None)`，仅供展示。
3. `drawdown_frac(equity, peak)`：`peak <= 1e-12` 且 `equity` 为有效数字时返回 `1.0`（fail-closed）；
   任一为 `None` 仍返回 `None`。
4. `order_executor._size_open()`：
   - 改用 `live_equity_and_peak_or_error`；
   - 第三项非空 → `return 0, f"账户权益读取失败，暂停开仓：{err}"`；
   - 失败原因同步 `logger.error` 并写入 `_dynamic_note`。
5. `runner._dynamic_status_text()`、`_maybe_dynamic_cut()` 改用新函数，缺失时传 `None` 展示“回撤未算”。

**验收**
- 账本读取异常时开仓手数被压到 0，并留下 error 日志。
- 新账（文件不存在）不误判为回撤 100%。
- 展示层不再因为读取失败显示虚假的 0 回撤。

---

## P0-7 委托意图写盘失败拒绝本笔并熔断

**文件**
- `src/scheduler/order_executor.py`

**改动**
1. 新增异常 `class OrderIntentWriteError(RuntimeError)`。
2. `_write_order_intent()`：
   - 若 `self.intent_path` 已存在（上一笔未清理）→ 抛 `OrderIntentWriteError`，禁止覆盖。
   - 写临时文件/替换失败 → `logger.error` 后抛 `OrderIntentWriteError`。
3. `_dispatch()`：
   - GUI 真下单分支改为：
     ```python
     try:
         with self._order_intent(decision, vol):
             return place()
     except OrderIntentWriteError as exc:
         self.risk.set_halt("intent_write_failed", str(exc))
         self._record_event("intent_write_failed", symbol=..., side=..., error=str(exc))
         self.last_error = str(exc)
         return False
     ```
4. `_order_intent()` 不再吞异常；写失败直接向上抛。
5. 本次不做按笔独立命名（涉及意图扫描/兼容，超出 P0 最小范围）；“禁止覆盖”由第 2 条保证。

**验收**
- 意图写盘失败时不下单、策略信号标记按现有规则释放、风控熔断并留事件。
- 已有未清理意图文件时拒绝覆盖并熔断。

---

## P0-8 扫描清单 = 当前品种 ∪ 全部非空持仓

**文件**
- `src/scheduler/runner.py`
- `src/scheduler/order_executor.py`

**改动**
1. `_pick_scan_symbols()`：
   - 先放当前选中品种；
   - 再遍历 `self.strategy.positions`，把 `side in ("long","short") and volume>0` 的品种追加去重；
   - 保持当前品种在首位（`_refresh_sessions` 仍以它为交易时段基准）。
2. 新增 `_refresh_scan_symbols()`：重新计算并赋给 `self.scan_symbols`，有变化时 `logger.info`。
3. `_load_persisted_state()` 末尾调用 `_refresh_scan_symbols()`，再执行原先“非扫描持仓”告警判断（此时通常为空）。
4. `scan_once()` 开头调用 `_refresh_scan_symbols()`，覆盖启动恢复、手工开仓、外部状态变化。
5. `manual_open()` 成功后若存在 `_refresh_scan_symbols` 则调用，保证新仓立即纳入止损/清仓。

**验收**
- 当前品种之外的非空持仓也进入 `scan_symbols`，从而被扫描、止损、收盘清仓。
- 启动恢复持仓后扫描清单已包含这些品种。
- GUI 多品种硬拦仍按 `run_forever` 原逻辑执行（宁可拦下也不裸奔）。

---

## 执行顺序

1 → 2 → 3 → 4 → 5 → 6 → 7 → 8。

每完成一项：
- 跑该模块相关测试；
- 全部完成后跑 ruff、mypy、全量 pytest；
- 不提交 commit。
