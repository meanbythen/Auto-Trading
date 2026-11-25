# 盘中触及止损（方案 C）Spec（54.0.0）

## 1. 背景与已确认结论

2026-11-25 夜盘实盘：`ag2702` 空 1 手 @8937，止损 9050.29，设置「止损触发方式 = 触及即止损」。

- 23:35–23:40 那根 5 分钟 `O=9031 H=9075 L=9013 C=9024`，最高价在 23:40 前就已越过 9050.29。
- 程序到 23:40:01.7 这根收线后才判定，23:40:02.7 报单、23:40:03.7 成交，实际晚了整整一根 K 线。
- 原因：`src/scheduler/runner.py` 的 `eval_bars = closed`（约 L2048），策略评估与止损判定都只看已收盘 K 线；未收线的那根只用于健康状态和启动回溯，注释也写明「不参与本轮决策」。
- 通用止损 `apply_generic_stop(..., forming=None, ...)`（约 L2136）同样没有用盘中那根，虽然 `GenericStopOverlay.check` 本身已支持 `forming` + `touch`。
- 回测侧核对结论：`src/backtest/engine.py` 的 `_close_position` 用 `stop_breached(side, pos_stop, fill_bar)` 判**当根**最高/最低，`exit_fill_price` 在 `touch_stop=True` 时按「止损价与当根开盘价中更差的一边 + 滑点」成交，成交时间取触发根（`now`）。即回测已经是「当根 high/low 触及 → 当根按止损价成交」，不需要改触发路径。

## 2. 目标

1. 实盘 `stop_mode = touch` 时，持仓期间用正在走的那根 K 线（forming）的最高/最低价对照已记录的止损价，触及即在本轮扫描（约 5 秒内）生成平仓决策，不再等收线。
2. 通用止损同样把 forming 传进去（其 `check` 已支持），配置关闭时行为不变。
3. 回测口径与实盘对齐并固化：新增回归用例证明「持仓 K 线的 high/low 触及止损 → 当根按止损价（含滑点）成交、成交时间为触发根」，不改引擎现有触发逻辑。
4. 不破坏现有约定：信号/开仓仍只用已收盘 K 线；止损价仍只由已收盘 K 线计算（forming 只用于「碰线」判定）。
5. 升版本 54.0.0，同步 CHANGELOG / README / VERSION / 文档与门禁。

## 3. 非目标

- 不改开仓触发方式（`entry_mode`）；本次只动止损。
- 不引入分笔/秒级撮合，不改变 trade 记录的时间粒度（仍为 K 线右标签时刻）。
- 不改 `GenericStopOverlay`、`exit_policy`、`bar_cursor` 的算法。
- 不改 `close`（收盘价确认）模式的语义：该模式仍只在收线后判定。

## 4. 口径定义（本次唯一口径）

| 场景 | 实盘 | 回测 |
| --- | --- | --- |
| 止损线怎么算 | 只用已收盘 K 线（策略 `trail_stop` / 通用止损 `_stop_level`） | 同左（`stop_before` / `stop_level`） |
| 用哪根 K 判触及 | 正在走的那根 forming（touch） | 当根已收盘 K（touch） |
| 成交价 | 扫描时的对手价/最新价（约等于止损价 ± 滑点） | `min(stop, bar.open)`（多）/`max(stop, bar.open)`（空）+ 滑点 |
| 成交时间 | 本轮扫描时刻 | 触发根右标签时刻 |

两条路径在「触发根」和「触发方向」上一致；成交价的差异是实盘只能市场成交、回测按止损价近似，已在既有口径内接受。

## 5. 具体改动

### 5.1 `src/scheduler/runner.py`：新增纯函数（模块级，便于单测）

在 `_eval_now` 之后新增：

```python
_INTRADAY_STOP_RETRY_SEC = 120.0


def _entry_dt(pos: Any) -> datetime | None:
    """程序账开仓时刻；解析不了返回 None（盘中判定按保守跳过处理）。"""


def _bar_start(bar_label: Any, period: str) -> datetime | None:
    """右标签 K 线的开始时刻；日线/周线或解析失败返回 None。"""


def _intraday_stop_decision(
    leg: Any,
    symbol: str,
    pos: Any,
    forming: Any,
    *,
    period: str,
) -> StrategyDecision | None:
    """持仓期间用还在走的那根 K 线判一次「触及止损」。命中返回 CLOSE，否则 None。"""
```

判定顺序（任一不满足即返回 `None`）：

1. `pos.side in {"long","short"}`；
2. `normalize_stop_mode(getattr(leg, "stop_mode", "touch")) == STOP_MODE_TOUCH`；
3. `forming` 非空；
4. `stop = float(pos.trail_stop or pos.initial_stop or 0) > 0`；
5. `_bar_start(forming.index[-1], period)` 有值（分钟周期才判）；
6. `_entry_dt(pos)` 有值且 `entry <= bar_start + 60 秒`（开仓那根若含较长一段开仓前行情就整根跳过；正常「收线后下一轮扫描成交」只差几秒，这根照判，与回测成交根一致）；
7. `stop_triggered(side=..., stop=stop, close/high/low=forming 最后一根, mode=STOP_MODE_TOUCH)` 为真。

命中时返回：

```python
StrategyDecision(
    StrategyAction.CLOSE,
    symbol,
    f"{leg.label}止损 {stop:.2f} 盘中触及离场",
    stop_price=stop,
    entry_price=float(pos.entry_price or 0),
    meta={"intraday_stop": True, "bar_id": str(forming.index[-1])},
)
```

### 5.2 `src/scheduler/runner.py`：`__init__` 增加去重标记

在 `self._dyn_cut_key` 一带新增：

```python
# 盘中止损：同一根正在走的 K 线只发一次平仓单（symbol -> (bar_id, monotonic)）。
self._intraday_stop_sent: dict[str, tuple[str, float]] = {}
```

### 5.3 `src/scheduler/runner.py`：接入扫描主流程（约 L2125–2147）

1. 通用止损调用改为传入 forming（仅非回溯时）：

```python
decision, gs_hit = apply_generic_stop(
    ...,
    gs_bars,
    flat=False,
    forming=None if catch_up_used else forming,
    mode=getattr(leg, "stop_mode", "touch"),
    cooldown_sec=120.0,
)
```

2. 通用止损之后、交割闸门之前追加盘中策略止损：

```python
if (
    decision.action == StrategyAction.HOLD
    and not catch_up_used
    and not self._orders_paused
    and not self._symbol_is_flat(symbol)
):
    intraday = _intraday_stop_decision(
        leg, symbol, self._position_for(symbol), forming, period=str(signal_tf)
    )
    if intraday is not None:
        bar_id = str(intraday.meta.get("bar_id") or "")
        last = self._intraday_stop_sent.get(symbol)
        too_soon = last is not None and last[0] == bar_id and (
            time.monotonic() - last[1] < _INTRADAY_STOP_RETRY_SEC
        )
        if not too_soon:
            decision = intraday
            self._record_event("intraday_stop", symbol=symbol, stop=intraday.stop_price, bar_id=bar_id)
```

3. 发单成功后登记并抑制收线重复：

在 `sent = bool(self._execute(decision))` 之后：

```python
if sent and (decision.meta or {}).get("intraday_stop"):
    bar_id = str((decision.meta or {}).get("bar_id") or "")
    self._intraday_stop_sent[symbol] = (bar_id, time.monotonic())
    note = getattr(leg, "note_flat_wait_next_bar", None)
    if callable(note) and bar_id:
        # 这根收线后不再让策略按同一根再发一次止损/再开新仓。
        note(symbol, bar_id)
```

说明：`note_flat_wait_next_bar` 会把该根写进策略的 `_signaled_bar` / `_last_entry_bar`，正好覆盖「平仓单未成交、收线后策略又发一次同根止损」与「同一根立刻补仓」两种情况；`_intraday_stop_sent` 覆盖收线前的重复扫描。发单失败（`False`）不登记，下一轮可重试。

### 5.4 注释与文档同步

- 删除/改写 L2052–2054 的错误注释（原话把回测描述成「只用已收盘 K 线、下一根成交」）；改为：策略评估用已收盘 K 线，盘中止损用 forming 判触及，回测按触发根 high/low 在当根按止损价成交。
- `src/backtest/engine.py` 的 `_close_position` 上方注释补一句：策略止损与通用止损都在**触发根**成交，不推到下一根；`close` 模式与主动离场才走下一根开盘。

### 5.5 回测：不改触发逻辑，只固化口径

依据第 1 节核对结论：`_decide_bar` 用 `stop_before`（上一根算出的止损）→ 策略在当根返回 CLOSE → `_close_position` 用 `stop_breached(当根)` + `exit_fill_price(touch_stop=True)` 在当根按止损价成交。本次不新增引擎级止损，避免与策略自身止损重复触发、改变全部历史回测结果。

## 6. 测试

新增 `tests/test_intraday_stop.py`（纯函数单测 8 例，无需登录/网络）：

1. 空头 `H=9075`、止损 9050.29 → CLOSE，`stop_price=9050.29`，`meta["intraday_stop"] is True`。
2. 空头 `H=9040`（未触及）→ `None`。
3. `stop_mode="close"` → `None`。
4. `forming` 那根开始时刻早于 `opened_at` 超过 60 秒（开仓根）→ `None`；
   只晚几秒（正常收线后成交）→ 照判 CLOSE。
5. 多头 `L <= stop` → CLOSE；多头 `L > stop` → `None`。
6. `trail_stop`/`initial_stop` 都为 0 → `None`。
7. 日线周期（`period="D"`）→ `None`。

`tests/test_backtest_fill.py` 补 1 例：

8. `stop_mode=touch`、止损由上一根算出、当根 `low` 打穿 → `exit_price == 止损价`（滑点 0）、`exit_time == 触发根时间`；再加一例带 1 跳滑点，验证 `exit_price` 比止损更差。

定向回归：

```bash
pytest tests/test_intraday_stop.py tests/test_backtest_fill.py tests/test_generic_stop.py tests/test_runner_guard.py -q
```

全量与门禁：

```bash
python tools/testkit.py baseline
python tools/testkit.py gate --coverage
```

## 7. 版本与文档（按项目固定流程）

- `VERSION` → `54.0.0`（止损规则变更，大更新）。
- `CHANGELOG.md` 顶部新增 54.0.0 条目（新增：盘中触及止损；口径：回测为触发根成交；风险提示：需重启生效）。
- `README.md` 版本号、用例数、止损行为描述同步。
- `docs/TODO.md` 记录本次完成项。
- 两份架构 HTML 的版本号与行数同步。
- `docs/软件测试/软件测试方案.md`、`docs/软件测试/软件测试用例.md`、`docs/测试报告/软件测试报告-2026-11-24.md` 同步版本、用例数、修订记录。
- `python tools/check_doc_versions.py` 通过。

## 8. 风险与回滚

- 风险 1：盘中影线假突破导致比收线口径更早离场（用户已明确选择「触及即止损」，属预期）。
- 风险 2：平仓单未成交时重复发单 → 已用 `_intraday_stop_sent`（同根 120 秒）+ `note_flat_wait_next_bar`（收线抑制）双重拦截。
- 风险 3：开仓当根影线误触 → 用「bar_start >= opened_at」整根跳过。
- 风险 4：回调/回退行情导致止损价失真 → 止损线仍只由已收盘 K 线算，forming 仅用于碰线；行情过期/回退的开仓闸门不变。
- 回滚：把 5.3 的调用块删除、`forming=forming` 改回 `None` 即可恢复原行为（功能开关不做配置项，避免半开状态）。

## 9. 验收标准

1. `_intraday_stop_decision` 单测 7 例全过；回测口径用例 2 例全过。
2. 全量 `pytest` 通过；`python tools/testkit.py gate --coverage` 6 步通过。
3. `python tools/check_doc_versions.py` 通过，VERSION=54.0.0。
4. 实盘手工验证（需重启生效）：持仓时把止损设在当前价前方，盘中越过止损后在下一次扫描（≤ 扫描周期）内看到 `intraday_stop` 事件与平仓单，而不是等收线。
