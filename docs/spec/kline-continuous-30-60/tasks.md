# 任务清单：30/60 分钟连续交易时间重拼

> 依赖：`spec.md` 已确认。按顺序执行，每步完成后勾选。

- [x] T1 `src/data/periods.py`：实现 `bars_from_trading_minutes` 连续累计
      （分组、cumcount、`pos//step`、聚合、右标签）。
- [x] T2 `src/data/periods.py`：`bars_from_minutes` 分流
      `step>=30` 走连续累计，`5<=step<30` 保持 `bars_from_session_clock`。
- [x] T3 `src/data/periods.py`：新增 `continuous_fill_minutes`、`partial_tail_bar`。
- [x] T4 `src/data/periods.py`：`session_close_times` 对 `step>=30` 输出连续标签。
- [x] T5 `src/data/periods.py`：`_drop_clock_hour_native_60` 泛化为
      `_drop_conflicting_native(native, persist, step)`，并在 `overlay_minute_bars`
      中对 `step>=30` 调用（保留覆盖区间外原生 K）。
- [x] T6 `src/data/kline.py`：删除 `_with_break_half_bar`、
      `_align_break_half_bar` 及 `fetch()` 调用，更新相关注释。
- [x] T7 `src/strategy/decision_guard.py`：新增 `apply_partial_tail_gate`。
- [x] T8 `src/scheduler/runner.py`：交割闸门后接尾盘闸门，
      记录 `partial_bar_block_open` 与 `signal_blocked` 审计；`catch_up_used` 放行。
- [x] T9 `src/backtest/engine.py`：`_decide_bar` 接同一闸门，
      `rollover_carry` 放行，其余开仓记 `尾盘半根` 拒绝。
- [x] T10 `tests/test_periods.py`：按 spec 验收表改写 30/60/白银/收盘时刻测试，
      新增夜盘、无夜盘、尾盘判定测试；5/15 测试保持不动。
- [x] T11 `tests/test_kline_rebuild.py`：删除半根补丁测试，
      新增连续累计边界、夜盘不结转、缺分钟、原生冲突清理、
      `apply_partial_tail_gate` 测试。
- [x] T12 运行定向测试、全量 pytest、ruff、mypy，修复回归。
- [x] T14 `src/data/bar_store.py`：`aligned_bars` 对 `step>=30` 改用交易时段允许收线集合，
      避免连续口径的 10:45、11:15、13:45、14:15、14:45 被 `hm % step` 误删；
      同步更新 `test_bar_store.py`。
- [x] T13 `VERSION` → `51.0.0`；更新 `CHANGELOG.md`、`README.md`（按项目规则）。
