# Checklist — 策略优化参数按配置页重新规划（v9.31.0）

对应规格：`spec.md`。逐条核对 T1-T8。

## 网格与深度（T1 / T2）

- [x] `GENERIC_STOP_KIND_LIGHT` / `GENERIC_STOP_KIND` / `GENERIC_STOP_PARAMS` 已新增
- [x] `_with_generic_stop` 返回深拷贝，不改原常量
- [x] `grid_for_engine("daily_body", "精简")` 组合 24 组，仅比 `off` / `sar`
- [x] `grid_for_engine("daily_body", "标准")` 组合 2592 组，通用止损四种都试
- [x] `grid_for_engine("daily_body", "完整")` 使用 `GRID_BODY_FULL`，组合约 398 万组
- [x] 趋势跟随 / SuperTrend / 单均线 / 双均线 / 长线趋势三档都带通用止损
- [x] 早盘 A/B 与未知引擎仍返回空网格

## 参数名与写回（T3 / T4）

- [x] `PARAM_LABELS_CN` 覆盖日内实体扩展项与全部 `generic_stop.*`
- [x] `describe_grid` 预览能看到 18 个旋钮与中文名
- [x] `_apply_params` 把 `generic_stop.kind` 规整成四种之一（`SAR` → `sar`）
- [x] `generic_stop.lookback` / `atr_period` 写成 int，`atr_mult` 等写成 float
- [x] `generic_stop.*` 写在 `strategy.generic_stop`，不残留在策略小节

## 出厂值（T5）

- [x] `_factory_values` 对全部非固定引擎给出 `generic_stop.kind == "off"`
- [x] `FACTORY_DEFAULT_SNAPSHOT.daily_body` 补齐跳空/箱宽/急破/EMA 默认值

## 测试与验证（T6 / T7）

- [x] `tests/test_optimizer_holdout.py` 三种深度断言通过
- [x] `ui_covered` 覆盖全部优化键，并按三种深度遍历
- [x] 新增「通用止损写回 strategy 根」用例通过
- [x] 新增「出厂值覆盖通用止损」用例通过
- [x] `python -m pytest -q` 全量 514 项通过
- [x] fu2701 5 分钟缓存数据上，四种通用止损都能跑出不同结果

## 版本与文档（T8）

- [x] `VERSION` 为 `9.31.0`
- [x] `CHANGELOG.md` 顶部有 9.31.0 条目
- [x] `README.md` 策略优化深度与通用止损说明已更新
