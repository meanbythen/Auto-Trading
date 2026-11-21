# 审查报告 §7.3 第三批实施任务清单

> 基线 49.20.4；目标版本 49.21.0。完成一项把 `[ ]` 改为 `[x]`。

## B1 日线未收线

1. [x] `src/data/periods.py`
   - [x] `drop_forming_bar` 增加 `period` 参数；`D` 周期按 `futures_trading_day` 截断。
2. [x] `src/scheduler/runner.py`
   - [x] 信号/止损/日线三处调用传 `period`。
3. [x] `src/backtest/engine.py`
   - [x] 信号/止损/大周期三处调用传 `period`。
4. [x] 测试：盘中排除当日日线、周末/夜盘边界、不传 `period` 行为不变。

## B2 trigger/touch 只用已收线

5. [x] `src/scheduler/runner.py::scan_symbol`
   - [x] 删除 `ENTRY_MODE_TRIGGER` 拼接分支。
   - [x] `eval_bars/eval_trail/eval_trend` 只用 `closed`。
   - [x] 通用止损 `forming=None`。
   - [x] 保留 `forming` 仅用于健康状态与启动回溯。
6. [x] 测试：未收线破位影线不触发止损/开仓。

## B3 回测单笔上限

7. [x] `src/backtest/limits.py`
   - [x] 读取 `trade.ctp.max_order_volume` 到 `max_per_order`。
   - [x] `size_open` 传给 `PositionSizer.calc_open`。
8. [x] `src/backtest/fill.py`
   - [x] `describe()` 补实盘对手价/单笔上限差异说明。
9. [x] 测试：上限 5 时 `calc_open(max_per_order=5)`；未配置为 `None`。

## B4 不模拟项标注

10. [x] `src/backtest/engine.py::_params_snapshot`
    - [x] 新增 `live_gap_note`（组合敞口/健康/去重/暂停/排队/延迟/部分成交）。
11. [x] `src/ui/backtest_actions.py`
    - [x] 结果文本输出“口径差异”。
12. [x] 测试：快照字段含关键差异项；旧结果文件兼容。

## B5 清仓时点说明

13. [x] `live_gap_note` 写入 ≥15 分钟周期清仓差异。
14. [x] 测试：15 分钟周期、`flatten_times=["14:55"]` 在 15:00 根收盘平仓。

## B6 平今按分笔 lot

15. [x] `src/backtest/engine.py`
    - [x] `open_meta` 增加 `lots`；首次开仓与加仓分别写入。
    - [x] `_net_pnl_after_fees` 按最新 lot 优先、逐 lot 判断平今。
    - [x] 部分平仓后更新剩余 lot、`entry_time`、`open_volume`。
16. [x] 测试：跨日加仓全平/部分平、同日加仓全平三种费率场景。

## B7 保证金与差异标注

17. [x] `src/backtest/engine.py`
    - [x] `_BacktestSetup` 增加 `margin_rate`。
    - [x] 读取 `backtest.margin_rate`，非法值回退 0.10。
    - [x] `peak_margin` 与快照使用该比例。
18. [x] 测试：0.12 生效、默认 0.10、非法值回退。

## B8 复盘口径标注

19. [x] `src/review/replay.py`
    - [x] 新增 `REPLAY_COST_NOTE` 并追加到正常复盘结果。
20. [x] `src/review/quality.py`
    - [x] 质量报告追加“按点数、不含手续费滑点”说明。
21. [x] 测试：两处文本均包含口径说明。

## 统一验证

22. [x] 新增 `tests/test_review_7_3_consistency.py` 并覆盖上述纯函数/快照用例。
23. [x] `python -m pytest -q`
24. [x] `lint-imports`
25. [x] `python -m ruff check src tests`
26. [x] `python -m mypy src`

## 文档与版本

27. [x] 更新 `docs/项目全面审查报告-2026-11-21.md`（§0.3、§4.2、§7.3）。
28. [x] 更新 `CHANGELOG.md`（49.21.0，注明回测净盈亏/成交笔数可能变化）。
29. [x] 更新 `README.md` 版本号。
30. [x] 更新 `VERSION` 为 `49.21.0`。
