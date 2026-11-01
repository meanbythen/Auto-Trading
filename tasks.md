# Tasks — Auto-Trading 阶段规划（v9.9.0）

对应规格：`spec.md`。不涉及实盘下单链路。

## [x] T1 成交模型
- 文件：`src/backtest/engine.py`
- 动作：新增 `FillModel`（下一根开盘 / 滑点跳数 / 每跳价格），`from_cfg` 读 `backtest` 段
- 判据：默认 `on_next_open=True`、`slippage_ticks=1`、`price_tick` 回落到 `trade.ctp.price_tick`

## [x] T2 成交价辅助函数
- 文件：`src/backtest/engine.py`
- 动作：`_next_open_price`（连续时段判定）、`_stop_breached`、`_exit_fill_price`（止损穿价 + 跳空 + 滑点）
- 判据：跨午休/夜盘/最后一根退回当根收盘；跳空按更差的开盘价

## [x] T3 主循环接入
- 文件：`src/backtest/engine.py` `run_backtest`
- 动作：开仓按 `entry_price` 或下一根开盘；平仓/反手走 `_exit_fill_price`；强平也计滑点并修正 `TradeRecord.exit_price`
- 判据：强平记录价 = 收盘 − 1 跳（多单）

## [x] T4 结果可见
- 文件：`src/backtest/engine.py`、`src/ui/app.py`
- 动作：`params_snapshot` 增加 `fill_on_next_open` / `slippage_ticks` / `price_tick` / `fill_note`；回测摘要输出 `fill_note`
- 判据：结果区出现「成交近似 …」一行

## [x] T5 配置开关
- 文件：`config/config.yaml`
- 动作：`backtest.fill_on_next_open: true`、`backtest.slippage_ticks: 1`，并写注释说明如何回到旧口径
- 判据：改 `false` / `0` 后结果回到收盘成交、零滑点

## [x] T6 测试
- 文件：`tests/test_backtest_fill.py`
- 动作：补 6 个成交近似用例；补回选合约与品种保证金 3 个用例
- 判据：单文件 9 passed

## [x] T7 全量验证
- 命令：`.venv\Scripts\python.exe -m pytest -q`
- 判据：`323 passed`，0 failed；`__version__` 为 `9.6.0`

## [x] T8 回测页成交开关
- 文件：`src/ui/app.py`
- 动作：回测页加「下一根开盘成交」开关与「滑点(跳)」输入；`_apply_backtest_switches` 写入本次回测副本
- 判据：改动后本次回测/优化立即生效，不写回实盘配置

## [x] T9 优化结果展示成交口径
- 文件：`src/ui/app.py`
- 动作：优化结果首屏加 `fill_text`（`FillModel.describe()`）
- 判据：优化结果在「运行开关」后出现「成交近似 …」

## [x] T10 分品种每跳价格
- 文件：`src/risk/universe_table.py`、`src/backtest/engine.py`
- 动作：品种表加第 8 列「每跳价格」；读/写/校验/旧表升级；`FillModel.from_cfg(cfg, symbol)` 按品种取跳价
- 判据：表里填 0.5 时 `FillModel.price_tick == 0.5`，留空回落到全局

## [x] T11 测试与版本
- 文件：`tests/test_backtest_fill.py`、`VERSION`、`CHANGELOG.md`
- 动作：新增分品种跳价、品种表每跳列往返两个用例；版本 9.7.0
- 判据：`325 passed`，`__version__ == 9.7.0`

## [x] T12 报单终态映射
- 文件：`src/trader/ctp_logic.py`
- 动作：状态 2（部分成交剩余已撤）、4（未成交且不在队列）、5（撤单）一律记为终态 `cancelled`
- 判据：状态 2 带成交时 `is_terminal` 为 True，`has_working_order` 变 False

## [x] T13 部分成交记账
- 文件：`src/trader/ctp_gateway.py`、`src/trader/ctp_trader.py`、`src/scheduler/runner.py`
- 动作：终态只要 `filled > 0` 就回调记账；`place_order` 汇总实际成交手数；调度层按 `filled_volume - accounted_volume` 补记
- 判据：成交 1/2 手后撤单，持仓只加 1 手，且只记一次

## [x] T14 超时自动撤单
- 文件：`src/trader/ctp_gateway.py`
- 动作：`submit` 等满 `order_wait_sec` 仍非终态时调用 `_cancel_stale`；有柜台编号先撤，再等 `cancel_wait_sec`；无编号只告警并保持拦截
- 判据：假柜台回「已报」后，超时自动发出撤单并收到「已撤」

## [x] T15 配置开关
- 文件：`config/config.yaml`
- 动作：`trade.ctp.auto_cancel_on_timeout: true`、`trade.ctp.cancel_wait_sec: 3`，并写注释说明如何回到旧行为
- 判据：`CtpSettings.from_cfg` 能读到两个值，字符串 `"false"` 也认

## [x] T16 测试与版本
- 文件：`tests/test_ctp_channel.py`、`VERSION`、`CHANGELOG.md`
- 动作：新增 8 个用例（终态、部分成交记账、超时撤单、开关解析、调度层补记）；版本 9.8.0
- 判据：`333 passed`，`__version__ == 9.8.0`

## [x] T17 信号账户去重与持仓派生
- 文件：`src/account/signal_book.py`
- 动作：`append_trade` / `record_program_trade` 新增 `fill_key`、`stop_price`；新增 `open_position_rows(book)` 和 `today_realized_pnl(book, day=None)`
- 判据：同 `bar_id` 不同 `fill_key` 的两笔成交都记；重复 `fill_key` 只记一次；`open_position_rows` 返回正确的方向、手数、加权开仓价和最早止损

## [x] T18 风控当日盈亏统一口径
- 文件：`src/risk/guardian.py`
- 动作：新增 `set_daily_pnl(value)`，覆盖 `daily_pnl` 并按 `max_daily_loss` 触发熔断
- 判据：传入超过限额的负值后 `halted` 变 True；传入小值不触发

## [x] T19 单一写入路径
- 文件：`src/scheduler/runner.py`
- 动作：`_apply_fill_accounting` 先写信号账户再改策略/风控，成功时用 `today_realized_pnl` 覆盖 `risk.daily_pnl`；`_on_ctp_fill` 与 `_finish_ctp_order` 传 `fill_key` 和 `stop_price`；全程只 `_persist_state()` 一次
- 判据：一次成交后策略持仓、风控、`bot_state`、信号账户四处一致；重复回报不重复记账

## [x] T20 启动对账
- 文件：`src/scheduler/runner.py`
- 动作：新增 `_reconcile_books()`，在 `_load_persisted_state` 末尾调用；账本为空时跳过；按账本修正策略持仓、风控净仓和 `daily_pnl`，有差异写 `ledger_reconcile` 事件
- 判据：账本有仓策略无仓时重建；账本无仓策略有仓时清仓；空账本新账期不清仓

## [x] T21 手工平账统一
- 文件：`src/scheduler/runner.py`、`src/utils/state_store.py`、`src/ui/app.py`
- 动作：新增 `TradingBot.manual_close` 与 `manual_close_without_bot`；`_on_book_actual_close` 按是否有常驻分别调用，不再自己写信号账户
- 判据：手工平账后策略/风控/bot_state/信号账户一致，且 `bot_state.daily_pnl == 账本当天净盈亏`

## [x] T22 测试与版本
- 文件：`tests/test_signal_book.py`、`tests/test_state_store.py`、`tests/test_ctp_channel.py`、新增 `tests/test_ledger_reconcile.py`、`VERSION`、`CHANGELOG.md`
- 动作：补齐 T17-T21 的用例；版本 9.9.0；更新 README 说明
- 判据：`pytest -q` 全量通过，`__version__ == "9.9.0"`

## 回滚

无 Git。回滚点按阶段：

- 阶段三（实盘可靠性）：`src/trader/ctp_logic.py`、`src/trader/ctp_gateway.py`、`src/trader/ctp_trader.py`、`src/scheduler/runner.py`、`config/config.yaml`（trade.ctp 段）、`tests/test_ctp_channel.py`
- 阶段二（回测真实性）：`src/backtest/engine.py`、`src/risk/universe_table.py`、`src/ui/app.py`（回测页/优化结果）、`config/config.yaml`（backtest 段）、`tests/test_backtest_fill.py`
- 通用：`VERSION`、`CHANGELOG.md`
