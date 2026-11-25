# 任务清单（54.0.0 盘中触及止损）

## T1 纯函数与状态
- [x] `src/scheduler/runner.py` 增加 `_INTRADAY_STOP_RETRY_SEC`、`_entry_dt()`、`_bar_start()`、`_intraday_stop_decision()`。
- [x] `TradingBot.__init__` 增加 `self._intraday_stop_sent: dict[str, tuple[str, float]] = {}`。
- [x] `from src.strategy.types import STOP_MODE_TOUCH, normalize_stop_mode, stop_triggered`。
- 验证：`python -c "from src.scheduler.runner import _intraday_stop_decision"` 可导入。

## T2 实盘接入
- [x] `apply_generic_stop(..., forming=None if catch_up_used else forming, ...)`。
- [x] 通用止损之后、交割闸门之前接入盘中策略止损块（HOLD + 非回溯 + 未暂停 + 非空仓 + 同根 120 秒去重）。
- [x] 发单成功后写 `_intraday_stop_sent` 并调用 `leg.note_flat_wait_next_bar(symbol, bar_id)`。
- 验证：`tests/test_intraday_stop.py` 全过；`tests/test_runner_guard.py` 无回归。

## T3 注释与口径固化
- [x] 改写 `runner.py` 关于「回测下一根成交」的错误注释。
- [x] `src/backtest/engine.py` `_close_position` 上方补「触发根成交」注释。
- 验证：注释与第 4 节口径表一致，无逻辑改动。

## T4 测试
- [x] 新增 `tests/test_intraday_stop.py`（8 例）。
- [x] `tests/test_backtest_fill.py` 补 2 例（触发根止损价、含滑点）。
- [x] `pytest tests/test_intraday_stop.py tests/test_backtest_fill.py tests/test_generic_stop.py tests/test_runner_guard.py -q` 通过（89 项）。

## T5 全量与门禁
- [x] `python tools/testkit.py baseline`：选中 2513 项，收集 2518 项，取消 5 项。
- [x] `python tools/testkit.py gate --coverage` 6 步全通过（pytest/coverage/ruff/mypy/lint-imports/docs）。

## T6 升版本与文档
- [x] `VERSION` → `54.0.0`。
- [x] `CHANGELOG.md` 顶部新增 54.0.0（大更新：止损规则变更；需重启生效）。
- [x] `README.md` 三处版本号同步。
- [x] `docs/TODO.md` 顶部对应版本同步。
- [x] 两份架构 HTML 的版本号与行数（115,357）同步，测试文件数 218。
- [x] 测试方案/用例/报告同步版本与用例数（2512），报告补 54.0.0 执行行。
- [x] `python tools/check_doc_versions.py` 通过。

## T7 汇报
- [x] 中文汇报：改动文件、验证结果、实盘需重启、盘中触发与回测口径对照。
