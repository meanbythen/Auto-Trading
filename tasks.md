# Tasks — 策略优化参数按配置页重新规划（v9.31.0）

对应规格：[spec.md](spec.md)

## T1 网格常量与通用止损公共旋钮
- [x] 在 `src/backtest/optimizer.py` 新增 `GENERIC_STOP_KIND_LIGHT`、`GENERIC_STOP_KIND`、`GENERIC_STOP_PARAMS`
- [x] 新增 `_with_generic_stop(grid, kinds, *, full=False)`（深拷贝，不改原常量）
- [x] 从 `GRID_BODY_LIGHT` / `GRID_BODY` 移除 `generic_stop.kind`
- [x] 从 `GRID_BODY_FULL` 移除四个 `generic_stop.*`，保留六个日内实体扩展项
- [x] 更新组合数注释（精简 24 / 标准 2592 / 完整约 398 万）

## T2 深度接线
- [x] `grid_for_engine`：日内实体「完整」返回 `_with_generic_stop(GRID_BODY_FULL, GENERIC_STOP_KIND, full=True)`
- [x] 其余非固定引擎：精简/标准/完整都接通用止损；完整额外带通用止损参数
- [x] 固定规则引擎（早盘 A/B）与未知引擎仍返回空网格

## T3 中文参数名
- [x] `PARAM_LABELS_CN` 补 `eth_minutes2`、`stop_extra_ticks`、`skip_gap_ticks`、`max_box_ticks`、`max_chase_ticks`、`ema_period`、`ema_buffer_ticks`
- [x] 补 `generic_stop.kind`、`generic_stop.lookback`、`generic_stop.sar_af_step`、`generic_stop.atr_period`、`generic_stop.atr_mult`

## T4 写回配置
- [x] 顶部导入 `GenericStopConfig`、`normalize_generic_stop`
- [x] 新增 `_GENERIC_STOP_PREFIX` 与 `_apply_generic_stop(st, params)`
- [x] `_apply_params` 进分支前先剥离 `generic_stop.*` 并写回 `strategy.generic_stop`

## T5 出厂值
- [x] `_factory_values` 用 `GenericStopConfig()` 补五个 `generic_stop.*` 出厂值
- [x] `param_history.FACTORY_DEFAULT_SNAPSHOT["daily_body"]` 补齐 `skip_gap_ticks` / `max_box_ticks` / `max_chase_ticks` / `ema_period` / `ema_buffer_ticks`

## T6 测试
- [x] 更新 `tests/test_optimizer_holdout.py::test_daily_body_grid_stays_small`（键集合、取值、三种深度组合数）
- [x] 更新 `test_every_optimized_param_is_a_config_page_item` 的 `ui_covered` 与深度遍历
- [x] 新增 `test_generic_stop_params_write_under_strategy_root`
- [x] 新增 `test_factory_values_cover_generic_stop`

## T7 验证
- [x] `python -m pytest -q` 全绿（514 passed）
- [x] 预览 `grid_for_engine("daily_body", "完整")` 与 `describe_grid`，确认键与中文名齐全
- [x] 检查 fu2701 缓存数据上四种通用止损的跑结果不同

## T8 版本与文档
- [x] `VERSION` → `9.31.0`
- [x] `CHANGELOG.md` 新增 9.31.0 条目
- [x] `README.md` 更新「策略优化」深度与通用止损说明
