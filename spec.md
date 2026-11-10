# Spec — 策略优化参数按配置页重新规划（v9.31.0）

## 背景

用户要求：根据当前「配置」页的实际配置项重新规划策略优化参数网格；
把「止损设置」里的**通用止损**加进优化参数；「策略配置」中各策略适合优化的项目也全部加进网格。

现状（已确认）：

- `src/backtest/optimizer.py` 已把 `GRID_BODY_LIGHT` / `GRID_BODY` 改写，
  并新增了 `GRID_BODY_FULL`，但后续链路尚未接通：
  - `grid_for_engine("daily_body", "完整")` 仍返回标准网格，`GRID_BODY_FULL` 是死代码。
  - `PARAM_LABELS_CN` 缺少新增项的中文名。
  - `_apply_params` 不认识 `generic_stop.*` 这类带点的公共键。
  - `_factory_values` 缺少新键的出厂值，分数接近时的「靠近出厂」判断会退化。
- `tests/test_optimizer_holdout.py` 仍按旧网格断言（精简只有 3 个键、标准不超过 400 组等）。
- 配置页（`src/ui/app.py` / `src/ui/config_tab.py`）暴露、且适合优化的项目：
  - 公共：信号周期 `timeframe`、止损触发 `stop_mode`、交易时段 `session_scope`、
    通用止损 `generic_stop`（kind / lookback / sar_af_step / atr_period / atr_mult）。
  - 日内实体：`eth_minutes`、`eth_minutes2`、`stop_extra_ticks`、`skip_gap_ticks`、
    `max_box_ticks`、`max_chase_ticks`、`ema_period`、`ema_buffer_ticks`、`day_cutoff`、`night_cutoff`。
  - 趋势跟随：`entry_bars`、`atr_mult`；SuperTrend：`atr_period`、`multiplier`；
    单均线：`ma_period`、`reverse_on_cross`；双均线：`short_period`、`long_period`、`reverse_on_cross`；
    长线趋势：`daily_ma`、`max_adds`、`add_min_atr`、`reduce_extend_atr`。

## 目标

1. 「完整」深度真正启用 `GRID_BODY_FULL`，覆盖日内实体配置页里全部适合优化的项目。
2. 通用止损作为**公共优化项**接入所有非固定规则策略（日内实体、趋势跟随、SuperTrend、单均线、双均线、长线趋势）：
   - 精简：只比 `off` / `sar`；
   - 标准：四种都试（`off` / `sar` / `prior_extreme` / `chandelier`），参数沿用配置页当前值；
   - 完整：再叠加通用止损自己的参数（回看根数、SAR 步长、ATR 根数、ATR 倍数）。
3. 优化参数能写回配置页对应位置（`strategy.generic_stop` 等），写入结果与界面上看到的一致。
4. 分数接近时仍能正确判断「哪组更靠近出厂」，不会把通用止损项当成「无出厂值」而一律加 1 分距离。
5. 网格说明（`describe_grid`）能显示新增参数的中文名与理论组合数。

## 非目标

- 不改任何策略的进场/止损判断逻辑，只改「搜索哪些参数、如何写回」。
- 不新增策略、不改回测撮合与风控规则。
- 不为早盘箱体A（`morning_box_a`）增加搜索项，它继续按出厂规则跑。
- 不把仓位规模类项目（`unit_volume`、手数、资金）放进网格，避免用放大仓位来「优化」收益。

## 变更设计

### S1 网格常量与通用止损公共旋钮

文件：`src/backtest/optimizer.py`（常量区，`GRID_BODY_FULL` 之后）

1. 新增：

```python
# 通用止损：任何策略都能叠加的一层止损（配置页「止损设置 → 通用止损」同一处）。
# 精简只比关/SAR；标准四种都试；完整再把通用止损自己的参数放进来。
GENERIC_STOP_KIND_LIGHT: list[Any] = ["off", "sar"]
GENERIC_STOP_KIND: list[Any] = ["off", "sar", "prior_extreme", "chandelier"]
GENERIC_STOP_PARAMS: dict[str, list[Any]] = {
    "generic_stop.lookback": [10, 20, 40],
    "generic_stop.sar_af_step": [0.01, 0.02],
    "generic_stop.atr_period": [14, 20],
    "generic_stop.atr_mult": [2.0, 3.0],
}

def _with_generic_stop(
    grid: dict[str, list[Any]],
    kinds: list[Any],
    *,
    full: bool = False,
) -> dict[str, list[Any]]:
    """把通用止损接到任一策略的网格上。返回深拷贝，不改原常量。"""
    out = copy.deepcopy(grid)
    out["generic_stop.kind"] = list(kinds)
    if full:
        out.update(copy.deepcopy(GENERIC_STOP_PARAMS))
    return out
```

2. 从 `GRID_BODY_LIGHT` / `GRID_BODY` 中删除 `"generic_stop.kind"`，
   从 `GRID_BODY_FULL` 中删除四个 `generic_stop.*`；
   保留 `GRID_BODY_FULL` 的日内实体扩展项：
   `stop_extra_ticks`、`skip_gap_ticks`、`max_box_ticks`、`max_chase_ticks`、`ema_period`、`ema_buffer_ticks`。
3. 更新网格注释的组合数：
   - 日内实体：精简 24 组、标准 2592 组、完整约 398 万组（均再被「最多试几组」抽样）。
   - 其它策略：精简/标准/完整均含通用止损；完整 = 标准网格 + 通用止损参数。

### S2 `grid_for_engine` 三种深度接线

文件：`src/backtest/optimizer.py`（`grid_for_engine`）

对 `trend_follow`、`supertrend`/`st`、`dual_ma`/`ma_cross`、`daily_body`、`swing_trend` 按同一规则返回：

| 深度 | 返回 |
|---|---|
| 精简 | `_with_generic_stop(GRID_*_LIGHT, GENERIC_STOP_KIND_LIGHT)` |
| 标准 | `_with_generic_stop(GRID_*, GENERIC_STOP_KIND)` |
| 完整 | `_with_generic_stop(GRID_*（日内实体用 GRID_BODY_FULL）, GENERIC_STOP_KIND, full=True)` |

`_FIXED_RULE_ENGINES` 与未知引擎仍返回 `GRID_FIXED`（空网格，不搜参数）。

### S3 中文参数名

文件：`src/backtest/optimizer.py`（`PARAM_LABELS_CN`）

新增：

```python
"eth_minutes2": "日盘第二段ETH分钟（0=只开一次）",
"stop_extra_ticks": "箱沿止损外扩跳数",
"skip_gap_ticks": "跳空超过几跳不开（0=关闭）",
"max_box_ticks": "箱体总宽上限跳数（0=关闭）",
"max_chase_ticks": "急破不追的跳数（0=关闭）",
"ema_period": "等回抽参考的EMA根数",
"ema_buffer_ticks": "EMA 有效打穿跳数",
"generic_stop.kind": "通用止损类型（off/sar/prior_extreme/chandelier）",
"generic_stop.lookback": "通用止损回看根数",
"generic_stop.sar_af_step": "SAR 加速步长",
"generic_stop.atr_period": "通用止损 ATR 根数",
"generic_stop.atr_mult": "通用止损 ATR 倍数",
```

### S4 写回配置：`_apply_params` 处理 `generic_stop.*`

文件：`src/backtest/optimizer.py`

1. 顶部导入：`from src.strategy.generic_stop import GenericStopConfig, normalize_generic_stop`。
2. 新增模块级常量 `_GENERIC_STOP_PREFIX = "generic_stop."` 与函数：

```python
def _apply_generic_stop(st: dict[str, Any], params: dict[str, Any]) -> None:
    """通用止损是公共设置：任何策略网格里的 generic_stop.* 都写到 strategy.generic_stop。"""
    raw = st.get("generic_stop")
    box = raw if isinstance(raw, dict) else {}
    for key, value in params.items():
        field = key[len(_GENERIC_STOP_PREFIX):]
        if field == "kind":
            box["kind"] = normalize_generic_stop(value)
        elif field in ("lookback", "atr_period"):
            try:
                box[field] = int(value)
            except (TypeError, ValueError):
                continue
        else:
            try:
                num = float(value)
            except (TypeError, ValueError):
                continue
            if num > 0:
                box[field] = num
    st["generic_stop"] = box
```

3. `_apply_params` 开头（深拷贝、解析 `engine` 之后、进入各策略分支之前）：

```python
generic = {k: v for k, v in params.items() if k.startswith(_GENERIC_STOP_PREFIX)}
if generic:
    _apply_generic_stop(st, generic)
    params = {k: v for k, v in params.items() if not k.startswith(_GENERIC_STOP_PREFIX)}
```

这样 `if/elif` 各分支只处理自己那一节的键，通用止损对任何引擎都生效。

### S5 `_factory_values` 补出厂值

文件：`src/backtest/optimizer.py`（`_factory_values`）

在 `values = dict(snap.get(section) or {})` 之后统一补：

```python
gs = GenericStopConfig()
values.update(
    {
        "generic_stop.kind": gs.kind,
        "generic_stop.lookback": gs.lookback,
        "generic_stop.sar_af_step": gs.sar_af_step,
        "generic_stop.atr_period": gs.atr_period,
        "generic_stop.atr_mult": gs.atr_mult,
    }
)
```

日内实体的 `skip_gap_ticks` / `max_box_ticks` / `max_chase_ticks` / `ema_period` / `ema_buffer_ticks`
出厂值来自 S6 的 `FACTORY_DEFAULT_SNAPSHOT`，不再单独硬编码。

### S6 出厂快照补齐日内实体项目

文件：`src/backtest/param_history.py`（`FACTORY_DEFAULT_SNAPSHOT["daily_body"]`）

改为与 `src/strategy/daily_body.py` 代码默认值一致：

```python
"daily_body": {
    "eth_minutes": 20,
    "eth_minutes2": 30,
    "stop_extra_ticks": 1,
    "skip_gap_ticks": 50,
    "max_box_ticks": 100,
    "max_chase_ticks": 20,
    "ema_period": 20,
    "ema_buffer_ticks": 2,
    "day_cutoff": "10:30",
    "night_cutoff": "22:15",
},
```

作用：优化「靠近出厂」判断有完整基准；「出厂默认」重置后这些项目显式回到代码默认值（行为不变）。

### S7 测试更新

文件：`tests/test_optimizer_holdout.py`

1. `test_daily_body_grid_stays_small` 改为：
   - `精简` 键集合 = `{"eth_minutes", "timeframe", "stop_mode", "generic_stop.kind"}`；
   - `eth_minutes == [15, 20, 30]`，`generic_stop.kind == ["off", "sar"]`；
   - `标准` 键集合 = 上述 + `eth_minutes2`、`day_cutoff`、`night_cutoff`、`session_scope`；
   - `generic_stop.kind == ["off", "sar", "prior_extreme", "chandelier"]`；
   - `完整` 键集合 = 标准 + 六个日内实体扩展项 + 四个 `generic_stop.*`；
   - 组合数：精简 ≤ 30，标准 ≤ 3000，完整 > 10 万（说明确实更大，靠抽样）。
2. `test_every_optimized_param_is_a_config_page_item` 的 `ui_covered` 增加：
   `eth_minutes2`、`stop_extra_ticks`、`skip_gap_ticks`、`max_box_ticks`、`max_chase_ticks`、
   `ema_period`、`ema_buffer_ticks`、`generic_stop.kind`、`generic_stop.lookback`、
   `generic_stop.sar_af_step`、`generic_stop.atr_period`、`generic_stop.atr_mult`；
   深度遍历改为 `("精简", "标准", "完整")`。
3. 新增 `test_generic_stop_params_write_under_strategy_root`：
   - `_apply_params(cfg, {"generic_stop.kind": "SAR", "generic_stop.lookback": "20", "generic_stop.atr_mult": 2.5}, "trend_follow")`
   - 断言 `st["generic_stop"]["kind"] == "sar"`、`lookback == 20`（int）、`atr_mult == 2.5`（float）；
   - 断言 `st["trend_follow"]` 里没有 `generic_stop.kind` 这种带点的键。
4. 新增 `test_factory_values_cover_generic_stop`：对每个非固定引擎，`_factory_values(engine)` 含
   `generic_stop.kind == "off"`，且日内实体含 `skip_gap_ticks == 50`。

### S8 版本与文档

1. `VERSION` → `9.31.0`。
2. `CHANGELOG.md` 顶部新增 9.31.0：
   - 「策略优化」按配置页重排：日内实体「完整」启用全部配置项；通用止损接入所有策略；
   - 优化参数写回 `strategy.generic_stop`；出厂快照补齐日内实体项目。
3. `README.md` 的「策略优化」段落：
   - 说明两种/三种深度的差别，新增参数写到哪一节；
   - 说明通用止损参数与配置页同一处，写入后界面同步。

## 验收标准

1. `python -m pytest -q` 全绿（基线 512，预计新增 2–3 条）。
2. 代码级：
   - `grid_for_engine("daily_body", "完整")` 含全部 S3 键，组合数约 398 万；
   - `grid_for_engine("trend_follow", "标准")` 同时含 `entry_bars`、`atr_mult`、`generic_stop.kind`；
   - `_apply_params` 对任何引擎都能把 `generic_stop.*` 写到 `strategy.generic_stop`，
     且不会残留在策略小节。
3. 界面级：
   - 优化页点「开始优化」前的网格预览（`describe_grid`）列出全部新键与中文名；
   - 选择「完整」时预览数量明显大于「标准」。
4. 手工验证：
   - 用 `fu2701`、5 分钟、最近可用区间，选「精简」跑一轮（最多 30 组），
     完成日志中每组参数都含 `generic_stop.kind`；
   - 写入优化结果后打开配置页，「通用止损」类型与参数已同步变化。
5. 版本号 `9.31.0`，CHANGELOG / README 已更新。

## 风险与回滚

- 网格变大后，固定抽样次数（默认 80）会被摊薄，单次优化对每个旋钮的覆盖变少；
  这是「完整」深度的预期行为，界面已有「最多试几组」与随机抽样提示。
- 通用止损接入所有策略后，老策略的搜索空间 ×2～×4，跑一轮耗时上升；「精简」仍保持小网格。
- 改动集中在 `optimizer.py` / `param_history.py` / 测试 / 文档；配置数据格式不变，
  回滚只需还原这几个文件，无需迁移历史版本 `data/param_history.json`。

## 声明

本规格待批准后执行；批准前不修改生产代码。
