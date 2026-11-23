# 30/60 分钟 K 线按连续交易时间重拼

## 1. 背景与问题

当前 `bars_from_session_clock()`（`src/data/periods.py` L500-573）对每个交易时段
单独按墙上自然时钟收线，休市处截断：

- 商品 30 分钟日盘会多出一根 10:15 的 15 分钟短 K；
- 商品 60 分钟日盘出 6 根（10:00 / 10:15 / 11:00 / 11:30 / 14:00 / 15:00），其中 4 根只有 15～31 分钟；
- 天勤原生 30 分钟会直接丢掉 10:15 半根，现用 `_with_break_half_bar()`
  （`src/data/kline.py` L56-95）补丁掩盖，口径仍与同花顺（连续交易时间）不一致。

短 K 进入 EMA/ADX/ATR 后，指标窗口和“一根 K = 一个周期”的假设都被破坏，
信号与实盘报单所用 K 线口径不一致。

**已确认的实盘口径：** 30/60 分钟按连续交易分钟累计。
休息分钟（10:15-10:30、11:30-13:30）不计入，余量结转；夜盘、日盘两组各自重置，
不跨夜盘→日盘结转；尾盘不足整周期的半根保留、参与指标，但不允许触发新开仓。

## 2. 目标口径（验收表）

右标签（收盘时刻），1 分钟按“开盘那一分钟不算、收盘那一分钟算”。

| 场景 | 周期 | 应出现的右标签 | 每分钟数 |
| --- | --- | --- | --- |
| 商品日盘 09:00-10:15 / 10:30-11:30 / 13:30-15:00（225 分钟） | 30 | 09:30, 10:00, 10:45, 11:15, 13:45, 14:15, 14:45, 15:00 | 30,30,30,30,30,30,30,15 |
| 同上 | 60 | 10:00, 11:15, 14:15, 15:00 | 60,60,60,45 |
| 夜盘 21:00-23:00（120 分钟） | 30 | 21:30, 22:00, 22:30, 23:00 | 30×4 |
| 同上 | 60 | 22:00, 23:00 | 60×2 |
| au/ag 夜盘 21:00-02:30（330 分钟） | 60 | 22:00, 23:00, 00:00, 01:00, 02:00, 02:30 | 60×5, 30 |
| 同上 | 30 | 21:30, 22:00, 22:30, 23:00, 23:30, 00:00, 00:30, 01:00, 01:30, 02:00, 02:30 | 30×11 |
| CFFEX 09:30-11:30 / 13:00-15:00（240 分钟） | 30 | 10:00, 10:30, 11:00, 11:30, 13:30, 14:00, 14:30, 15:00 | 30×8 |
| 同上 | 60 | 10:30, 11:30, 14:00, 15:00 | 60×4 |
| 无夜盘品种（lh/jd 等） | 30/60 | 不得出现 21:00 之后的夜盘 K | — |
| 5 / 15 分钟 | 5/15 | 与改动前逐根一致 | 不变 |

组的定义：

- `day` 组：`start.hour < 18` 且 `start <= end` 的时段；组键 = 该自然日。
- `night` 组：`start.hour >= 18` 或跨零点的时段；凌晨（hour < 8）分钟归到前一天夜盘，
  组键 = 夜盘开盘日。
- 同一组内按实际 1 分钟行的时间顺序累计，`pos // step` 分桶，标签取桶内最后一分钟。
- 组与组之间清零，不结转。

## 3. 代码改动

### 3.1 `src/data/periods.py`

1. **`bars_from_trading_minutes(minutes, step, *, symbol="")`**：删掉“旧名字别名”，实现真正的连续累计：
   - 时间索引转北京时间、`floor("min")`、去重、排序；
   - 用 `trading_segments_for_symbol(symbol)` 计算每行的 `kind`（0=night，1=day）
     和 `base_days`（凌晨分钟为 -1），丢弃不在任何时段内的分钟；
   - `group = (日序号 + base_days) * 2 + kind`；
   - 组内 `cumcount` 得 `pos`，`bucket = pos // step`；
   - 聚合：open=first、high=max、low=min、close=last、volume=sum、oi=last、`minute_count`=size；
   - 索引取每组每桶最后一行的真实时间戳；
   - 空输入返回带 `minute_count` 的空表。
   - 性能：只做一次 `groupby`，不逐根 Python 循环（一年 1 分钟 < 1 秒）。

2. **`bars_from_minutes()`**（L646-695）：
   - `step <= 1` 原样返回；
   - `5 <= step < 30` 继续走 `bars_from_session_clock`（保证 5/15 逐根不变）；
   - `step >= 30` 改走 `bars_from_trading_minutes`；
   - 同步修改文档字符串（去掉“60 分钟日盘是 10:00、10:15、11:00、11:30、14:00、15:00”）。

3. **新增 `continuous_fill_minutes(ts, step, *, symbol="") -> int | None`**：
   给定右标签和周期，按交易时段推算该组从开盘到该标签为止的连续交易分钟数；
   找不到所属组返回 `None`。用于尾盘判定。

4. **新增 `partial_tail_bar(ts, period, *, symbol="") -> bool`**：
   `canonical_period` 非纯数字或 `step < 30` 返回 `False`；
   否则 `fill is not None and fill % step != 0`。
   （连续累计下只有组尾桶可能不满整周期，因此等价于“尾盘半根”。）

5. **`session_close_times()`**（L461-497）：
   - `step < 30`：保持现有“每段自然时钟”逻辑；
   - `step >= 30`：先按组生成期望右标签分钟序列（日盘用当日、夜盘从当日 21:00 起跨零点），
     每 `step` 根取一个标签，末尾不足一个周期也保留；`include_night=False` 不生成夜盘。

6. **`_drop_clock_hour_native_60()` → `_drop_conflicting_native(native, persist, step)`**：
   - 天勤/新浪原生 30/60 与重拼结果时间戳冲突时，删掉“已被重拼覆盖范围内、
     但不在重拼结果时间集合里”的原生 K；
   - 每天只处理 `[persist 当日首标签, 当日末标签]` 区间，区间外原生 K 保留，
     避免某天 1 分钟不全时把当天未覆盖的原生 K 误删。

7. **`overlay_minute_bars()`**（L723-779）：
   - `step >= 30` 调用 `_drop_conflicting_native`；`step < 30` 不调用；
   - 其余替换/落库逻辑不变。

8. **`src/data/bar_store.py` 的 `aligned_bars()`**（L175-230）：
   - 原来用 `hm % step == 0` 留 K，会把连续口径的 10:45、11:15、13:45、14:15、14:45
     全部误删，必须改；
   - `step >= 30` 改为用 `session_close_times(样本日, step, symbol, include_night=True)`
     算出的**允许收线时刻集合**过滤，旧的整点/半点时钟 K 一并清掉；
   - 5～29 分钟继续用整周期格子，行为不变。

### 3.2 `src/data/kline.py`

1. 删除 `_with_break_half_bar()`（L56-95）。
2. 删除 `KlineProvider._align_break_half_bar()`（L449-464）及 `fetch()` 中调用（L347）。
3. 更新 `_fetch_akshare()`、`_rebuild_from_local_minutes()` 等处的注释：
   30/60 改为“按连续交易分钟累计，休息不计入，尾盘半根保留”。

### 3.3 `src/strategy/decision_guard.py`

新增与 `apply_delivery_gate` 同风格的闸门：

```python
def apply_partial_tail_gate(
    decision: StrategyDecision,
    bars: pd.DataFrame | None,
    period: str,
    *,
    symbol: str = "",
    allow: bool = False,
) -> tuple[StrategyDecision, bool]:
```

- `allow=True`、`bars` 为空、或动作不是 `BUY_OPEN/SELL_OPEN` 时原样返回 `(decision, False)`；
- `partial_tail_bar(bars.index[-1], period, symbol=symbol)` 为真时返回
  `(StrategyDecision(HOLD, symbol, "尾盘不足整周期（半根），只参与指标，不开新仓"), True)`。

### 3.4 `src/scheduler/runner.py`

在交割闸门之后（L2149 `decision = gate_outcome.decision`）、`log_scan_brief` 之前：

```python
decision, partial_blocked = apply_partial_tail_gate(
    decision, closed, str(signal_tf), symbol=symbol, allow=catch_up_used
)
if partial_blocked:
    self._record_event(
        "partial_bar_block_open", symbol=symbol,
        bar_id=str(closed.index[-1]) if len(closed) else "",
        reason="尾盘半根不开新仓",
    )
    journal.record_audit(
        "signal_blocked", trace_id=str((decision.meta or {}).get("trace_id") or ""),
        symbol=symbol, message="尾盘半根不开新仓",
        payload={"gate": "partial_bar", "bar_time": signal_bar_time},
        root=self.cfg.get("_project_root"),
    )
```

- 平仓、通用止损不受影响；
- 启动回溯补开仓（`catch_up_used=True`）不加此闸门，避免补开历史信号被误挡。

### 3.5 `src/backtest/engine.py`

`_decide_bar()` 中 `decision = outcome.decision`（L898）之后、`run.limits.on_bar` 之前：

```python
decision, partial_blocked = apply_partial_tail_gate(
    decision,
    signal_df,
    run.tf,
    symbol=symbol,
    allow=bool((decision.meta or {}).get("rollover_carry")),
)
if partial_blocked:
    run.note_reject(
        "尾盘半根",
        "尾盘不足整周期，不触发新开仓",
        "多" if <原动作> == StrategyAction.BUY_OPEN else "空",
        at=now,
        price=px,
        stop=decision.stop_price,
        key="partial_bar_no_open",
    )
```

- 换月移仓（`rollover_carry`）放行，保持持仓连续性；
- 回测与实盘使用同一 `partial_tail_bar`，口径一致。

### 3.6 版本与文档

- `VERSION` → `51.0.0`（拼合口径变化，属大版本）。
- `CHANGELOG.md` 顶部新增 51.0.0：
  - 30/60 改为连续交易分钟累计，休息结转、夜间/日间分组重置；
  - 删除 50.12.0 的“补 10:15 半根”补丁；
  - 尾盘半根保留但不触发新开仓；
  - 5/15 逐根不变；
  - 提示：需重启程序、重新拉取/重拼 30/60 历史并重跑回测。
- `README.md`：更新 K 线周期/拼合说明中 30/60 的边界描述（如有）。

## 4. 测试

### 4.1 `tests/test_periods.py`

- 保留 5/15 现有测试不动（黄金基准）。
- `test_sixty_minute_bars_follow_session_clock` → 改为连续口径：
  `["10:00","11:15","14:15","15:00"]`，分钟数 `[60,60,60,45]`；
  overlay 后原生 11:00/12:00/13:00/14:00 不再残留。
- `test_thirty_minute_bars_do_not_span_tea_or_lunch` → 改为：
  `["09:30","10:00","10:45","11:15","13:45","14:15","14:45","15:00"]`，
  末根 15 分钟。
- `test_silver_sixty_keeps_0230_stub` → 增加 `["00:00","01:00","02:00","02:30"]`，
  02:30 为 30 分钟（夜盘共 330 分钟，60×5 + 30）。
- `test_session_close_times_sixty_matches_clock_bars` → 改为连续标签
  `["10:00","11:15","14:15","15:00"]`。
- 新增：夜盘 21:00-23:00 的 30/60；无夜盘品种 lh 不出现 21:00 后 K；
  `partial_tail_bar` 对 `15:00,30` 为 True、`14:45,30` 为 False、`15:00,5` 为 False。

### 4.2 `tests/test_kline_rebuild.py`

- 删除 `_with_break_half_bar` 的 4 个测试与 import。
- 新增：
  - 30/60 日盘边界与 `minute_count`；
  - au 夜盘 60 分钟含 02:30 尾桶；
  - 夜盘→日盘不结转（21-23 点 + 次日日盘，标签不混）；
  - 缺 1 根 1 分钟时桶边界仍按实际分钟累计；
  - 原生 60 分钟冲突时间被清理且覆盖区间外的原生 K 保留。
- 新增 `apply_partial_tail_gate` 单元测试：开仓→HOLD、平仓不受影响、
  `allow=True` 放行、5/15 放行。

### 4.3 `tests/test_bar_store.py`

- `test_aligned_bars_drop_lunch_and_old_ths_sixty` 改为连续口径：
  60 分钟只留 10:00、11:15、14:15、15:00；
- 新增 30 分钟：保留 10:45、11:15、13:45、14:15、14:45、15:00，
  清掉 10:15、11:00、14:00 等旧时钟 K。

### 4.4 验证命令

```powershell
python -m pytest tests/test_periods.py tests/test_kline_rebuild.py -q
python -m pytest -q
ruff check src tests
mypy src
```

## 5. 风险与兼容

- 本地库里已有的旧 30/60 K 线会被新结果替换，但仅限 1 分钟覆盖到的日期区间；
  区间外历史需重新拉取 1 分钟或重跑回测补齐。
- 指标、信号、回测收益会变化；本次不改任何策略规则。
- 5/15 分钟输出逐根不变，已有回测基准不受影响。
- 不提交代码，不修改用户配置。

## 6. 验收标准

1. 验收表所有标签与分钟数在单测中逐项通过。
2. 5/15 现有测试全部保持通过（无改动）。
3. `partial_tail_bar` 对 30/60 尾盘返回 True，对完整桶和 5/15 返回 False。
4. 实盘扫描日志出现 `partial_bar_block_open` 时，决策为 HOLD，平仓/止损仍可执行。
5. 全量 pytest、ruff、mypy 通过；VERSION=51.0.0，CHANGELOG/README 已更新。
