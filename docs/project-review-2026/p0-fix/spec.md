# P0 修复规格说明（Spec）

版本：v1（待审批）
范围：`docs/project-review-2026/project-review-2026.html` 中确认的 4 个 P0
目标：在不改变现有交易语义的前提下，消除「账实分叉 / 并发改仓 / 带仓退出 / 无柜台保护」四类严重风险

---

## 0. 已核实的现状证据

| 编号 | 问题 | 关键证据 |
|---|---|---|
| P0-1 | CTP 成交回报在回调线程里改仓，未持交易锁 | [order_executor.py#L1382-L1434](file:///d:/Auto-Trading/src/scheduler/order_executor.py#L1382-L1434) 的 `_on_ctp_fill` 直接调用 [order_executor.py#L495-L709](file:///d:/Auto-Trading/src/scheduler/order_executor.py#L495-L709) `_apply_fill_accounting`；而扫描发单在 [order_executor.py#L960-L964](file:///d:/Auto-Trading/src/scheduler/order_executor.py#L960-L964) 持 `_trade_lock`，手工平/开仓在 [order_executor.py#L260-L262](file:///d:/Auto-Trading/src/scheduler/order_executor.py#L260-L262) / [L376-L378](file:///d:/Auto-Trading/src/scheduler/order_executor.py#L376-L378) 也持锁。回调来源见 [ctp_gateway.py#L1030-L1048](file:///d:/Auto-Trading/src/trader/ctp_gateway.py#L1030-L1048) `_account_fill`（明确写在网关锁之外）与 [ctp_gateway.py#L671-L692](file:///d:/Auto-Trading/src/trader/ctp_gateway.py#L671-L692) `on_trade`。 |
| P0-2 | 关窗只发停止信号不等待，daemon 线程被进程强杀 | [run_tab.py#L1269-L1281](file:///d:/Auto-Trading/src/ui/run_tab.py#L1269-L1281) `_on_close`；[bot_worker.py#L58-L64](file:///d:/Auto-Trading/src/ui/bot_worker.py#L58-L64) daemon 线程 + `stop()` 只转发；[runner.py#L1183-L1186](file:///d:/Auto-Trading/src/scheduler/runner.py#L1183-L1186) `stop()` 只置 `_running=False`；清理在 [runner.py#L1290-L1297](file:///d:/Auto-Trading/src/scheduler/runner.py#L1290-L1297) 的 `finally`。 |
| P0-3 | 启动对账只以信号账户账本为事实源，柜台实际持仓无法认领 | [runner.py#L515-L616](file:///d:/Auto-Trading/src/scheduler/runner.py#L515-L616) `_reconcile_books` 用 `open_position_rows(book)` 覆盖内存；[ctp_trader.py#L97-L138](file:///d:/Auto-Trading/src/trader/ctp_trader.py#L97-L138) `reconcile` 只做「一致/不一致」二分；[ctp_gateway.py#L676-L680](file:///d:/Auto-Trading/src/trader/ctp_gateway.py#L676-L680) foreign 成交只展示不入账；[ctp_trader.py#L182-L191](file:///d:/Auto-Trading/src/trader/ctp_trader.py#L182-L191) `_sync_signal_book` 只同步资金。不一致时的唯一动作是 `blocks_new_opens()`。 |
| P0-4 | 止损全部在本地内存计算，进程消失即失去保护 | [runner.py#L928-L953](file:///d:/Auto-Trading/src/scheduler/runner.py#L928-L953) 通用止损；`src/trader/` 全目录无条件单/预埋单实现；`heartbeat_at` 只写状态供界面显示（[runner.py#L1150-L1152](file:///d:/Auto-Trading/src/scheduler/runner.py#L1150-L1152)），无外部看门狗。 |

---

## 1. 总体原则

1. **单一写者**：`strategy.positions` / `risk.positions` / `bot_state.json` / 信号账户账本，只允许在 `_trade_lock` 内被修改。
2. **单一事实源分层**：柜台持仓是「实际风险」事实源，信号账户账本是「资金与成交」事实源，内存仓位是「运行态缓存」。三者不一致必须显式修复，不允许静默覆盖。
3. **不丢成交**：回调入账失败必须可重试（返回 `False` 让网关不推进 `accounted_volume`，由补记/对账兜底），不允许「先确认再丢」。
4. **不引入新进程依赖**：P0-4 先做看门狗 + 快速恢复；柜台条件单作为可选适配器，不阻塞本次修复。
5. **可回滚**：每个 P0 独立提交，均带开关或可整体 revert。

---

## 2. P0-1 成交回报并发改仓

### 2.1 根因

- `_on_ctp_fill` 是唯一在锁外写 `strategy/risk/state` 的路径，与扫描线程、手工平仓线程构成三方竞争。
- 去重依赖「读账本 → 判断 fill_key → 写账本」三步，两步之间无互斥，同一 `fill_key` 可被两个回调线程同时判为「未记过」。
- 网关在 `on_trade` 中先 `_account_fill(report)`，后 `waiter.set()`；若入账直接去抢 `_trade_lock`，而主线程正持锁等待该 `waiter`，会形成死锁。

### 2.2 方案

**A. 网关：先唤醒等待者，再记账**

`ctp_gateway.on_trade`（[#L671-L692](file:///d:/Auto-Trading/src/trader/ctp_gateway.py#L671-L692)）调整为：

```python
with self._lock:
    ...
    report, waiter = self._finish_locked(order)
if waiter is not None:
    waiter.set()            # 先放行主线程，避免它持锁等回报
self._account_fill(report)
```

同一调整适用于所有调用 `_finish_locked` 的终态路径（`OnRtnOrder`、查询回报）。

**B. 执行器：入账统一走锁**

`order_executor.py` 新增：

```python
FILL_LOCK_TIMEOUT = 10.0

def _on_ctp_fill(self, report: dict[str, Any]) -> bool:
    """CTP 回调线程入口：拿交易锁后入账，超时返回 False 交回补记路径。"""
    lock = getattr(self, "_trade_lock", None)
    if lock is None:
        return self._on_ctp_fill_locked(report)
    if not lock.acquire(timeout=self.FILL_LOCK_TIMEOUT):
        logger.error("成交入账等待交易锁超时，交回补记/对账 | {}", report.get("order_ref"))
        self._record_event("fill_lock_timeout", order_ref=str(report.get("order_ref") or ""))
        return False
    try:
        return self._on_ctp_fill_locked(report)
    finally:
        lock.release()
```

原 `_on_ctp_fill` 函数体整体改名为 `_on_ctp_fill_locked`，逻辑不变，仅在末尾返回入账结果。

**C. 入账结果可判定**

`_apply_fill_accounting` 返回值由 `None` 改为 `bool`：

- 账本/策略/风控全部同步成功 → `True`
- `dry_run` → `True`
- 命中 `fill_key` 去重 → `True`
- 账本拒记（`balance is None` 且非 dry_run）→ `False`
- 策略同步异常（`_ledger_failed`）→ `False`

`_on_ctp_fill_locked` 返回该值；返回 `False` 时网关不推进 `accounted_volume`，主线程 `_finish_ctp_order` 的 `_book_ctp_leftover`（[#L1436-L1487](file:///d:/Auto-Trading/src/scheduler/order_executor.py#L1436-L1487)）会在同一轮补记。

**D. 进程内二级去重**

在锁内维护 `self._recent_fill_keys: OrderedDict[str, float]`（上限 5000，超限弹出最旧）：

```python
if fill_key and fill_key in self._recent_fill_keys:
    return True
ok = self._apply_fill_accounting(...)
if ok and fill_key:
    self._recent_fill_keys[fill_key] = time.monotonic()
```

作用：账本读写在锁内虽已串行，但账本落盘有延迟/失败窗口，二级去重把「同进程重复回报」彻底挡住。

### 2.3 改动清单

| 文件 | 改动 |
|---|---|
| `src/trader/ctp_gateway.py` | `on_trade` 及所有终态回调：`waiter.set()` 提前到 `_account_fill` 之前 |
| `src/scheduler/order_executor.py` | 新增 `_on_ctp_fill` 包装 + `_on_ctp_fill_locked`；`_apply_fill_accounting` 返回 `bool`；新增 `_recent_fill_keys` 及 `FILL_LOCK_TIMEOUT` |
| `tests/test_ctp_fill_accounting.py`（新建） | 并发/去重/账本失败重试用例 |

### 2.4 验证

1. 新建测试：两个线程同时对同一 `report` 调用 `_on_ctp_fill`，断言账本只新增 1 条成交、内存仓位只加 1 次。
2. 新建测试：`_record_signal_account` 抛异常时 `_on_ctp_fill` 返回 `False`，且下一次同 `report` 调用仍能成功入账。
3. 新建测试：假网关 + 假 trader，`place_order` 在 `waiter` 上等待，回调线程调用 `_on_ctp_fill`，断言 5 秒内两者都返回（无死锁）。
4. 回归：`pytest tests/ -k "ctp or order"`，`ruff check src tests`。

### 2.5 风险与回滚

- 风险：锁等待拉长回调耗时。已用 `acquire(timeout=10)` 兜底，超时走补记路径，不丢单。
- 回滚：仅 revert `order_executor.py` 中包装函数即可回到原行为；网关的 `waiter.set()` 提前可独立保留（无副作用）。

---

## 3. P0-2 关窗不等待后台线程

### 3.1 根因

`_on_close` 调 `worker.stop()` 后立即 `destroy()`；`stop()` 只是置标志，主线程仍在 `scan_once` / `time.sleep` / CTP 等待中，进程退出时 daemon 线程被强杀，`finally` 里的 `trader.shutdown()`、`_persist_state()` 可能未执行。

### 3.2 方案

**A. TradingBot 增加可等待停止**

```python
# __init__
self._stopped = threading.Event()
self._stop_requested_at: float | None = None

def stop(self) -> None:
    if self._stop_requested_at is None:
        self._stop_requested_at = time.monotonic()
    self._running = False
    logger.info("已请求停止机器人主循环")

def wait_stopped(self, timeout: float = 15.0) -> bool:
    """等待主循环真正结束并完成落盘。"""
    return self._stopped.wait(timeout)
```

`run_forever`：进入时 `self._stopped.clear()`；`finally` 改为嵌套 `try/finally`，保证 `_stopped.set()` 一定执行：

```python
finally:
    self._running = False
    try:
        keepalive.stop()
    except Exception:
        logger.exception("停止防休眠失败")
    try:
        if hasattr(self.trader, "shutdown"):
            self.trader.shutdown()
    except Exception:
        logger.exception("关闭交易通道失败")
    try:
        self._persist_state()
    finally:
        self._stopped.set()
        logger.info("机器人已停止")
        self._record_event("shutdown")
```

**B. BotWorker 支持 join**

```python
def stop(self, *, wait: bool = False, timeout: float = 15.0) -> bool:
    """请求停止；wait=True 时等待线程退出，返回是否已退出。"""
    if self.bot is None:
        return True
    self.bot.stop()
    if not wait:
        return not self.running
    thread = self._thread
    if thread is None or thread is threading.current_thread():
        return True
    thread.join(timeout)
    stopped = not thread.is_alive()
    if not stopped:
        logger.error("后台线程 {} 秒内未退出", timeout)
    return stopped
```

**C. 关窗流程**

`run_tab._on_close` 改为：

1. 若 `worker.running`：
   - 读取 `status_snapshot()`，有持仓或未完成委托时弹确认框，文案包含品种/手数与「关闭后本程序不再执行止损」。
   - 用户取消 → `self._closing = False`，直接返回（不 destroy）。
2. `ok = worker.stop(wait=True, timeout=15)`。
3. `ok is False` → 二次确认「后台未在 15 秒内退出，强制关闭可能导致成交未记账或状态未落盘」，默认「否」；用户坚持才 `destroy()`，并写 `force_close` 事件 + `emit_alert`。
4. `ok is True` → `destroy()`。

**D. 缩短停止延迟**

- `scan_once` 的品种循环内，每个品种结束后检查 `if not self._running: return`。
- `run_forever` 的 1 秒分段 sleep 前已有 `_running` 检查，保留。
- CTP 报单等待本身有超时，不额外改动。

### 3.3 改动清单

| 文件 | 改动 |
|---|---|
| `src/scheduler/runner.py` | `_stopped` / `_stop_requested_at` / `wait_stopped`；`run_forever` 的 `finally` 加固；`scan_once` 循环内停止检查 |
| `src/ui/bot_worker.py` | `stop(*, wait, timeout) -> bool` |
| `src/ui/run_tab.py` | `_on_close` 确认 + join + 超时二次确认 + 告警事件 |
| `tests/test_bot_worker_stop.py`（新建） | 停止等待/超时返回/线程内调用自身 |

### 3.4 验证

1. 单测：`bot.run_forever` 在假 trader 上运行，`stop()` 后 `wait_stopped(5)` 为 `True`，且假 trader 的 `shutdown` 被调用、`bot_state.json` 已更新。
2. 单测：`BotWorker.stop(wait=True)` 在 2 秒内返回 `True`；人为让 `run_forever` 卡 30 秒时返回 `False`。
3. 手工：启动常驻 → 关闭窗口 → 观察日志出现 `shutdown` 事件且进程退出，无「daemon 线程被强杀」痕迹。

### 3.5 风险与回滚

- 风险：关窗多等最多 15 秒。属于可接受代价，且提供强制退出兜底。
- 回滚：`stop(wait=False)` 保持旧行为，UI 侧改回一行即可。

---

## 4. P0-3 启动/运行时三方对账

### 4.1 根因

- 启动只做「账本 → 内存」单向覆盖，柜台持仓未参与决策。
- `CtpTrader.reconcile` 只产出「一致/不一致」，不一致仅拦开仓，不修复内存、不提供认领入口、不影响止损与清仓判断。
- foreign 成交（他人/手工单）被丢弃，导致柜台有仓、账本无仓的长期分叉。

### 4.2 方案

**A. 统一对账报告对象**

在 `src/trader/ctp_logic.py` 新增：

```python
@dataclass(frozen=True)
class ReconcileReport:
    at: str                        # ISO 时间
    queried: bool                  # 柜台查询是否成功（False 时禁止据此修复）
    broker: dict[str, int]         # 柜台净仓
    ledger: dict[str, int]         # 信号账户账本净仓
    memory: dict[str, int]         # 程序内存净仓
    funds: float | None            # 柜台可用资金
    ok: bool                       # 三方一致
    diffs: list[str]               # 人类可读差异
    unclaimed: list[str]           # 柜台有、账本无
    phantom: list[str]             # 账本有、柜台无
    extra: list[str]               # 账本无、柜台无、内存有（脏内存）
```

`CtpTrader` 增加：

```python
self.last_report: ReconcileReport | None = None

def reconcile(
    self,
    internal: dict[str, int],
    ledger: dict[str, int] | None = None,
) -> tuple[bool, str]:
```

`ledger=None` 时退化为现有二分逻辑（保持所有既有调用可用）。查询失败、`dry_run`、未登录时 `queried=False`，仍返回 `False`，但调用方不得据 `broker` 修复内存。

**B. 启动顺序调整**

`_load_persisted_state` 改为：

1. 恢复 `bot_state.json` 到内存（保持现状）。
2. `_reconcile_books()`：仅做「账本 vs 内存」，**账本无成交记录时跳过**（保持现状）。
3. 新增 `_reconcile_broker_at_startup()`：仅 `executor == "ctp"` 且非 `dry_run` 时执行；调用 `trader.reconcile(self._internal_nets(), ledger=self._ledger_nets())`；拿到 `last_report` 后调用 `_apply_reconcile(report, phase="startup")`。
4. 之后才 `_refresh_scan_symbols()`。

**C. 修复策略表（`_apply_reconcile`）**

| 情形 | 内存处理 | 开仓 | 保护动作 | 提示 |
|---|---|---|---|---|
| 三方一致 | 不动 | 允许 | 正常 | 无 |
| 柜台 = 账本，内存不同 | 按账本重建内存 | 允许 | 正常 | warning 事件 |
| 柜台 ≠ 账本（`queried=True`） | 该品种按柜台净仓重建内存，`entry_price` 取柜台开仓均价（不可得则用最新价），`initial_stop=0`，`origin="broker"` | 禁止 | 仅执行通用止损；**不做反手/加仓** | `emit_alert("reconcile_mismatch")` + UI 横幅 |
| 柜台有、账本无（`unclaimed`） | 按柜台建仓，`origin="broker"` | 禁止该品种 | 默认「冻结」：不自动止损、不清仓 | `emit_alert("position_unclaimed")`，UI 提供「认领 / 平掉 / 忽略」 |
| 账本有、柜台无（`phantom`） | 内存置空，并补记一笔平仓（价格取最新价，原因 `对账确认柜台无持仓`） | 允许 | 正常 | `emit_alert("position_phantom")` |
| `queried=False` | 完全不动 | 禁止 | 维持上一轮 | `emit_alert("reconcile_unavailable")` |

- 「冻结」通过 `PositionState` 上新增 `origin` 字段与 `self._frozen_symbols: set[str]` 实现；`scan_symbol` 中对冻结品种跳过开仓与自动平仓，仅记录。
- 认领入口复用现有 `manual_open`（[order_executor.py#L365-L490](file:///d:/Auto-Trading/src/scheduler/order_executor.py#L365-L490)），认领后清除冻结标记。

**D. 运行时对账复用同一报告**

`login_flow._reconcile_ctp`（[#L112-L126](file:///d:/Auto-Trading/src/scheduler/login_flow.py#L112-L126)）在拿到 `trader.last_report` 后同样调用 `_apply_reconcile(report, phase="runtime")`，但：

- 运行时不自动补记 `phantom` 平仓（避免把「查询瞬时缺失」当成真平仓），只标记 + 告警；
- 只有在连续 2 轮 `queried=True` 且 `phantom` 相同的情况下才允许补记。

**E. 状态可见**

`bot_state.json` 新增：

```json
"reconcile": {"at": "...", "ok": false, "unclaimed": ["rb2601"], "phantom": [], "notes": ["..."]}
```

运行台与仪表盘读取该字段显示横幅，不再只靠日志。

### 4.3 改动清单

| 文件 | 改动 |
|---|---|
| `src/trader/ctp_logic.py` | 新增 `ReconcileReport`；新增三方比对函数 `three_way_diff()` |
| `src/trader/ctp_trader.py` | `reconcile(internal, ledger=None)`、`last_report`；`_sync_signal_book` 不改 |
| `src/scheduler/runner.py` | 新增 `_ledger_nets()`、`_reconcile_broker_at_startup()`、`_apply_reconcile()`、`_frozen_symbols`；`_load_persisted_state` 调整顺序；`scan_symbol` 冻结分支；`_persist_state` 写入 `reconcile` |
| `src/scheduler/login_flow.py` | `_reconcile_ctp` 调用 `_apply_reconcile(phase="runtime")` |
| `src/strategy/types.py` | `PositionState` 增加 `origin: str = ""`（默认空，不影响既有构造） |
| `src/ui/run_tab.py` / `src/ui/dashboard.py` | 读取 `reconcile` 字段显示横幅与按钮回调（认领/平掉/忽略） |
| `tests/test_reconcile_three_way.py`（新建） | 覆盖上表 6 种情形 |

### 4.4 验证

1. 单测：构造假网关持仓 `{rb2601: +2}`、账本空、内存空，断言 `unclaimed=["rb2601"]`、内存建仓、`blocks_new_opens()==True`、冻结集合包含该品种。
2. 单测：柜台空、账本 +2、内存 +2，`queried=True`，断言内存置空且补记一笔平仓。
3. 单测：`gateway.refresh()` 失败，断言内存完全未变。
4. 单测：连续两轮 runtime 对账均报 `phantom` 才补记，单轮不补记。
5. 手工：仿真账号上手工开一手 → 启动程序 → 观察 `position_unclaimed` 告警与横幅，点击「认领」后开仓限制解除。

### 4.5 风险与回滚

- 风险：对账误判可能导致错误平账。已用 `queried` 门闩 + 运行时两轮确认 + `phantom` 补记使用最新价并单独事件记录来控制。
- 回滚：`_apply_reconcile` 入口加配置开关 `trade.ctp.reconcile_repair`（默认 `true`），置 `false` 即回到「只拦开仓」的旧行为。

---

## 5. P0-4 本地止损无柜台兜底

### 5.1 根因

止损、清仓、交割闸门都依赖进程存活；进程退出/断网/卡死时没有任何机制在柜台侧保护持仓，也没有外部看门狗发现「进程还在但主循环已停」。

### 5.2 方案（分两步，第一步必须做）

**A. 看门狗与心跳（必做）**

新增 `src/ops/watchdog.py`：

```python
class TradingWatchdog:
    """独立线程：主循环心跳停滞时告警，并把持仓写成未保护标记。"""

    def __init__(self, root, get_snapshot, stale_after_sec=180.0, on_stale=None): ...

    def start(self) -> None: ...
    def stop(self) -> None: ...

    def _loop(self) -> None:
        # 每 5 秒检查一次
        # 非交易时段 / 空仓：只更新标记，不告警
        # 交易时段且 now - last_scan_at > stale_after_sec：
        #   -> emit_alert("heartbeat_stale", level="error")
        #   -> 写 data/unprotected_positions.json
        #   -> 回调 on_stale()（runner 内设置 risk.halt("stale_heartbeat")，停止开仓）
```

- `TradingBot.run_forever` 在 `keepalive.start()` 之后启动看门狗，`finally` 中停止。
- `last_scan_at` 已有（[runner.py#L1150-L1152](file:///d:/Auto-Trading/src/scheduler/runner.py#L1150-L1152)），直接复用。
- 阈值取 `max(3 * scan_interval, 180)` 秒，避免长周期策略误报。

**B. 退出保护（必做）**

- `stop()` / 关窗流程中，若存在持仓或未完成委托：
  - `_persist_state()` 必须成功才允许正常退出；
  - 写 `data/unprotected_positions.json`（品种、方向、手数、最近止损价、时间）；
  - `emit_alert("exit_with_positions")`。
- 启动时检测该标记：若存在且柜台仍有对应持仓 → 在完成首轮对账与止损评估前**禁止开新仓**，并在界面提示「上次退出时仍有持仓」。

**C. 柜台侧保护单适配器（可选，第二步）**

新增 `src/trader/protective.py`：

```python
class ProtectiveOrderAdapter(Protocol):
    def supported(self) -> bool: ...
    def place_stop(self, symbol: str, side: str, trigger: float, volume: int) -> str | None: ...
    def cancel(self, stop_id: str) -> bool: ...
    def list_stops(self) -> list[dict]: ...
```

- `CtpNativeStopAdapter`：标准 CTP 无原生条件单，`supported()` 返回 `False`；本次只落地接口与调用点，不实现假功能。
- `Runner` 在每次 `_persist_state` 后调用 `_sync_protective_orders()`：若适配器不支持则跳过并只在状态里记录 `protective="none"`。
- 若未来接入券商条件单/条件单界面自动化，实现同一接口即可，不改交易主链路。

**D. 快速恢复（必做）**

启动时若 `unprotected_positions.json` 存在：

1. 跳过回溯补开仓（`_catch_up_pending = False`）；
2. 先做对账（P0-3），再对每个持仓立即跑一次通用止损评估；
3. 评估完成前 `orders_paused = True`，完成后恢复。

### 5.3 改动清单

| 文件 | 改动 |
|---|---|
| `src/ops/watchdog.py`（新建） | 看门狗线程 |
| `src/trader/protective.py`（新建） | 保护单适配器协议 + CTP 空实现 |
| `src/scheduler/runner.py` | 启动/停止看门狗；`_write_unprotected_flag()`；启动检测标记；恢复流程 |
| `src/ui/run_tab.py` | 退出前持仓确认文案复用 P0-2 流程；启动横幅提示 |
| `tests/test_watchdog.py`（新建） | 心跳停滞触发告警与标记；空仓不告警 |
| `tests/test_unprotected_recovery.py`（新建） | 标记存在时禁止开仓、禁止补开仓 |

### 5.4 验证

1. 单测：把 `last_scan_at` 人为改到 10 分钟前，`stale_after_sec=60`，断言 `emit_alert` 被调用且标记文件生成。
2. 单测：非交易时段或空仓时，断言不产生告警。
3. 手工：常驻运行中 `kill -9` 模拟崩溃，重启后观察「上次退出仍有持仓」提示与首轮止损评估日志。
4. 文档：在 `docs/` 现有交易说明中补一段「本地止损的边界与看门狗行为」，不新建独立文档。

### 5.5 风险与回滚

- 风险：看门狗误报（长周期策略扫描慢）。阈值按扫描间隔自适应，且误报只告警不自动平仓。
- 回滚：`runtime.watchdog_enabled=false` 关闭；`unprotected` 标记不影响交易，仅提示。

---

## 6. 实施顺序与依赖

```
P0-2（可等待停止）  ──┐
                      ├──> P0-4-B 退出保护（依赖 stop 流程）
P0-1（成交锁）      ──┘

P0-3（三方对账） ──> P0-4-D 快速恢复（依赖对账报告）
```

建议提交粒度：

1. `fix(order): 成交回报在交易锁内入账，网关先唤醒等待者`
2. `fix(runtime): 停止常驻时等待线程退出并完成落盘`
3. `feat(reconcile): 启动与运行时三方对账，支持未认领持仓`
4. `feat(ops): 心跳看门狗与未保护持仓标记`

---

## 7. 验收标准

| 编号 | 验收项 | 判定 |
|---|---|---|
| A1 | 并发同一成交回报 | 账本仅 1 条、内存仅加 1 次、无死锁 |
| A2 | 账本写入失败 | 回报返回 `False`，下一轮补记成功，不产生幽灵仓 |
| A3 | 关窗 | 后台线程在 15 秒内退出，`shutdown` 事件与 `bot_state.json` 均落盘 |
| A4 | 关窗超时 | 明确二次确认，强制退出写 `force_close` 事件与告警 |
| A5 | 柜台有仓、账本无 | 生成 `unclaimed`，禁止开仓，界面可认领，不自动平仓 |
| A6 | 账本有仓、柜台无 | 补记平仓并告警，不重复补记 |
| A7 | 柜台查询失败 | 内存零改动，开仓被拦 |
| A8 | 主循环停滞 | 3 倍扫描间隔内产生 `heartbeat_stale` 告警并写未保护标记 |
| A9 | 崩溃后重启 | 提示未保护持仓、禁止补开仓、先评估止损 |
| A10 | 回归 | `ruff check src tests` 全绿；`pytest tests/` 全绿 |

---

## 8. 本次不做

- 不实现券商条件单界面自动化（无稳定接口，风险高于收益）。
- 不重写 `order_executor` 的整体结构，仅在入口加锁与返回值。
- 不改变现有风控阈值、手数算法、策略语义。
- 不做多账户/多柜台并发支持。

---

## 9. 需要确认的决策点

1. **P0-3 未认领持仓的默认动作**：本 spec 取「冻结（只展示、不自动动仓）+ 界面三按钮」。若你希望默认直接接管并按通用止损管理，需要改为 `adopt`。
2. **P0-2 关窗交互**：本 spec 取「有持仓才确认 + 超时二次确认」。若希望任何情况下都静默等待退出，可去掉第一次确认。
3. **P0-4 保护单**：本 spec 只落地适配器接口与看门狗，不接券商条件单。若你要接，请指明具体通道（同花顺条件单 / 期货公司条件单 API）。
