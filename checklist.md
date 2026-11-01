# Checklist — 阶段四：统一三套账本（v9.9.0）

对应规格：`spec.md`「阶段四：统一三套账本」。逐条核对 T17-T22。

## 信号账户（T17）

- [x] 同一 `bar_id`、不同 `fill_key` 的两笔部分成交都会记进账本
- [x] 同一 `fill_key` 的重复回报只记一次
- [x] `open_position_rows` 返回正确方向、合计手数、加权开仓价
- [x] `open_position_rows` 取最早的有效止损价；多空同品种按净仓处理
- [x] `today_realized_pnl` 只统计当天的 `pnl` 与 `commission`，忽略初始资金/出入金/结算

## 风控口径（T18）

- [x] `set_daily_pnl` 直接覆盖 `daily_pnl`
- [x] 覆盖值低于 `-max_daily_loss` 时置 `halted=True` 并写 `halt_reason`
- [x] 未超限额的小额不触发熔断

## 单一写入（T19）

- [x] 一次成交后策略持仓、风控净仓、`bot_state`、信号账户四处一致
- [x] 重复的同一笔成交回报不重复记账，也不重复加仓
- [x] `_on_ctp_fill` 与 `_finish_ctp_order` 都把 `fill_key`、`stop_price` 传给记账入口
- [x] 一次记账流程只调用一次 `_persist_state()`

## 启动对账（T20）

- [x] 账本有仓、策略无仓时按账本重建持仓（含止损）
- [x] 账本无仓、策略有仓时清掉策略持仓
- [x] 账本没有任何成交记录时跳过对账，不清真实持仓
- [x] 对账有差异时向 `data/events.jsonl` 追加 `ledger_reconcile` 事件
- [x] 对账后 `risk.daily_pnl` 等于账本当天净盈亏

## 手工平账（T21）

- [x] 常驻在跑时 `TradingBot.manual_close` 同步策略、风控、`bot_state`、信号账户四处
- [x] 常驻没在跑时 `manual_close_without_bot` 同步 `bot_state` 与信号账户
- [x] 手工平账后 `bot_state.daily_pnl == 账本当天净盈亏`
- [x] 界面 `_on_book_actual_close` 不再自己写信号账户，只调用上述两个入口

## 测试、版本与文档（T22）

- [x] `tests/test_signal_book.py`、`tests/test_state_store.py`、`tests/test_ctp_channel.py`、`tests/test_ledger_reconcile.py` 覆盖上述要点
- [x] `.venv\Scripts\python.exe -m pytest -q` 全量通过（344 passed）
- [x] `VERSION` 为 `9.9.0`，`__version__` 与之一致
- [x] `CHANGELOG.md` 顶部有 9.9.0 条目
- [x] `README.md`「状态持久化」「账户分析」写明账本统一口径
