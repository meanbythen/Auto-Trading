# Spec — Auto-Trading 阶段规划（v9.9.0）

## 背景

阶段一把测试基线修到全绿（317 passed）。阶段二处理回测与实盘的成交差异。
旧引擎一律用「当根收盘价」成交：开仓、平仓、反手、强平都用同一个 `close[i]`，且没有滑点。
这会让回测偏乐观——尤其是止损：策略按影线判定破位，但成交价仍取收盘价，快速穿越与跳空的风险被抹掉。

## 目标

1. 信号确认时点与实盘一致：当根收盘后才决策，成交推迟到下一根开盘。
2. 破止损按止损价成交；跳空时按更差的开盘价成交。
3. 成交价按买卖方向让滑点，跳数可配。
4. 结果可见、可回溯：回测结果区写明本次成交近似，参数写入 `params_snapshot`。
5. 旧结果可复现：配置可一键关掉新增规则。

## 非目标

- 不改策略判断逻辑（仍按已收盘K线出信号）。
- 不建模盘口深度、部分成交、报单排队时长。
- 不改优化器挑选规则与打分公式（但优化结果会随成交价变化）。
- 不改实盘 `runner` / `trader` 下单路径。

## 变更清单

### 1. `src/backtest/engine.py` — 成交模型

新增 `FillModel`（`@dataclass(frozen=True)`）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `on_next_open` | `True` | 用下一根开盘价成交 |
| `slippage_ticks` | `1.0` | 买卖各让几跳 |
| `price_tick` | `trade.ctp.price_tick` → `trade.price_tick` → 1 | 一跳价格 |

- `FillModel.from_cfg(cfg)` 读取 `backtest.fill_on_next_open` / `backtest.slippage_ticks` / `backtest.price_tick`。
- `buy(ref)` = `ref + slip`，`sell(ref)` = `ref - slip`（买方抬价、卖方压价，双向不利）。
- `describe()` 生成结果区文案，例如「成交近似  信号确认后按下一根开盘成交；滑点 1 跳（每跳 1）；破止损按止损价成交，跳空按开盘价」。

新增模块级辅助函数：

- `_next_open_price(bars, i, period)`：仅当下一根与当根在连续交易时段内（间隔 ≤ 一个周期）才返回下一根开盘价；D/W 直接取下根开盘；跨午休、跨夜盘、最后一根返回 `None`。
- `_stop_breached(side, stop, bar)`：多单看 `low ≤ stop`，空单看 `high ≥ stop`。
- `_exit_fill_price(...)`：先判止损穿价（多单 `min(stop, 开盘)`、空单 `max(stop, 开盘)`），否则下一根开盘，最后退回收盘价；再按平仓方向让滑点。

### 2. 回测主循环

- 每根先算 `next_open`（`fill.on_next_open` 关闭时为 `None`）。
- 开仓：`decision.entry_price` 有值就作参考价（策略给的是突破/挂单价），否则用下一根开盘 / 收盘；再按开多买入、开空卖出让滑点。
- 反手平仓、普通平仓：走 `_exit_fill_price`，止损价取 `pos.initial_stop or pos.trail_stop or decision.stop_price`。
- 回测结束强平：最后收盘价按持仓方向让滑点，`TradeRecord.exit_price` 记为实际成交价（原实现记的是未让滑点的收盘价）。
- 未实现盈亏 `equity` 仍按收盘价估值，不受滑点影响。

### 3. 结果快照与界面

`params_snapshot` 新增：`fill_on_next_open`、`slippage_ticks`、`price_tick`、`fill_note`。
[app.py](src/ui/app.py#L4447-L4448) 历史回测结果摘要紧跟「覆盖说明」后输出 `fill_note`。

### 4. 配置

`config/config.yaml` 的 `backtest` 段新增：

```yaml
fill_on_next_open: true
slippage_ticks: 1
```

注释写明：要对比旧结果时改为 `false` / `0`。

### 5. 测试

`tests/test_backtest_fill.py`：

- `test_next_open_only_within_continuous_session`：连续时段取下一根开盘；最后一根、跨午休返回 `None`。
- `test_fill_model_reads_config_and_defaults_to_one_tick`：默认开启 + 1 跳；配置可关闭；`price_tick` 可自定义。
- `test_stop_fill_uses_stop_price_and_gap_open`：止损价成交、跳空按开盘价、空单对称。
- `test_exit_without_stop_uses_next_open_then_slippage`：无止损时下一根开盘 + 方向性滑点。
- `test_backtest_records_fill_note_and_slips_forced_close`：端到端，强平价 = 收盘 − 1 跳，快照含成交说明。
- `test_slippage_and_next_open_can_be_switched_off`：关掉后强平价 = 收盘，快照开关为关闭。
- 回测选合约与品种保证金三个用例（`test_backtest_symbol_follows_the_watch_list`、`test_first_symbol_uses_the_watched_contract`、`test_symbol_margin_wins_over_the_generic_number`）。

## 验收

| 项 | 判定 |
|---|---|
| 全量测试 | `.venv\Scripts\python.exe -m pytest -q` → `323 passed` |
| 版本 | `from src.version import __version__` → `9.6.0` |
| 结果可见 | 历史回测摘要出现「成交近似  信号确认后按下一根开盘成交；滑点 1 跳…」 |
| 可回退 | `fill_on_next_open: false` + `slippage_ticks: 0` 时结果回到旧的收盘成交口径 |

## 影响与风险

- 历史回测/交叉测试/策略优化结果会比旧版本差（更接近实盘），与旧报告不可直接比较；`fill_note` 与快照字段用于区分口径。
- 日内实体本身在突破箱沿时给 `entry_price`（箱沿价），滑点后成交略差；收盘清仓走下一根开盘，跨收盘无下一根时退回当根收盘，不会隔夜。
- 风险中等：改动集中在 `engine.py`，不进入实盘链路；有全量测试兜底。

## 阶段二补完（v9.7.0，已完成）

### 1. 回测页可视化开关

- 位置：[app.py](src/ui/app.py#L1460-L1490) 历史回测页「收盘清仓」下面。
- 控件：`下一根开盘成交` 开关（`bt_fill_next_open_var`）、`滑点(跳)` 输入（`bt_slippage_var`）。
- 默认值取自 `config.yaml` 的 `backtest` 段；只在本次回测副本生效，不写回实盘配置。
- 应用点：`_apply_backtest_switches()`，回测与优化两条链路都经过它。

### 2. 优化结果同步展示成交口径

- [app.py](src/ui/app.py#L6093-L6095) 计算 `FillModel.from_cfg(cfg, symbol).describe()`，
- 结果首屏在「运行开关」后多一行，与回测结果同一套文案。

### 3. 分品种每跳价格

- 品种与合约表新增第 8 列「每跳价格」（`universe_table.HEADERS`），留空 = 用全局。
- 读表支持别名：`每跳价格 / 每跳 / 最小变动价位 / tick / price_tick`。
- `parse_universe_rows` → `risk.contracts[key]["price_tick"]`；同品种多行校验纳入该字段。
- 旧表缺列时 `upgrade_universe_tick_column()` 自动补一列（全空），表格会被重写一次。
- `FillModel.from_cfg(cfg, symbol)` 取值顺序：`backtest.price_tick` → 品种表 `price_tick` → `trade.ctp.price_tick` → 1。

### 验证

- `pytest -q` → `325 passed`（新增 2 个用例：分品种跳价优先级、品种表每跳列往返）。
- 版本：`9.7.0`。

## 阶段三：实盘可靠性（v9.8.0，已完成）

已发现并修掉的三处问题，都会让实盘「停摆」或「记账不准」：

### 1. 报单终态

- 现象：CTP 的 `OrderStatus` 2（部分成交、剩余已撤）和 5（撤单但已部分成交）被记成 `partial`，而 `is_terminal("partial")` 为 False。
- 后果：这笔报单永远结束不了，`has_working_order` 一直为 True，该品种后续开仓、平仓全被挡；已成交的手数也不会入账。
- 改法：[ctp_logic.py](src/trader/ctp_logic.py#L288-L293) 把 2 / 4 / 5 统一记为终态 `cancelled`；成交手数保留在 `filled` 里，用于记账和文案。

### 2. 部分成交记账

- 网关：`_finish_locked` 从「仅 `filled` 状态记账」改为「终态且 `filled > 0` 就记账」，`accounted` 标记保证只记一次。
- 执行层：[ctp_trader.py](src/trader/ctp_trader.py#L186-L214) 的 `filled_volume` / `accounted_volume` 改为按每笔实际成交手数累加，不再只看全部成交的单。
- 调度层：`_finish_ctp_order` 用 `filled_volume - accounted_volume` 补记差额，部分成交也能正确更新策略持仓、风控与日志；新增 `order_partial` / `order_unfilled` 事件。

### 3. 超时自动撤单

- [ctp_gateway.py](src/trader/ctp_gateway.py#L349-L384)：`submit` 等满 `order_wait_sec` 仍非终态时调用 `_cancel_stale`。
  - 有柜台编号：先发撤单，再等 `cancel_wait_sec` 秒确认终态回报。
  - 没有柜台编号：撤单报文柜台不认，只告警，并继续用挂单拦截后续报单，不把未成交当成成交。
- 配置：`trade.ctp.auto_cancel_on_timeout: true`、`trade.ctp.cancel_wait_sec: 3`；关掉即回到旧行为。

### 验证

- `pytest -q` → `333 passed`（新增 8 个用例）。
- 版本：`9.8.0`。

### 未纳入本轮（已确认，留待后续）

- 实盘下单前的价位校验仍用全局 `trade.ctp.price_tick`，没有接品种表的每跳价格（cu/au/ag/ru 等会被误拦或放过非法价）。
- GUI 无双实例锁，两个界面并行会重复点同一交易窗口。
- GUI `assume_fill` 仍把「点击成功」当作成交；`assume_fill=false` 又完全不更新持仓，中间缺少「发单待确认」状态。

## 阶段四：统一三套账本（v9.9.0，已完成）

### 现状与问题

三套账本各自写、互不校准：

1. 策略内存账：`strategy.positions`（`PositionState`），启动时从磁盘恢复。
2. 运行状态账：`data/bot_state.json`（持仓快照 + `daily_pnl` + 报单计数/熔断）。
3. 信号账户账：`data/signal_account.json`（开平仓、资金变动、余额），余额还会写回 `config.yaml` 的 `risk.capital`。

已确认的漂移点：

- 手工平账（[app.py](src/ui/app.py#L3681-L3714)）清了策略仓和 `bot_state`，也写了信号账户，但没有更新 `risk.positions` / `risk.daily_pnl`。
- 自动成交（[runner.py](src/scheduler/runner.py#L420-L492)）先改策略和风控，再写信号账户；`append_trade` 按 `bar_id` 去重，同一根 K 线的两笔部分成交会被误当成重复而漏记。
- 当日盈亏用 `risk.daily_pnl`（隔日清零），信号账户用永久累计的 `pnl` 变动，同一笔平仓在两处各算一次，口径不同且从不校准。
- 启动只从 `bot_state` 恢复持仓，不与信号账户未平仓比对，漂移无告警。

### 目标

- 所有成交/平仓/手工平账走**同一个记账入口**，一次操作同时更新策略、风控、`bot_state`、信号账户。
- 以**信号账户**为持仓与已实现盈亏的校准基准：启动时自动对账，不一致就按账本修正并写事件日志。
- 修复部分成交被 `bar_id` 去重误伤的问题：报单级成交用 `fill_key` 去重。
- 不合并文件、不迁移历史数据；`bot_state` 与信号账户仍各自保存原有字段。

### 非目标

- 不改回测、交叉测试、策略优化。
- 不改 CTP 终态判定、超时撤单、GUI 点击路径。
- 不把未实现浮动盈亏写进余额。
- 不新增配置项。

### 变更清单

1. **`src/account/signal_book.py`**
   - `append_trade` 新增 `fill_key: str = ""`、`stop_price: float | None = None`：
     - 有 `fill_key` 时按 `fill_key` 去重（同报单同累计成交量只记一次）；没有时保持原 `bar_id` 去重。
     - 开仓行写入 `stop_price`（缺省 0），供重启重建止损。
   - `record_program_trade` 透传上述两个参数。
   - 新增 `open_position_rows(book)`：用 `_walk_matches` 的未平仓明细，按品种汇总方向、剩余手数、加权开仓价、最早开仓时间、最早有效止损；同品种多空同时存在时按净仓并写 warning。
   - 新增 `today_realized_pnl(book, day=None)`：把当天 `pnl` 与 `commission` 两类资金变动相加，作为当日已实现盈亏。

2. **`src/risk/guardian.py`**
   - 新增 `set_daily_pnl(value)`：覆盖 `daily_pnl`，并按 `max_daily_loss` 触发熔断；用于统一到账本口径。

3. **`src/scheduler/runner.py`**
   - `_apply_fill_accounting(..., *, fill_key: str = "", stop_price: float | None = None)`：
     - 非 `dry_run` 时**先写信号账户**（携带 `fill_key` / `stop_price`），再更新 `strategy.on_filled` 与 `risk.on_order_filled`；
     - 记账成功 → `risk.set_daily_pnl(today_realized_pnl(book))`；记账被跳过 → 保留原有 `update_pnl` 并写 `ledger_skip` 事件；
     - `_persist_state()` 只在末尾执行一次。
   - `_on_ctp_fill`：`fill_key = f"{order_ref}|{action}|{filled}"`，并传 `stop_price`。
   - `_finish_ctp_order`：补记差额时传 `fill_key = f"{order_id}|{action}|{filled_volume}"`。
   - `_load_persisted_state` 末尾调用新增的 `_reconcile_books()`。
   - 新增 `_reconcile_books()`：
     - 账本不存在或没有任何 `trades` 时跳过（新账期空账本不误清持仓）。
     - 用 `open_position_rows` 与 `snapshot_positions` 比较，按品种以账本为准：账本有仓策略无仓 → 重建 `PositionState`；账本无仓策略有仓 → 清仓；手数/方向不同 → 覆盖。
     - 账本行缺少 `stop_price` 时：同品种同方向沿用策略原止损；否则止损置 0、`trail_armed=False`，并写 warning，避免开仓价即止损被立刻打掉。
     - 重建 `risk.positions` / `current_position`，`risk.set_daily_pnl(today_realized_pnl(book))`。
     - 有差异时写 `ledger_reconcile` 事件（含 before / after）。
   - 新增 `manual_close(symbol, exit_price, *, reason="按实际成交价平账")`：读策略仓 → `flatten_strategy_position` → `risk.on_order_filled("close", vol, symbol)` → 写信号账户（`fill_key=manual-<时间>`）→ 同步 `daily_pnl` → `_persist_state()`；返回 `(closed, balance)`。

4. **`src/utils/state_store.py`**
   - 新增 `manual_close_without_bot(root, *, symbol, exit_price, multiplier, initial_capital, reason="按实际成交价平账")`：常驻没在跑时，读 `bot_state` → `mark_position_closed` → 写信号账户 → `daily_pnl = today_realized_pnl(book)` → `save_state`；返回 `(closed, balance)`。信号账户相关导入放在函数内，避免循环依赖。

5. **`src/ui/app.py`**（`_on_book_actual_close`）
   - 有 bot：调用 `bot.manual_close(...)`，不再自己调用 `record_program_trade`。
   - 无 bot：调用 `manual_close_without_bot(...)`。
   - 两条路径都用返回的余额刷新配置页显示与账户分析，不再重复写余额。

6. **测试**
   - `tests/test_signal_book.py`：同 `bar_id` 但不同 `fill_key` 的两笔部分成交都记；`fill_key` 相同的重复回报只记一次；`open_position_rows` 汇总与加权开仓价；`today_realized_pnl` 含手续费。
   - `tests/test_state_store.py`：`manual_close_without_bot` 清仓后 `bot_state.daily_pnl` 与账本当天净盈亏一致。
   - 新增 `tests/test_ledger_reconcile.py`：启动对账按账本重建/清仓，新增 `ledger_reconcile` 事件。
   - `tests/test_ctp_channel.py`：`_on_ctp_fill` 与 `_finish_ctp_order` 的 `fill_key` 传递、重复回报不重复记账。

7. **版本与文档**
   - `VERSION` 改为 `9.9.0`；`CHANGELOG.md` 顶部追加本阶段。
   - `spec.md` / `tasks.md` 追加本阶段；`README.md` 在「状态持久化」和「账户分析」各补一句：三本账以信号账户为准，启动自动对账，不一致写 `ledger_reconcile` 事件。

### 验收

- `pytest -q` 全量通过（在 333 例基础上新增 11 例，全量 344 passed）。
- 同一报单同一累计成交量的重复回报只记一次；同一根 K 线的两笔部分成交分别记账。
- 启动对账后，策略持仓与信号账户未平仓一致；账本为空的新账期不会清掉现有持仓。
- 手工平账后 `bot_state.daily_pnl == signal_account` 当天净盈亏。
- `__version__ == "9.9.0"`。

### 影响与风险

- 启动对账会主动清掉「账本没有、`bot_state` 有」的持仓；新账期空账本已跳过，避免误清。
- 历史开仓行没有 `stop_price`，重建时止损优先沿用原策略止损，否则置 0 并提示。
- 仅实盘路径受影响；`dry_run` 不写信号账户，保持现有模拟行为。

### 回滚

- 阶段四：`src/account/signal_book.py`、`src/risk/guardian.py`、`src/scheduler/runner.py`、`src/utils/state_store.py`、`src/ui/app.py`（`_on_book_actual_close`）、`tests/test_signal_book.py`、`tests/test_state_store.py`、`tests/test_ctp_channel.py`、新增 `tests/test_ledger_reconcile.py`
- 通用：`VERSION`、`CHANGELOG.md`、`spec.md`、`tasks.md`、`README.md`

