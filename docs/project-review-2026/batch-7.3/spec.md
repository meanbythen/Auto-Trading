# 审查报告 §7.3 第三批（一致性：回测与实盘）规格

- 基线版本：49.20.4（2026-11-21）
- 范围来源：`docs/项目全面审查报告-2026-11-21.md` §4.2（B1~B8）、§7.3 验收表
- 目标：让同一份数据、同一套配置下，实盘扫描与回测的信号/止损/手数/费用口径可对齐，无法对齐的项在结果页和文档中明确标注。
- 原则：不引入循环依赖；不改 JSON 账本/SQLite 投影权威；不改用户未跟踪文档；不 commit / 不 push。

---

## 1. 决策摘要

| ID | 决策 | 是否改变回测/实盘结果 |
|----|------|------------------------|
| B1 | 实盘与回测的日线统一排除“当前交易日未收线”那一根 | 实盘日线策略信号可能变化；回测在盘中取到当日日线时也会排除 |
| B2 | 策略评估、通用止损的 trigger/touch 判断只用已收线 K 线 | 实盘盘中影线不再即时触发；止损响应最多延后一根信号周期 |
| B3 | 回测 `PositionSizer` 接入 `trade.ctp.max_order_volume`；成交模型保持已有参数化，补充与实盘对手价的差异说明 | 回测手数可能变小 |
| B4 | 不模拟项在回测结果页与快照中明确列出，不新增模拟逻辑 | 仅文案 |
| B5 | 清仓时点保持“K 线收盘成交”，在差异说明中标注 ≥15 分钟周期与实盘时钟的 5 分钟差 | 仅文案 |
| B6 | 回测按分笔开仓日期计算平今/平昨手续费，加仓生成新 lot，优先平今 | 跨日加仓后的平仓手续费可能变化 |
| B7 | 保证金比例改为可配置（默认仍 10%）；`chain_capital`、K 线根数差异写入说明 | 仅显式配置时改变收益率分母 |
| B8 | 复盘/质量报告明确标注“按点数、未扣手续费滑点”，不强行改为金额口径 | 仅文案 |

版本号：`49.21.0`（功能口径更新，回测净盈亏/成交笔数可能变化）。

---

## 2. B1 日线未收线

### 修改 `src/data/periods.py`

`drop_forming_bar(bars, *, now=None)` 增加可选参数 `period: str | None = None`：

- 当 `period is not None and canonical_period(period) == "D"`：
  - `cutoff = pd.Timestamp(futures_trading_day(now or datetime.now()))`；
  - 返回 `bars[bars.index < cutoff]`，即只保留交易日早于“当前期货交易日”的日线。
  - 日线索引为交易日 00:00（天勤 `frame_from_klines` 已 `normalize()`），因此周二 10:00 会排除周二 00:00 那根；周一 21:30 的期货交易日是周二，同样排除周二 00:00 那根。
  - 若过滤后为空，返回空 DataFrame（保持调用方现有空值处理）。
- 其它周期（分钟、小时、W）完全沿用现有“右标签 vs 墙钟”逻辑，避免改变周线语义。
- 新增 `from src.utils.trading_day import futures_trading_day` 导入；`data.periods -> utils.trading_day` 为允许方向。

### 修改调用方

统一传 `period`：

- `src/scheduler/runner.py::scan_symbol`
  - `closed = drop_forming_bar(bars, period=str(signal_tf))`
  - `trail_closed = drop_forming_bar(trail_bars, period=str(trail_tf))`
  - `trend_closed = drop_forming_bar(trend_bars, period=str(trend_tf))`
- `src/backtest/engine.py::_prepare_setup`
  - `bars_signal = unique_ohlc_columns(drop_forming_bar(bars_signal, period=tf))`
  - `bars_trail = unique_ohlc_columns(drop_forming_bar(bars_trail, period=trail_tf))`
  - `bars_trend = unique_ohlc_columns(drop_forming_bar(bars_trend, period=trend_tf))`

不修改 `src/screen/picker.py`、tick 合成、K 线落库等其它调用（它们处理的是分钟 K 或“是否写库”，不在本批范围）。

### 验收测试

- 日线索引 `[2026-11-19, 2026-11-20, 2026-11-21]`，`now=2026-11-21 10:00`、`period="D"` → 只留 19、20。
- 同一数据 `now=2026-11-22 10:00`（周六，期货交易日为周一）→ 三根全留（21 日已收线）。
- 同一数据 `now=2026-11-21 21:30`、`period="D"` → 只留 19、20（21 日属于正在形成的下一交易日）。
- 不传 `period` 时行为与旧测试 `tests/test_periods.py` 完全一致。

---

## 3. B2 trigger/touch 只用已收线

### 修改 `src/scheduler/runner.py::scan_symbol`

删除以下逻辑：

- `ENTRY_MODE_TRIGGER` 的惰性导入与 `entry_trigger` 计算；
- `use_forming_for_entry` / `use_forming_for_stop`；
- 把 `forming` 拼到 `eval_bars` 的 `pd.concat` 分支。

改为：

- `eval_bars = closed`（`closed` 已是 `drop_forming_bar(..., period=signal_tf)`）；
- `eval_trail = trail_closed`；
- `eval_trend = trend_closed`。

通用止损：

- `apply_generic_stop(..., gs_bars, forming=None, ...)`：只传已收线 K 线；删除 `gs_forming` 变量。
- 保留 `forming` / `trail_forming` 用于：
  - `health["forming_bar"]` 与 `bar_forming` 告警；
  - 启动回溯 `find_catch_up_open(..., forming=forming)`（回溯本身不改）。
- `entry_mode=trigger` 的含义变为“在某根已收线 K 线的范围内触发”，不再读取盘中未收线影线。

### 验收测试

- 构造一根已收线 K + 一根未收线 K（后者低点跌破多单止损）：
  - 用 `tests/test_runner_guard.py` 的轻量 bot/假行情模式跑 `scan_symbol`；
  - 断言 `_evaluate_symbol` / 通用止损收到的 DataFrame 最大索引等于已收线那根；
  - 断言未收线的破位影线不产生平仓决策。
- 现有 `test_runner_guard.py`、`test_backtest_*` 全部通过。

---

## 4. B3 回测单笔上限与成交说明

### 修改 `src/backtest/limits.py::BacktestLimits.__init__`

新增读取：

```python
ctp = (cfg.get("trade") or {}).get("ctp") or {}
raw = ctp.get("max_order_volume")
self.max_per_order = None
if raw not in (None, ""):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 0
    if value > 0:
        self.max_per_order = value
```

### 修改 `BacktestLimits.size_open`

`self.sizer.calc_open(..., max_per_order=self.max_per_order)`。

- 与实盘 `src/scheduler/order_executor.py::_size_open` 的 `order_volume_cap(trader)` 语义一致：只往小压，不放大。
- 不读取账号档案（回测没有登录账号）；档案上限只在实盘链路生效。

### 修改 `src/backtest/fill.py::FillModel.describe`

在说明末尾补一句：

> 实盘按对手价/盘口成交并受单笔上限约束；回测按上述开盘/收盘参考价加减固定跳数。

不新增成交模型参数，不修改已有默认值（避免历史成绩无谓漂移）。

### 验收测试

- `cfg.trade.ctp.max_order_volume=5`，monkeypatch `limits.sizer.calc_open` 捕获 `max_per_order`，断言为 5。
- 未配置时 `max_per_order is None`。
- 现有 `test_backtest_*` 通过；如某用例手数因上限变化，按新口径修正断言并在用例名中说明“受单笔上限约束”。

---

## 5. B4 回测不模拟项标注

### 修改 `src/backtest/engine.py::_params_snapshot`

新增快照字段 `live_gap_note`（字符串），固定包含：

1. 未模拟：组合敞口限额、行情健康/过期拒单、信号去重、账本暂停/发单暂停、排队与拒单、网络延迟、部分成交分布。
2. 保证金按 `margin_rate` 固定估算（见 B7）。
3. `chain_capital` 当前开关状态：开=手数随剩余权益；关=固定起始资金（净值曲线仍随盈亏）。
4. K 线长度：实盘扫描默认 `data.live_max_bars`（默认 200），回测默认 `backtest.max_bars`（默认 1200）。
5. 清仓时点：≥15 分钟周期以该根收盘（如 15:00）成交，实盘按时钟 14:55/22:55。

### 修改 `src/ui/backtest_actions.py`

在结果文本 `fill_note` 之后插入：

```python
f"口径差异  {snap.get('live_gap_note')}"
```

仅当字段非空时输出，避免旧结果文件出现空行。

不修改回测计算逻辑；不新增组合敞口/健康/去重模拟。

### 验收测试

- 跑一次 `test_backtest_smoke.py` 级别的回测，断言 `params_snapshot["live_gap_note"]` 同时包含“组合敞口”“保证金”“K线”“清仓”。
- 旧 `data/backtest_last.json`（无该字段）加载不报错。

---

## 6. B5 清仓时点说明

不改 `is_flatten_time` / `flatten_manager` 的计算：

- 实盘：系统时钟到达品种表清仓钟点（14:55/22:55/02:25）后平仓。
- 回测：在该钟点所在的已收线 K 线上按收盘价平仓；5 分钟周期与实盘一致，≥15 分钟周期会落在该根收盘（如 15:00），差 5 分钟。

`live_gap_note` 记录该差异。新增一条回归用例：15 分钟 K 线、`flatten_times=["14:55"]`，断言最后一笔平仓发生在 15:00 那根且成交价为该根收盘，用于锁定“已知差异”。

---

## 7. B6 回测平今判断按分笔 lot

### 数据结构

`run.open_meta` 新增 `lots`：

```python
"lots": [{"time": "2026-11-20T09:05:00", "volume": 1}]
```

- 首次开仓：写入当前 lot。
- 同向加仓（`is_add` 且已有 `open_meta`）：追加 lot；`entry_time` 继续保留**最早一笔**的时间（用于成交明细展示），不改写为加仓时间。
- 老数据/手工构造的 `open_meta` 没有 `lots` 时，回退为单个 lot：`entry_time` + `open_volume`。

### 修改 `src/backtest/engine.py::_net_pnl_after_fees`

- 开仓费仍按手数比例分摊：`open_share = open_fee * cut / held`。
- 平仓费改为按 lot 计算：
  - 从**最新** lot 开始消耗（优先平今，符合保守费用口径）；
  - 每个 lot 的 `close_today = is_close_today(lot["time"], exit_time)`；
  - `close_fee = sum(fee_close(contracts, symbol, lot_cut, close_today=...))`。
- 扣减 lot 后：
  - 删除空 lot；
  - `open_volume` 扣减；
  - `entry_time` 更新为剩余最早 lot 的时间；全部平掉时保持原值（调用方随后清空 `open_meta`）。
- 函数签名不变；`optimistic_meta` 仍用 `copy.deepcopy`，避免影响正式 lot。

### 修改 `src/backtest/engine.py::_open_position`

- 首次开仓的 `run.open_meta` 增加 `"lots": [{"time": entry_time.isoformat(...), "volume": open_vol}]`。
- 加仓分支：
  - 追加 `{"time": entry_time.isoformat(...), "volume": open_vol}`；
  - `entry_time` 保持首笔；
  - 保留现有均价、`open_fee`、`open_volume` 累加。

### 验收测试

- 合约费率：`commission=1`、`commission_close=10`、`commission_close_today=30`。
- 场景 A：第 1 日开 1 手，第 2 日加 1 手，第 2 日全平 → 平仓费 `10 + 30 = 40`。
- 场景 B：同上但只平 1 手 → 优先平今，平仓费 `30`，剩余 lot 为第 1 日 1 手。
- 场景 C：同一交易日内加仓后全平 → 两手均按平今 `30`，合计 `60`。
- 现有手续费/回测用例通过；若旧断言基于“单一 entry_time”，按新口径更新。

---

## 8. B7 保证金可配置与差异标注

### 修改 `src/backtest/engine.py`

- `_prepare_setup` 读取：

```python
raw = bt_cfg.get("margin_rate", MARGIN_RATE)
try:
    margin_rate = float(raw)
except (TypeError, ValueError):
    margin_rate = MARGIN_RATE
if not (0 < margin_rate <= 1):
    margin_rate = MARGIN_RATE
```

- 存入 `_BacktestSetup.margin_rate`（新增字段，默认 `MARGIN_RATE`）。
- `_params_snapshot`：
  - `"margin_used": peak_margin(run.trades, run.mult, setup.margin_rate)`
  - `"margin_rate": setup.margin_rate`
  - `live_gap_note` 写入“保证金按 X% 估算”。

- `chain_capital` 默认值、K 线根数不改，只写入 `live_gap_note`。

### 验收测试

- `cfg["backtest"]["margin_rate"]=0.12` 的回测，`params_snapshot["margin_rate"] == 0.12`。
- 未配置时为 0.10。
- 非法值（0、-1、2、"abc"）回退 0.10。

---

## 9. B8 复盘/质量口径标注

### 修改 `src/review/replay.py`

- 新增模块常量：

```python
REPLAY_COST_NOTE = (
    "复盘口径：按价格点数估算开平仓，不含手续费、滑点与合约乘数；"
    "净盈亏与回测/实盘金额口径不可直接相加。"
)
```

- 在 `replay_strategy` 成功产出开平仓明细后（`result.lines` 已有内容时），追加该说明；无 K 线/无法重放的分支不加。

### 修改 `src/review/quality.py`

- `build_quality_report` 在 `report.lines` 末尾（分笔明细之前）追加同一句说明：
  “质量指标按点数统计，不含手续费、滑点；与回测/实盘金额口径不可直接比较。”
- 不把 `pnl_points` 改为金额，不引入合约乘数/费率查询。

### 验收测试

- `replay_strategy` 产出的文本包含“不含手续费、滑点”。
- `build_quality_report` 正常成交场景的 `lines` 包含同一提示。
- 空成交场景仍只输出原有“没有可分析信号”提示（可同时带口径说明）。

---

## 10. 不改动项（本批明确排除）

- 不新增盘口、排队、拒单、延迟、部分成交分布模拟。
- 不模拟保证金强平/追保、平今可用量校验。
- 不修改周线处理、涨跌停近似、滑点常数模型。
- 不新增 K 线内容哈希、实盘成交参数快照（属 §4.4）。
- 不处理 §7.4 第四批（D6/D7/D9、C1/C3、文档过期表述）。

---

## 11. 验证与交付

依次执行：

```powershell
python -m pytest -q
lint-imports
python -m ruff check src tests
python -m mypy src
```

新增测试文件：`tests/test_review_7_3_consistency.py`，包含 B1、B3、B6、B7、B8 的纯函数/快照用例；B2、B5 用现有 runner/backtest 测试夹具扩展。

文档与版本：

1. `docs/项目全面审查报告-2026-11-21.md`
   - §0 增加“0.3 第三批修复复核（49.21.0）”，逐条写 B1~B8 结果与已知差异。
   - §4.2 表格的 B1/B2/B3/B6 标“已修”，B4/B5/B7/B8 标“已标注”。
   - §7.3 表末加“状态：49.21.0 已完成/已标注”。
2. `CHANGELOG.md` 顶部新增 `## 49.21.0 — 2026-11-21`，中文逐条说明上述口径变化，并明确“回测净盈亏/成交笔数可能变化”。
3. `README.md` 更新版本号（第 3 行与版本说明处）。
4. `VERSION` 单行写 `49.21.0`。

不 commit、不 push。

---

## 12. 需要确认

请确认：

1. 是否接受 B2 的取舍：实盘 trigger/touch 只认已收线，盘中影线不再即时触发止损/开仓。
2. 是否接受 B4/B5/B7/B8 以“结果页标注 + 文档说明”为主，不在本批新增模拟逻辑。
3. 是否接受版本号 `49.21.0`。
