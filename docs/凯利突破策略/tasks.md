# 凯利突破策略任务清单（Tasks）

按顺序执行；每一步完成后勾掉。

## T1 策略模块

- [x] 新建 `src/strategy/kelly_break.py`，实现 `KellyBreakStrategy`（继承 `TrendStrategyBase`）。
- [x] 覆写 `flatten_at_close`：读 `strategy.flatten_at_close`，默认 True。
- [x] 实现 `need_bars` / `prepare` / `entry_signal` / `exit_reason`，按 spec 第 3 节口径。
- [x] 止盈按收盘价确认，不覆写 `trail_stop`。

## T2 引擎注册

- [x] `src/strategy/factory.py`：import、默认参数节、`REGISTRY`、`_CROSS_ENGINE_ORDER` 四处登记。
- [x] `src/strategy/session_audit.py`：`ENGINE_AUDIT_MODES` 与 `ENGINE_NAMES` 各加一项。
- [x] `src/ui/app_common.py`：`ENGINE_LABELS` 与 `ENGINE_CHOICES` 各加一项。

## T3 配置页

- [x] `src/ui/app_state.py`：`__init__` 新增 6 个变量（含止盈 BooleanVar）。
- [x] `src/ui/app_state.py`：重新载入配置处同步这 6 个变量。
- [x] `src/ui/app_state.py`：`_apply_ui_to_cfg` 写回 `strategy.kelly_break`，带默认值和范围夹紧。
- [x] `src/ui/config_tab.py`：新增「凯利突破」参数面板，含回看根数、ATR 周期、止损倍数、止盈开关、盈亏比、波动率下限。

## T4 测试

- [x] 新建 `tests/test_kelly_break.py`，覆盖 spec 第 4.7 节 8 类场景。
- [x] 确认构造 K 线的 ATR、突破价、止盈价断言与实现一致。

## T5 文档与版本

- [x] `VERSION` 改 `44.2.0`。
- [x] `README.md` 三处版本号改 `44.2.0`，策略列表补「凯利突破」。
- [x] `CHANGELOG.md` 顶部新增 44.2.0 一节。

## T6 验证

- [x] `python -m ruff check .`
- [x] `python -m mypy`
- [x] `python -m pytest -q` 全过
- [x] 汇总改动，不自动 git commit
