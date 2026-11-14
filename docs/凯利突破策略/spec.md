# 凯利突破策略接入规格（Spec）

## 1. 目标

在现有程序里新增一套「凯利突破」策略：开平仓只依赖价格突破和 ATR 止损距离，资金管理完全复用现有凯利 / 每笔风险仓位模块。纯新增策略模块并接入策略引擎、回测、优化交叉测试和配置页，不改公共风控口径。

## 2. 已确认的产品决策

1. 止盈是策略自己的开关：`take_profit=true` 用固定盈亏比止盈；`false` 只靠止损和收盘清仓离场。默认开，盈亏比 2.5。
2. 是否隔夜听公共配置「收盘清仓」开关（`strategy.flatten_at_close`），策略小节不单独加这个键。
3. 参数放在配置页新增的「凯利突破」面板里，不改公共配置页布局。
4. 手数不在策略里算：策略只返回止损价，手数继续由 `src/risk/kelly.py` + `src/risk/sizing.py` 决定。

## 3. 策略规则（实现口径）

| 项目 | 规则 | 默认 |
| --- | --- | --- |
| 做多开仓 | 当根收盘 > 最近 N 根（不含当根）最高价 | N=20 |
| 做空开仓 | 当根收盘 < 最近 N 根（不含当根）最低价 | N=20 |
| 止损 | 多：进场价 − ATR×K；空：进场价 + ATR×K | ATR=14，K=1.2 |
| 止盈 | 多：进场价 + 止损距离×盈亏比；空：进场价 − 止损距离×盈亏比；收盘到价即平 | 2.5 |
| 波动率过滤 | ATR/收盘价 < 阈值时不开新仓；0=关 | 0 |
| 出场方式 | 止损沿用公共「触及/收盘确认」设置；止盈按收盘价确认（回测也按收盘成交，保证实际盈亏比不低于目标） | — |
| 仓位 | 复用现有凯利/每笔风险/手数上限；策略不计算手数 | — |

实现继承 `src/strategy/trend_base.py` 的 `TrendStrategyBase`，复用会话、收盘清仓、持仓记账、止损触发、信号去重和回测成交逻辑。

## 4. 文件级改动

### 4.1 新增 `src/strategy/kelly_break.py`

- `class KellyBreakStrategy(TrendStrategyBase)`
  - `section = "kelly_break"`，`label = "凯利突破"`
  - `__init__`：调用 `super().__init__(cfg)`；读 `self.box` 的 `lookback`（≥5，默认 20）、`stop_atr`（>0，默认 1.2）、`take_profit`（默认 True）、`reward_risk`（>0，默认 2.5）、`min_atr_ratio`（≥0，默认 0）；并把 `flatten_at_close` 覆写为读 `strategy.flatten_at_close`（默认 True）。
  - `need_bars()`：`max(lookback + 1, atr_period + 1) + 5`
  - `prepare(bars)`：算 ATR（`self._atr`）、当根之前 N 根的最高/最低、`atr/close`；算不出来返回 `None`。
  - `entry_signal(...)`：先判波动率过滤（命中返回 `None`）；收盘突破 N 根高点返回 `("long", close - stop_atr*atr, 说明, meta)`，跌破低点返回空头；`meta` 带 `high_n/low_n/atr/atr_ratio`。
  - `exit_reason(...)`：`take_profit` 关时返回 `None`；多头 `close >= entry + (entry - initial_stop)*reward_risk` 返回「凯利突破止盈平多…」，空头对称。不做移动止损（不覆写 `trail_stop`）。

### 4.2 `src/strategy/factory.py`

1. `from .kelly_break import KellyBreakStrategy`
2. `_DEFAULT_ENGINE_SECTIONS["kelly_break"] = {"lookback": 20, "atr_period": 14, "stop_atr": 1.2, "take_profit": True, "reward_risk": 2.5, "min_atr_ratio": 0.0}`
3. `_CROSS_ENGINE_ORDER` 追加 `"kelly_break"`
4. `REGISTRY["kelly_break"] = KellyBreakStrategy`

### 4.3 `src/strategy/session_audit.py`

- `ENGINE_AUDIT_MODES["kelly_break"] = None`（策略用自己的波动率阈值，不接公共趋势过滤）
- `ENGINE_NAMES["kelly_break"] = "凯利突破"`

### 4.4 `src/ui/app_common.py`

- `ENGINE_LABELS["kelly_break"] = "凯利突破"`
- `ENGINE_CHOICES` 追加 `"kelly_break"`

### 4.5 `src/ui/app_state.py`

- `__init__` 和「重新载入配置」两处各新增变量：`kb_lookback_var`、`kb_atr_period_var`、`kb_stop_atr_var`、`kb_take_profit_var`（BooleanVar）、`kb_reward_risk_var`、`kb_min_atr_ratio_var`，初值从 `strategy.kelly_break` 读，合法范围与策略一致。
- `_apply_ui_to_cfg` 在布林参数块后写 `st["kelly_break"]`，填错回默认、超范围夹紧；写入后仍调用现有 `sanitize_strategy_config`。

### 4.6 `src/ui/config_tab.py`

- 在布林面板后新增 `_detail_panel("kelly_break", "凯利突破", ...)`，用现有 `_param_entry` / `_switch_row` / `_note_block`：
  - 突破回看根数（默认 20）
  - ATR周期（默认 14）
  - 止损ATR倍数（默认 1.2）
  - 固定盈亏比止盈开关
  - 目标盈亏比（默认 2.5）
  - 波动率下限 ATR/价（默认 0=关）
- 说明文字写明：只按收盘突破开仓、ATR 只算止损、手数用左边凯利/每笔风险、隔夜听公共「收盘清仓」。

### 4.7 新增 `tests/test_kelly_break.py`

用构造 K 线覆盖：

1. 收盘突破前 N 根高点 → `BUY_OPEN`，止损价 = 收盘 − 1.2×ATR；
2. 跌破前 N 根低点 → `SELL_OPEN`，止损价 = 收盘 + 1.2×ATR；
3. 未突破 → `HOLD`；
4. `take_profit=True` 时收盘越过目标 → `CLOSE`，原因含「止盈」；
5. `take_profit=False` 时越过目标不平 → `HOLD`；
6. `min_atr_ratio` 高于当前 ATR/价 → `HOLD`；
7. `flatten_at_close` 跟随 `strategy.flatten_at_close`（不读策略小节）；
8. `create_strategy({"engine": "kelly_break"})` 返回 `KellyBreakStrategy`，`list_engines()` 含 `kelly_break`，`sanitize_strategy_config` 补齐默认节。

## 5. 版本与文档

- `VERSION`：`44.1.2` → `44.2.0`（功能更新）。
- `README.md`：当前版本号三处同步为 `44.2.0`，策略列表补「凯利突破」及一句说明。
- `CHANGELOG.md` 顶部新增 `## 44.2.0 — 2026-11-14`，写清新增策略、参数、复用凯利仓位、默认不额外引入隔夜开关。

## 6. 验证

```powershell
python -m ruff check .
python -m mypy
python -m pytest -q
```

要求：ruff / mypy 无新增错误；pytest 全部通过（当前基线 909 项 + 新增用例）。

## 7. 不做的事

- 不改公共风控、凯利算法、仓位模块。
- 不在策略内重算手数、不新增全局风险单例。
- 不加滚动胜率统计（胜率/盈亏比继续由配置页手填，喂给现有凯利模块）。
- 不自动 git commit（等用户单独要求）。
