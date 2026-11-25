# 任务清单（57.0.0 触发即开仓）

## T1 公共类型与工具
- [x] `src/strategy/types.py`：`StrategyDecision` 增加 `trigger_price: float | None = None`。
- [x] 新增 `entry_touched()`、`forming_price_range()`、`pick_breakout_side()` 三个纯函数。
- 验证：`python -c "from src.strategy.types import entry_touched"` 可导入。

## T2 趋势基类
- [x] `src/strategy/trend_base.py`：`evaluate` 增加 `forming=None`，保存 `self._forming`。
- [x] 空仓 `bar_id` 在 trigger 时用 forming 索引。
- [x] 决策组装支持 `meta["trigger_price"]` → `entry_price` / `trigger_price` / `meta["intraday_entry"]`。

## T3 支持 trigger 的策略（9 个）
- [x] `bollinger_break.py`：触发价 up / down。
- [x] `kelly_break.py`：触发价 high_n / low_n。
- [x] `sar_trend.py`：触发价 max(sar, ma) / min(sar, ma)。
- [x] `liq_stop.py`：forming 极值补足击穿前提，触发价 zone 外沿。
- [x] `trend_follow.py`：`evaluate` 增加 forming，触发价 entry_high / entry_low。
- [x] `supertrend.py`：`evaluate` 增加 forming，触发价 st_now。
- [x] `daily_body.py`：`evaluate` 增加 forming，新增 `_direction_signal_trigger`，触发价箱沿。
- [x] `swing_box.py`：`evaluate` 增加 forming，触发价箱沿 + forming 量能。
- [x] `dual_donchian.py`：`evaluate` 增加 forming，触发价 up / down。

## T4 实盘接入
- [x] `src/scheduler/runner.py`：读取 `entry_mode`，trigger 时把 forming 传给 `_evaluate_symbol`。
- [x] `_evaluate_symbol` 增加 `forming` 参数，按 `_evaluate_accepts("forming")` 传入。
- [x] 触发开仓成功后记录 `intraday_entry` 事件。
- [x] 更新 L2157 附近注释，明确 trigger / close 两种口径。
- 验证：`pytest tests/test_runner_guard.py -q` 无回归。

## T5 回测接入
- [x] `src/backtest/engine.py`：trigger 时把当前根当 forming 传给 `evaluate`。
- [x] `_open_position` 增加触发成交分支：触发价（跳空按开盘）+ 滑点，成交时间为触发根。
- [x] 触发成交的 `exec_idx` / `exec_bar` 改为当前根。
- 验证：`pytest tests/test_backtest_fill.py -q` 无回归。

## T6 UI 文案
- [x] `src/ui/config_tab_sections.py`：改写触发方式提示，列出 9 个支持引擎与 6 个回退引擎。
- 验证：配置页文案与实际行为一致。

## T7 测试
- [x] 新增 `tests/test_intraday_entry.py`，覆盖第 7 节 12 类用例。
- [x] `pytest tests/test_intraday_entry.py tests/test_intraday_stop.py tests/test_backtest_fill.py tests/test_runner_guard.py -q` 通过。

## T8 全量与门禁
- [x] `python tools/testkit.py baseline`。
- [x] `python tools/testkit.py gate --coverage` 6 步通过。

## T9 升版本与文档
- [x] `VERSION` → `57.0.0`。
- [x] `CHANGELOG.md` 顶部新增 57.0.0（大更新：开仓规则变更；列出支持/回退引擎；需重启生效）。
- [x] `README.md` 版本号、用例数、开仓触发方式说明同步。
- [x] `docs/TODO.md` 记录完成项。
- [x] 两份架构 HTML 版本号与行数同步。
- [x] 测试方案/用例/报告同步版本与用例数。
- [x] `python tools/check_doc_versions.py` 通过。

## T10 汇报
- [x] 中文汇报：支持/回退引擎清单、触发价口径表、验证结果、实盘需重启。
