# R9 架构收敛

对应版本：49.16.0
上位文档：[阶段性验收报告-49.16.0.md](../阶段性验收报告-49.16.0.md) 第六节 R9、第八节

## 1. 目标

把「架构合理性」从「基本合理，需收敛」推进到可长期维护的状态，四项各收口一次，业务数值与交易行为不变：

1. `OrderExecutor` 按职责拆开，单文件只留编排与命令入口。
2. UI 不再直接改运行对象、不再读 `strategy.positions`、不再调私有方法。
3. 用 import-linter 固定分层方向，并清掉六组设计级循环。
4. 位置状态以策略持仓为唯一真相，风控净仓只派生，UI 只读快照。

不做：任何价格、手数、风控阈值、撮合规则变更；费率与夜盘模型（R10）；`data ↔ backtest` 与 `strategy → export` 两组历史反向依赖（本轮登记豁免）。

## 2. 范围与判定

| 编号 | 问题 | 处置 |
|---|---|---|
| A1 | `src/scheduler/order_executor.py` 2371 行，价格、意图、成交、编排、命令入口混在一起 | 拆成 `order_pricing` / `order_state` / `fill_reconcile` 三个 mixin，原类组合 |
| B1 | `src/ui/config_support.py` 保存配置时直接写 `bot.scan_interval`、`bot.cfg`，并调 `bot._refresh_sessions()` | 新增 `TradingBot.apply_runtime_config(cfg)`，UI 只发命令 |
| B2 | `src/ui/run_tab.py` 直接读 `bot.strategy.positions`、直接调 `bot._record_event` | 改用只读快照与公开命令 |
| B3 | `src/ui/bot_worker.py` 读私有 `_orders_paused` | 暴露公开属性 |
| C1 | 无依赖方向约束，新增反向导入无人拦截 | `[tool.importlinter]` 分层契约 + 禁止核心依赖 UI |
| C2 | 六组设计级循环（全部由函数内延迟导入隐藏） | 逐组拆边 |
| D1 | `risk.positions` 在 3 处各写一遍内联重建 | 统一走 `_rebuild_risk_positions()` |
| D2 | UI/报告各自摸 `strategy.positions` | `TradingBot.positions_snapshot()` 返回只读副本 |

## 3. 实现

### A1 拆分 OrderExecutor（行为不变，纯搬移）

`src/scheduler/order_executor.py` 的类改为组合：

```python
class OrderExecutorMixin(OrderPricingMixin, OrderIntentMixin, FillReconcileMixin):
    """订单编排与人工命令入口；共享状态仍保存在 TradingBot 实例上。"""
```

新增三个模块，均为 mixin，只搬方法体、不改逻辑：

| 新文件 | 搬入内容 |
|---|---|
| `src/scheduler/order_pricing.py` | `ctp_opponent_limit`、`_price_tick_for`、`_close_protect_ticks`、`_open_protect_ticks`、`_ctp_quote_snapshot`、`_ctp_is_buy`、`_ctp_limit_price`、`_ctp_open_slip_blocked`；同时把 `entry_slip_blocked` 从 `runner.py` 搬入 |
| `src/scheduler/order_state.py` | `_intent_files`、`_write_order_intent`、`_clear_order_intent`、`_order_intent`、`_halt_on_pending_intent`、`_check_pending_intent`、`pending_intent_matches`、`confirm_positions_reviewed`、时间线 `_timeline_trace`/`_record_order_timeline`/`_start_decision_timeline` |
| `src/scheduler/fill_reconcile.py` | `ctp_fill_key`、`_apply_fill_accounting`、`_record_signal_account`、`_on_ctp_fill`、`_on_ctp_fill_locked`、`_book_ctp_leftover`、`_finish_ctp_order` |

`order_executor.py` 保留：`trader_capabilities`、`_contract_multiplier`、`_size_open`、`_trade_intent`、`_reject_gate`、`_execute*`、`_dispatch`、`_send_and_book`、`_order_session_open`、`_broker_net_for`、`_book_external_close`、`manual_open`/`manual_close` 及柜台持仓认领命令。

兼容导出：`order_executor` 继续可 `from src.scheduler.order_executor import ctp_opponent_limit, CTP...`；`runner` 继续可 `from src.scheduler.runner import entry_slip_blocked`。现有测试不改导入路径。

类属性按归属搬移：`_INTENT_HALT_PREFIX`、`_pending_intent*` 去 `order_state`；`FILL_LOCK_TIMEOUT`、`_RECENT_FILL_LIMIT`、`_recent_fill_keys` 去 `fill_reconcile`。

### B1–B3 UI → 命令层

`src/scheduler/runner.py` 新增公开命令/只读接口：

```python
def apply_runtime_config(self, cfg: dict[str, Any]) -> None:
    """配置页保存后同步运行中的扫描间隔、合约时段、合约表与日亏限额。"""
    # 与下单共用交易锁，避免一轮扫描按旧参数算手数

def positions_snapshot(self) -> dict[str, dict[str, Any]]:
    """程序账持仓的只读副本（side/volume/entry_price/opened_at）。"""

def note_force_close(self) -> None:
    """界面强制关窗时记一条关键事件。"""

@property
def orders_paused(self) -> bool: ...
```

- `src/ui/config_support.py::_on_save`：删除对 `bot.scan_interval`、`bot.cfg`、`bot._refresh_sessions`、`bot.sync_risk_limits` 的直接操作，改为一次 `bot.apply_runtime_config(cfg)`；日志里的间隔读公开属性 `bot.scan_interval`。
- `src/ui/run_tab.py`：持仓弹窗改用 `bot.positions_snapshot()`；强制关窗改调 `bot.note_force_close()`。
- `src/ui/bot_worker.py`：增加 `apply_runtime_config` 转发；`orders_paused()` 读公开属性。

### C1 import-linter 分层

`requirements-dev.txt` 增加 `import-linter==2.3`；`pyproject.toml` 增加：

```toml
[tool.importlinter]
root_packages = ["src"]
```

三个 `forbidden` 契约（不用 `layers`：`|` 在 layers 里表示同层互相独立，会把业务层内部的互相引用也判违规）：

| 契约名 | source | forbidden |
|---|---|---|
| 领域/内核不依赖业务层 | `src.domain`、`src.kernel` | 其余全部业务层 + 调度 + 界面 |
| 业务层不依赖调度与界面 | 业务层 13 个包 | `src.scheduler`、`src.ui` |
| 调度不依赖界面 | `src.scheduler` | `src.ui` |

契约含义：`domain`/`kernel` 不依赖任何其它层；业务层不得依赖 `scheduler`/`ui`；`scheduler` 不得依赖 `ui`。实测 `Contracts: 3 kept, 0 broken`，后续新增反向导入会直接失败。`src.main` 是启动入口，不在契约内。

### C2 拆六组循环（8 组 → 2 组）

| 循环 | 拆法 |
|---|---|
| `scheduler`：`order_executor → runner` | `entry_slip_blocked` 随 A1 搬到 `order_pricing`，`runner` 反向再导出 |
| `account`：`ctp_sync ↔ signal_book` | `broker_day_pnl` 搬到新 `src/account/broker_pnl.py`，`ctp_sync` 原样导出 |
| `review`：`fundamental ↔ sources` | `_PRODUCTS` 与 `product_of` 搬到新 `src/domain/products.py`，两处引用改导入 |
| `backtest`：`optimizer ↔ param_table` | `PARAM_LABELS_CN`、`PARAM_VALUE_LABELS_CN`、`default_grid_for_engine` 搬到新 `src/backtest/param_meta.py`，`optimizer` 原样再导出 |
| `login`：`app_login ↔ ths_login ↔ price_mode` | `force_foreground`、`_visible_text` 搬到新 `src/utils/winui.py`，三个调用方改导入 |
| `ops`：`alerts ↔ notify` | 告警外发失败的本地告警改由 `alerts` 在 `notify` 返回失败时补记，`notify` 不再反向导入 `alerts` |
| 大环（`data/strategy/export/backtest/utils`） | 本轮只拆两处低风险边：`session_bar_times` 搬到 `src/data/sessions.py`；`param_history` 的引擎标签改用 `strategy.factory`，不再导入 `review.replay` |

拆后实测仍存在的两组，用 `tests/test_architecture.py` 的豁免清单登记：

- 13 模块业务环：`data / strategy / backtest / export / review / risk / account / ops / utils / domain / trader / screen / research` 之间历史反向依赖（`data ↔ backtest`、`strategy → export` 等），本轮不动，留给 R10 之后按需处理。
- `src.scheduler.flatten_manager ↔ src.scheduler.runner`：`in_trading_session` 的兼容转发 shim，保留旧调用方可替换该函数的语义。

另：`src.ui` 的 `TYPE_CHECKING` 导入是伪环，架构测试不把它算作循环。

### D1–D2 位置状态单一真相

- `src/scheduler/runner.py` 三处内联重建（启动恢复、账本对账、`_rebuild_risk_positions` 本体）统一调用 `_rebuild_risk_positions()`；`risk.current_position` 由它一并维护。
- 新增 `positions_snapshot()` 返回深拷贝的简单行，UI 与报告不再摸 `strategy.positions`。
- 柜台快照 `trader.last_broker_snapshot` 仍是外部事实，不并入程序账；`_broker_net_for` 继续区分「快照未就绪 = 未知」与「查不到 = 0」。

## 4. 验收

1. `python -m pytest -q -p no:cacheprovider` 全量 0 失败。
2. `lint-imports` 通过（import-linter 2.3 无 `python -m importlinter` 入口）；新增 `tests/test_architecture.py`：
   - 分层契约对应的反向导入扫描；
   - 核心模块循环扫描，豁免清单只含上面两组；
   - `src/ui` 不得出现 `bot.cfg =`、`bot.sizer`、`bot.strategy.positions`、`bot._` 调用（白名单：`bot.status_snapshot` 等公开接口）。
3. `OrderExecutor` 拆分后：`order_executor.py` ≤ 1400 行；三个新模块各自可单独导入；现有 `test_runner_guard.py`、`test_ctp_channel.py` 不改断言即通过。
4. 新增 `tests/test_runtime_config.py`：`apply_runtime_config` 更新间隔/时段/合约表/限额；`positions_snapshot` 改副本不影响策略持仓。
5. 改动文件 `ruff` 通过；改动模块 `mypy` 通过。
6. 文档：README、配置说明书、综合升级规划状态刷新；验收报告第八节 R9 记 4/4。

## 5. 风险

- A1 是纯搬移，风险集中在 mixin 组合后的 MRO 与类属性归属；用全量测试与导入冒烟兜底，不新增回调路径。
- C2 的 `ops` 改动会改变「外发失败」告警的写入顺序（先失败再补记），内容与级别不变；`login` 的窗口工具搬移只改导入位置。
- C1 新增开发依赖，只影响开发环境；运行依赖不变。
- D1 只去掉重复重建，不改变任何持仓数值；风控净仓仍由策略持仓派生。
