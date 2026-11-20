# R8 任务清单

对应版本：49.15.0
规格：[spec.md](spec.md)

## 一、代码去重

- [x] T1 新增 `src/domain/pnl_stats.py`（`PnlSummary` + `summarize_pnl`）
- [x] T2 `src/backtest/metrics.py::calc_performance` 改用 `summarize_pnl(zero_as_loss=True)`
- [x] T3 `src/ops/health.py` 删除本地 `_profit_factor` / `_win_rate`，改调 `summarize_pnl`
- [x] T4 `src/review/shadow.py` 改调 `summarize_pnl`
- [x] T5 `src/account/signal_stats.py::_slice_from_closes` 改调 `summarize_pnl(zero_as_loss=False)`
- [x] T6 `src/domain/contracts.py` 增加 `DEFAULT_MULTIPLIER`，替换 sizing / ledger / signal_book / signal_stats / state_store / universe_table / margin_table / quality 的字面量兜底
- [x] T7 `src/strategy/factory.py`：派生 `_OBSOLETE_STRATEGY_SECTIONS`，新增 `param_section`，补 `__init__` 导出
- [x] T8 `src/backtest/optimizer.py`、`src/backtest/stress.py` 去掉 `_ENGINE_PARAM_SECTION`，改调 `param_section`
- [x] T9 `src/ui/config_collect.py` 用 `REMOVED_ENGINES` 批量清旧小节

## 二、测试

- [x] T10 新增 `tests/test_pnl_stats.py`
- [x] T11 新增乘数一致性 / 别名映射 / 已删引擎名单断言
- [x] T12 全量 `pytest`、改动文件 `ruff`、改动模块 `mypy`

## 三、文档

- [x] T13 README：版本、自检入口、验收报告链接
- [x] T14 配置说明书：版本 + 49.15.0 复核记录 + 临时脚本核对键
- [x] T15 回测实盘评估方案 / 综合升级规划状态刷新

## 四、发版

- [x] T16 `VERSION` 49.15.0、`CHANGELOG.md` 新增 49.15.0 段
- [x] T17 验收报告 `git mv` 到 49.15.0，更新 R8 4/4、更新记录、P2 条目
- [x] T18 同步 README 与 `R11-第二阶段/spec.md` 引用
- [x] T19 汇报结果（不自动提交）
