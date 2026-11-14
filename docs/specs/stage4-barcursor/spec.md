# Stage4-4 BarCursor 抽取规格（44.6.11）

## 1. 背景

`docs/项目审阅报告.md` 阶段 4 第 4 条：`run_backtest` 过长，逐根推进的状态（索引、当前 K、下一根开盘价、时间游标）混在主循环里。
本轮只做**等价抽取**：把「这一根 K 看到什么」收进 `BarCursor`，主循环保留「这一根做什么」。
不改任何交易规则、成交价、止损顺序、统计口径。

## 2. 现状（当前行号）

- 主循环：`src/backtest/engine.py:717` `for i in range(loop_start, n)`。
- 每条 K 的视图推导：`engine.py:719-726`
  - `signal_df = bars_signal.iloc[: i + 1]`
  - `now = _ts_to_dt(signal_df.index[-1])`
  - `trail_df = bars_trail[bars_trail.index <= signal_df.index[-1]]`（无止损 K 时为 `None`）
  - `trend_df = _trend_frame_for_signal(bars_trend, signal_df.index[-1], trend_tf)`
  - `px = last_ohlc_price(signal_df, "close")`，取不到就 `continue`
- 下一根开盘价：`engine.py:836` `_next_open_price(bars_signal, i, tf)`，仅当 `fill.on_next_open` 为真；实现在 `src/backtest/exit_policy.py:40-52`。
- 起点/终点：`loop_start = max(_range_start_index(...), warmup)`（`engine.py:618-624`），`n = len(bars_signal)`。

## 3. 设计

新增 `src/backtest/bar_cursor.py`，只依赖 pandas 与现有纯函数，不 import engine，避免循环依赖。

```python
@dataclass(frozen=True, slots=True)
class BarView:
    index: int                    # 当前 K 在 bars_signal 里的位置
    signal: pd.DataFrame          # 截至当根（含）的历史窗口，等价 iloc[:i+1]
    now: datetime                 # 当根收盘时间
    px: float                     # 当根收盘价
    next_open: float | None       # 下一根开盘价；最后一根或关闭下一根成交时为 None
    trail: pd.DataFrame | None    # 止损周期窗口，按当根时间对齐
    trend: pd.DataFrame | None    # 大周期窗口，按当根时间对齐
    is_last: bool

class BarCursor:
    def __init__(self, signal, trail, trend, *, timeframe, trend_timeframe,
                 start, end=None, fill_next_open=True) -> None: ...
    def indices(self) -> range: ...           # range(start, end)
    def at(self, i: int) -> BarView | None: ...  # px 取不到返回 None（对应原 continue）
    def __len__(self) -> int: ...
```

约束：

- `at()` 内部必须沿用现有表达式与函数（`_ts_to_dt`、`last_ohlc_price`、`_trend_frame_for_signal`、`_next_open_price`），不重写取值逻辑。
- 止损/大周期窗口继续用现有的整段布尔过滤，不做滑动窗口优化（性能优化不在本轮）。
- 所有切片保持 `iloc` 视图语义，不 `copy()`。
- `next_open` 在 `at()` 里一次算好，纯函数，提前计算不改变结果。

主循环改为：

```python
cursor = BarCursor(bars_signal, bars_trail, bars_trend, timeframe=tf, trend_timeframe=trend_tf,
                   start=loop_start, fill_next_open=fill.on_next_open)
for i in cursor.indices():
    view = cursor.at(i)
    if view is None:
        continue
    signal_df, now, px = view.signal, view.now, view.px
    trail_df, trend_df, next_open = view.trail, view.trend, view.next_open
    ...
```

原 `engine.py:836` 的 `next_open = ...` 一行删除，其余循环体不动（变量名保持不变，减少 diff）。

## 4. 非目标

- 不把开仓/平仓/账本搬进游标；`cash`/`pos`/`trades`/`equity`/`limits`/`open_meta` 仍归 `run_backtest`。
- 不改 `while cursor.advance()` 的推进方式（留给 Stage4-4b，需另立规格）。
- 不动 optimizer、UI、`BarCursor` 之外的任何模块。
- 不做性能优化，不改变任何数值结果。

## 5. 回归方案

1. 新增 `tests/test_bar_cursor.py`：
   - 索引范围与 `len`；
   - `signal` 行数随 `i` 递增、`now` 等于当根索引；
   - 最后一根 `next_open is None`；`fill_next_open=False` 时恒为 `None`；
   - `trail` 不含晚于当根的数据；无止损 K 时为 `None`；
   - `trend` 与直接调用 `_trend_frame_for_signal(...)` 完全一致；
   - 收盘价缺失（NaN/空窗口）时 `at()` 返回 `None`。
2. 全量 `python -m pytest -q -p no:warnings`；`ruff check .`；`mypy src`。
3. 等价性人工核对：用同一配置、同一区间跑 `tools/run_backtest.py`，对比成交笔数、每笔开平仓价、最终权益与 `calc_performance` 指标；重构前后应逐项相同（记录在 tasks.md 备注）。
4. 确认 diff 不含 `optimizer.py`、UI、成交模型与止损策略逻辑。

## 6. 风险

| 风险 | 处理 |
|---|---|
| trail/trend 时间对齐口径被无意改变 | 直接复用原表达式，测试对照旧函数输出 |
| 最后一根 `next_open` 语义 | 保持 `_next_open_price` 原实现，测试覆盖 |
| `px` 缺失时跳过整根 | `at()` 返回 `None`，主循环 `continue` 行为不变 |
| 提前计算 `next_open` 影响顺序 | 纯函数、无副作用，且只在成交分支使用 |
| 循环体变量改名引入漏改 | 局部变量名保持不变，仅赋值来源改为 `view` |

## 7. 版本

纯重构 → 第三位：`44.6.10` → `44.6.11`。更新 CHANGELOG、README、审阅报告阶段 4 第 4 条。
