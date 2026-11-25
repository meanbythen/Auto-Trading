# 触发即开仓（entry_mode=trigger）Spec（57.0.0）

## 1. 背景

配置页「开仓触发方式」有两档：`trigger`（触发即开仓）、`close`（收盘价确认，默认）。
当前两档**没有任何行为差异**：

- `src/strategy/types.py` 只定义/归一化了 `entry_mode`，没有分支消费它。
- `src/scheduler/runner.py` 约 L2157：「策略评估仍只用已收线 K 线：信号、开仓都以收盘为准」，forming 只用于 touch 止损。
- `src/backtest/engine.py` 约 L1711 只把 `entry_mode` 写进快照。
- 配置页提示写明「选哪档都等价收盘价确认」（`src/ui/config_tab_sections.py` 约 L34-41）。

用户已确认按「触发即开仓」方案实现，但要求先确认各策略的触发价定义。本 Spec 先固化触发价口径，再给出实现与验收。

## 2. 目标

1. `entry_mode=trigger` 时，空仓阶段用**正在走的 forming K 线**最高/最低价判断是否碰到触发价；碰到即返回开仓决策（实盘市价、回测按触发价成交），不再等收线。
2. `entry_mode=close` 的行为、历史回测结果完全不变。
3. 每个策略自己定义触发价；没有自然触发价的策略在 trigger 模式下**回退为收盘确认**，并在界面明确列出。
4. 实盘与回测同一口径：触发根、触发方向、成交价锚点一致。

## 3. 非目标

- 不改止损逻辑（touch 止损已在 54.0.0 完成）。
- 不改信号周期、指标算法、市场过滤、仓位/风控。
- 不为「无自然触发价」的策略强行推导触发价（例如解 EMA 交叉方程）。
- 不引入 tick 级撮合；forming 仍以 K 线级最高/最低近似。

## 4. 各策略触发价定义（本次确认的核心）

约定：
- 「收盘开仓条件」指当前 `evaluate` 里真正决定开仓的比较。
- 「触发价」指 trigger 模式下 forming 最高/最低需要碰到的价位；多单看 forming 最高价上穿，空单看 forming 最低价下穿。
- `forming` = 已收盘 K 线之后那根尚未收线的 K 线（runner `bars.iloc[len(closed):]`）。

| 引擎 | 文件 | 当前收盘开仓条件 | 触发价（trigger） | 盘中可判定性 | 本版结论 |
| --- | --- | --- | --- | --- | --- |
| daily_body 日内实体 | `src/strategy/daily_body.py` | 信号周期收盘 > 开盘箱体上沿做多 / < 下沿做空（约 L275-294、L1101-1109） | 多：`box_high`；空：`box_low`（`opening_box_levels`，ETH 走完后固定） | 可判定；跳空/箱宽/开仓次数等闸门照常先判 | 支持 |
| trend_follow 趋势跟随 | `src/strategy/trend_follow.py` | 收盘 > 前 `entry_bars` 根最高做多 / < 最低做空（L209-227） | 多：`entry_high`；空：`entry_low`（前 N 根，不含当根） | 可判定 | 支持 |
| supertrend | `src/strategy/supertrend.py` | 本根方向相对上一根翻转（L208-227） | 上一根已收盘算出的 SuperTrend 线 `st_now`；价格上穿转多、下穿转空 | 线值可判定；翻转后的新线要收线才定 | 支持 |
| dual_ma 双均线 | `src/strategy/dual_ma.py` | 短 EMA 上穿/下穿长 EMA（L193-195、L243-246） | 需解 `EMA_short(p)=EMA_long(p)`，随价格连续变化，无固定价 | 无固定触发价 | 回退收盘确认 |
| ema_swing 均线波段 | `src/strategy/ema_swing.py` | 三线排列 + 站上/跌破分界线 EMA + 倾角门槛（L350-403） | 排列是状态，单一价格无法表达 | 无固定触发价 | 回退收盘确认 |
| bollinger 布林突破 | `src/strategy/bollinger_break.py` | 收盘 > 中轨且 > 上轨做多 / < 中轨且 < 下轨做空（L65-86） | 多：`up`；空：`down`（轨道由已收盘 K 线算） | 可判定（价格 > up 必然 > mid） | 支持 |
| liq_stop 以损定开 | `src/strategy/liq_stop.py` | 先击穿止损区、再收回触发价（L149-187） | 多：`zone_h + entry_offset*ATR`；空：`short_l - entry_offset*ATR`；前提「本段曾击穿 zone_l/short_h」用 forming 极值补足 | 可判定（需 forming 的 low/high） | 支持 |
| kelly_break 凯利突破 | `src/strategy/kelly_break.py` | 收盘 > 前 N 根最高 / < 最低（L82-97） | 多：`high_n`；空：`low_n` | 可判定 | 支持 |
| sar_trend SAR波段 | `src/strategy/sar_trend.py` | SAR 翻向且收盘在 MA 同侧（L70-88） | 多：`max(sar, ma)`；空：`min(sar, ma)`（用上一根已收盘值） | SAR 值可提前算；MA 用上一根近似 | 支持 |
| multi_factor 多因子 | `src/strategy/multi_factor.py` | 加权打分穿阈值（L588-608） | 打分依赖收盘价与多项指标，无单一价格 | 无固定触发价 | 回退收盘确认 |
| swing_trend 长线趋势 | `src/strategy/swing_trend.py` | 大周期方向 + 回调后收盘回到均线一侧（L353-414） | 回调/确认是状态，无单一价格 | 无固定触发价 | 回退收盘确认 |
| swing_box 长线箱体 | `src/strategy/swing_box.py` | 收盘 > 箱体上沿 / < 下沿（L349-354） | 多：`formed.high`；空：`formed.low` | 可判定；量能闸门用 forming 当前累计量 | 支持 |
| dual_donchian 双周期通道 | `src/strategy/dual_donchian.py` | 大周期 bias + 小周期收盘 > up / < down（L86-97） | 多：`up`；空：`down`（`donchian_prior`） | 可判定 | 支持 |
| dual_fractal 双周期分型 | `src/strategy/dual_fractal.py` | 强分型 + MACD DIF 背离（L104-118） | 分型第一根 high/low 可作锚点，但 DIF 背离必须用当根收盘算 | 无固定触发价 | 回退收盘确认 |
| rule_dsl 自定义规则 | `src/strategy/rule_dsl.py` | 任意 YAML 条件（L2397-2402） | 规则可写任意指标，无统一触发价 | 无固定触发价 | 回退收盘确认 |

汇总：

- 支持 trigger：`daily_body`、`trend_follow`、`supertrend`、`bollinger`、`liq_stop`、`kelly_break`、`sar_trend`、`swing_box`、`dual_donchian`（9 个）。
- trigger 模式下自动回退 close：`dual_ma`、`ema_swing`、`multi_factor`、`swing_trend`、`dual_fractal`、`rule_dsl`（6 个）。

同一根 forming 同时碰到上下两个触发价时的定向规则：按 forming 最新价判断——最新价 ≥ 上触发价则做多，≤ 下触发价则做空，夹在中间则本轮放弃，等下一次扫描。

## 5. 具体改动

### 5.1 `src/strategy/types.py`

1. `StrategyDecision` 增加字段：

```python
trigger_price: float | None = None
```

语义：trigger 模式下实际触发价；close 模式为 None。开仓决策时 `entry_price` 与 `trigger_price` 相同。

2. 新增纯函数：

```python
def entry_touched(*, side: str, trigger: float | None, high: float, low: float) -> bool:
    """trigger 模式下 forming 最高/最低是否碰到触发价。"""
```

- `trigger` 为空或 ≤0 → False；
- `long`：`high >= trigger`；
- `short`：`low <= trigger`；
- 其它方向 False。

3. 新增纯函数（供各策略复用）：

```python
def forming_price_range(forming: Any) -> tuple[float, float] | None:
    """取 forming 最后一根的最高/最低；空数据返回 None。"""
```

4. 新增纯函数：

```python
def pick_breakout_side(*, close: float, high_level: float, low_level: float,
                       high: float, low: float) -> str | None:
    """同一根 forming 同时碰到上下沿时，按最新价定向。"""
```

### 5.2 `src/strategy/trend_base.py`（bollinger / kelly_break / sar_trend / liq_stop / multi_factor 共用）

1. `evaluate` 增加关键字参数 `forming: Any = None`，并在开头保存：

```python
self._forming = forming if forming is not None and len(forming) else None
```

2. 空仓开仓时，`bar_id` 改为：

```python
bar_id = str(self._forming.index[-1]) if self._forming is not None else str(bar.index)
```

3. `entry_signal` 返回的 `meta` 允许携带 `trigger_price`。`evaluate` 组装决策时：

```python
trigger = meta.get("trigger_price")
...
return StrategyDecision(
    action, symbol, reason,
    stop_price=stop,
    entry_price=float(trigger or close),
    trigger_price=float(trigger) if trigger else None,
    meta={"signal": self.section, "bar_id": bar_id, **meta},
)
```

4. `multi_factor` 不设置 `trigger_price`，因此 trigger 模式下仍按收盘确认（回退）。

### 5.3 各支持策略的触发价

#### `bollinger_break.py`（`entry_signal`）

- `forming` 存在时：
  - `high >= up` → 做多，`entry = up`，`stop = up - pad`，`meta["trigger_price"] = up`；
  - `low <= down` → 做空，`entry = down`，`stop = down + pad`，`meta["trigger_price"] = down`；
  - 否则返回 None。
- `forming` 为空时保持原收盘逻辑。

#### `kelly_break.py`（`entry_signal`）

- `forming` 存在时：`high >= high_n` 做多、`low <= low_n` 做空；`entry` 用对应触发价，止损按 `entry ± stop_atr*atr`。

#### `sar_trend.py`（`entry_signal`）

- `forming` 存在时：
  - 上一根为空头（`not ind["bull"]`）且 `high >= max(sar, ma)` → 做多，`entry = max(sar, ma)`；
  - 上一根为多头且 `low <= min(sar, ma)` → 做空，`entry = min(sar, ma)`；
  - 止损仍取 `sar`（多单 `min(sar, entry)`，空单 `max(sar, entry)`）。

#### `liq_stop.py`（`entry_signal`）

- `forming` 存在时，把 forming 的最高/最低并入 `recent_high` / `recent_low`：
  - 多：`recent_low < zone_l` 且 `forming_high > zone_h + offset*atr` → `entry = zone_h + offset*atr`；
  - 空：`recent_high > short_h` 且 `forming_low < short_l - offset*atr` → `entry = short_l - offset*atr`；
  - 止损沿用 `zone_l - stop_atr*atr` / `short_h + stop_atr*atr`。

#### `trend_follow.py`（`evaluate`）

1. 签名增加 `forming: Any = None`。
2. 空仓路径：`forming` 存在时用 `high >= entry_high` / `low <= entry_low` 触发；`entry` 用触发价，止损按 `entry ± atr_mult*atr`。
3. 同时碰到上下沿时用 `pick_breakout_side`。
4. `bar_id` / `_signaled_bar` / `_pending["bar_id"]` 用 forming 的索引。

#### `supertrend.py`（`evaluate`）

1. 签名增加 `forming: Any = None`。
2. 空仓路径：`forming` 存在时
   - `dir_now < 0 and high >= st_now` → 做多，`entry = st_now`；
   - `dir_now > 0 and low <= st_now` → 做空，`entry = st_now`。
3. 止损仍传 `st_now`；翻转当根风险距离为 0 时沿用 `_open` 现有的 pad 兜底。
4. `bar_id` 用 forming 索引。

#### `daily_body.py`（`evaluate`）

1. 签名增加 `forming: Any = None`。
2. 新增 `_direction_signal_trigger(bars, forming, now, session_open, eth_minutes, symbol, second)`：
   - 复用 `opening_box_levels` 画箱体；
   - ETH 未走完、已过开仓截止钟点等闸门与收盘路径一致；
   - `forming_high >= box_high` → long；`forming_low <= box_low` → short；同时碰到用 `pick_breakout_side`。
3. trigger 模式下：
   - 跳过「最新已收盘 K 线必须在一根周期内」的时效检查（改为盯 forming）；
   - `entry = box_high / box_low`；
   - 急破闸门在触发点距离为 0，天然通过；`_chase_wait` 只保留给收盘路径；
   - `bar_id = str(forming.index[-1])`。
4. 收盘路径代码不改。

#### `swing_box.py`（`evaluate`）

1. 签名增加 `forming: Any = None`。
2. 空仓路径：`forming` 存在时 `high >= formed.high` 做多、`low <= formed.low` 做空，`entry` 用箱沿。
3. 量能闸门改用 forming 当前累计成交量（`forming.iloc[-1]["volume"]`）。
4. `bar_id` 用 forming 索引。

#### `dual_donchian.py`（`evaluate`）

1. 签名增加 `forming: Any = None`。
2. 空仓路径：bias 为 long 且 `high >= up` → 做多；bias 为 short 且 `low <= down` → 做空；`entry` 用 `up` / `down`。
3. `bar_id` 用 forming 索引。

### 5.4 `src/scheduler/runner.py`

1. 导入 `ENTRY_MODE_TRIGGER`、`entry_mode_from_cfg`。
2. 扫描主流程（约 L2087 取到 `leg` 之后）：

```python
entry_mode = entry_mode_from_cfg(self._strategy_cfg_for(symbol))
```

3. 调用 `_evaluate_symbol` 时传入（回溯补开仓保持 None）：

```python
decision = self._evaluate_symbol(
    symbol, eval_bars, trail_bars=eval_trail, trend_bars=eval_trend,
    now=..., forming=None if catch_up_used or entry_mode != ENTRY_MODE_TRIGGER else forming,
)
```

4. `_evaluate_symbol` 增加 `forming: Any = None` 参数；仅当 `_evaluate_accepts("forming", target)` 为真时放进 kwargs。
5. 发单成功后，若 `(decision.meta or {}).get("intraday_entry")`，记录事件：

```python
self._record_event("intraday_entry", symbol=symbol,
                   price=decision.trigger_price,
                   bar_id=str((decision.meta or {}).get("bar_id") or ""))
```

6. 改写约 L2157-2159 的注释：trigger 模式用 forming 判触发，close 模式仍只看已收线；止损口径不变。

### 5.5 `src/backtest/engine.py`

1. `_decide_bar`（约 L817）：
   - 读取 `entry_mode = entry_mode_from_cfg(run.cfg)`；
   - trigger 模式下把**当前这根**当 forming：`forming = signal_df.iloc[-1:]`；
   - 仅当策略 `evaluate` 签名接受 `forming` 时传入（模块级 `_accepts_kwarg`，用 `inspect.signature`，结果缓存在 `run` 上，避免每根反射）。
2. `_open_position`（约 L1119）在「下一根开盘」分支之前增加触发成交分支：

```python
triggered = bool(meta.get("intraday_entry")) and (decision.trigger_price or 0) > 0
if triggered:
    bar = signal_df.iloc[-1]
    ref = min(max(float(decision.trigger_price), float(bar["low"])), float(bar["high"]))
    if not (float(bar["low"]) <= float(decision.trigger_price) <= float(bar["high"])):
        ref = float(bar["open"])  # 跳空直接穿过触发价，按开盘价成交
    entry_ref, entry_ts = ref, run.bars_signal.index[ctx.i]
elif run.fill.on_next_open and next_open is not None:
    ... 原逻辑 ...
```

3. 触发成交时 `exec_idx = ctx.i`、`exec_bar = signal_df.iloc[-1]`，流动性与成交量按触发根计算；滑点仍走 `run.fill.buy/sell`。
4. 快照已记录 `entry_mode`，不新增字段；成交记录 `entry_time` 取触发根时间。

### 5.6 UI 文案

`src/ui/config_tab_sections.py` 约 L34-41 的提示改为：

> 触发即开仓：盘中用未收线 K 线的最高/最低碰到触发价就市价开仓，不等收线。
> 支持：日内实体、趋势跟随、SuperTrend、布林突破、以损定开、凯利突破、SAR波段、长线箱体、双周期通道。
> 其余策略（双均线、均线波段、多因子、长线趋势、双周期分型、自定义规则）没有固定触发价，仍按收盘价确认。

## 6. 状态与去重

- 触发开仓使用 forming 的 K 线时间作为 `bar_id`，写入策略 `_signaled_bar` 与 `_pending["bar_id"]`；同一根 forming 只开一次。
- 收线后策略看到该 `bar_id` 已处理，不会按收盘再开一次；下一根 forming 正常继续。
- 发单失败沿用现有 `clear_signaled_bar` 逻辑，可在同一根重试。
- 开仓当根仍按 `_last_entry_bar` 跳过自身影线止损（各策略现有逻辑），避免触发当根立刻被止损。

## 7. 测试

新增 `tests/test_intraday_entry.py`：

1. `entry_touched`：多/空/空触发价/负触发价。
2. `pick_breakout_side`：只碰一边、同时碰两边按最新价、夹在中间返回 None。
3. `trend_follow`：forming high 碰到前 N 根高点 → BUY_OPEN，`entry_price == trigger_price == entry_high`，`meta["intraday_entry"] is True`。
4. `trend_follow` close 模式（`forming=None`）：收盘未破 → HOLD，破 → BUY_OPEN，价格用收盘。
5. `bollinger` / `kelly_break` / `sar_trend` / `supertrend` 各 1 例触发成功 + 1 例未触发。
6. `liq_stop`：forming low 补足击穿 + forming high 收回触发价 → 做多。
7. `dual_donchian`：bias=long 且 forming high ≥ up → 做多。
8. `swing_box`：forming high ≥ 箱体上沿且量能够 → 做多。
9. `daily_body`：ETH 结束后 forming high ≥ box_high → 做多；ETH 未结束 → HOLD。
10. 不支持的引擎（`dual_ma`）`evaluate` 不接受 `forming`，runner 回退 close（用签名检查断言）。
11. runner：`entry_mode=trigger` 时 `_evaluate_symbol` 传入 forming；`close` 时不传。
12. 回测：trigger 成交价 = 触发价（含滑点）、成交时间 = 触发根；跳空穿过触发价时按开盘价；close 模式仍按下一根开盘。

定向回归：

```bash
pytest tests/test_intraday_entry.py tests/test_intraday_stop.py tests/test_backtest_fill.py tests/test_runner_guard.py -q
```

全量与门禁：

```bash
python tools/testkit.py baseline
python tools/testkit.py gate --coverage
```

## 8. 版本与文档

- `VERSION` → `57.0.0`（开仓规则变更，大更新）。
- `CHANGELOG.md` 顶部新增 57.0.0：触发即开仓生效；列出 9 个支持引擎与 6 个回退引擎；标注需重启生效。
- `README.md` 版本号、用例数、开仓触发方式说明同步。
- `docs/TODO.md` 记录完成项。
- 两份架构 HTML 的版本号与行数同步。
- 测试方案/用例/报告同步版本、用例数与修订记录。
- `python tools/check_doc_versions.py` 通过。

## 9. 风险与回滚

- 风险 1：盘中假突破比收盘口径更早进场（用户已选择「触发即开仓」，属预期）。
- 风险 2：同一根 forming 反复扫描重复开仓 → `_signaled_bar` 用 forming 时间 + runner 现有同根去重。
- 风险 3：回测与实盘成交价差异 → 回测按触发价（跳空按开盘）+滑点，实盘市价；口径写入文档。
- 风险 4：6 个回退引擎用户以为已生效 → 配置页文案 + CHANGELOG 明确列出。
- 回滚：把配置改回「收盘价确认」即可；代码回滚只需停止传 `forming`（策略在 `forming=None` 时全部走原收盘逻辑），不需要改策略内部。

## 10. 验收标准

1. 第 7 节用例全过，原有 `test_intraday_stop.py` / `test_backtest_fill.py` / `test_runner_guard.py` 无回归。
2. 全量 `pytest` 通过；`python tools/testkit.py gate --coverage` 6 步通过。
3. `python tools/check_doc_versions.py` 通过，`VERSION == 57.0.0`。
4. 实盘手工验证（需重启）：选「触发即开仓」+ 支持引擎，盘中 forming 碰到触发价后下一次扫描（≤5 秒）出现 `intraday_entry` 事件与开仓单；选「收盘价确认」时仍只在收线后开仓。
