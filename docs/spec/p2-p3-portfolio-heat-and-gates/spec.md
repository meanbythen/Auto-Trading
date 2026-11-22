# P2 组合风险热度与在途预留 + P3 精简闸门 规格

基线版本：49.39.0（2026-11-22）
目标版本：49.40.0
范围来源：TODO 3.2「组合级热度与在途风险预留（P2）」及本轮 P3「精简闸门」要求。

原则：不改变平仓路径；所有新闸门只拦开新仓；默认配置保持现有回测成绩不变（新限额默认 0=不限）。

---

## 1. P2：组合风险热度与在途单预留

### 1.1 口径

- **组合风险热度（heat）**：组合内所有未平仓头寸的「到止损的最大亏损」之和，再叠加尚未成交的开仓预留。
  - 单笔风险现金 = `abs(entry_price - stop_price) × multiplier × volume`
  - 热度占权益比 = `heat / equity × 100`
  - 配置上限 `risk.portfolio.max_heat_pct`，0 表示不限。
- **在途预留**：开仓闸门通过时，把这一笔的风险现金记入内存预留，形成「还没成交也已占用热度」。
  - 预留生命周期 `risk.portfolio.pending_ttl_sec`（默认 60 秒），到期自动释放；成交时立即释放对应品种。
  - 预留只用于开仓；平仓不预留、不检查。
- **总手数敞口**：删除 `max_gross_lots` 与 `portfolio_gross_limit` 判断；`gross_lots()` 仅保留为快照展示字段，不再作为拦截条件。

### 1.2 `src/risk/portfolio.py`

`PortfolioLimits`：

```python
mode: str = "observe"          # off / observe / enforce
max_symbol_lots: int = 0       # 保留：品种净手数上限
max_heat_pct: float = 0.0      # 新增：组合风险热度上限（%权益）
pending_ttl_sec: float = 60.0  # 新增：在途预留有效期
```

- `from_config` 读取上述四个键；`max_gross_lots` 不再读取。
- 未配置任何上限时仍保持零开销路径，不读库。

`PortfolioSnapshot`：

- 新增 `PositionRow(book, symbol, side, volume, entry_price, stop_price)`。
- `books` 与 `net_lots/gross_lots/digest` 保持；新增 `rows` 保存原始行。
- 新增 `heat_cash(multiplier_of, fallback_per_position=0.0)`：
  - 逐行计算 `abs(entry-stop) × mult × volume`；
  - entry/stop 缺失或为 0 时，用 `fallback_per_position`（= 权益 × `risk_per_trade_pct`/100）代替，避免未知止损被当成零风险。

`check(...)`：

- 新参数：`entry_price`、`stop_price`、`multiplier`、`equity`、`pending_heat`、`fallback_risk`。
- 先判品种净手数上限（保留原逻辑）。
- 再判热度：`heat_budget = equity × max_heat_pct / 100`，`current_heat = snapshot.heat_cash(...) + pending_heat`；
  - `max_heat_pct > 0` 且 `current_heat + risk_cash > heat_budget` → `blocked=True`，`code="portfolio_heat_limit"`，原因写「组合风险热度 X 元 / 上限 Y 元」。
- `PortfolioDecision` 增加 `heat`、`heat_budget`、`risk_cash`，`gross` 仍保留用于留痕。

`PortfolioRisk`：

- 构造时读取 `cfg`，保留对 `cfg` 的引用以读取最新 `risk.capital`；用 `PositionSizer(cfg).multiplier_for(symbol)` 取乘数（无循环依赖）。
- 新增 `_pending: list[tuple[str, str, float, float]]`（symbol、side、risk_cash、expires_at）与 `threading.Lock`；时钟可注入（默认 `time.monotonic`）。
- `snapshot()` 返回 `PortfolioSnapshot | None`；`_read` 在连接或查询失败时返回 `None`，不再伪装成空快照。
- `check(...)`：
  1. `mode == "off"` 或非开仓 → 直接放行（`portfolio_off` / `portfolio_skip`）。
  2. 未配置任何上限 → 放行，不读库。
  3. 读快照失败，或 `max_heat_pct > 0` 但权益 ≤ 0 → 返回 `blocked=True, enforced=True`，`code` 分别为 `portfolio_unavailable` / `portfolio_equity_unknown`。**开仓失败关闭；平仓在步骤 1 已放行。**
  4. 清理过期预留，汇总 `pending_heat`，调用纯函数 `check`。
  5. 未超限且 `max_heat_pct > 0` → 追加一条预留（TTL 取 `pending_ttl_sec`）。
- 新增 `release(symbol: str, side: str = "") -> None`：成交或明确失败后释放该品种（可按方向）的预留。

### 1.3 `src/kernel/risk_chain.py`

`TradeIntent` 增加：

```python
entry_price: float = 0.0
stop_price: float = 0.0
```

只用于组合热度计算，不改变现有闸门顺序。

### 1.4 `src/scheduler/order_executor.py`

`_trade_intent` 从 `decision.entry_price` / `decision.stop_price` 填入新增字段。

### 1.5 `src/risk/service.py`

`evaluate_portfolio`：

- 调用 `portfolio.check` 时传入 `intent.entry_price`、`intent.stop_price`。
- 组合检查抛异常时：
  - 开仓且 `portfolio.limits.mode != "off"` → 记录一条 `portfolio` 拒绝（`code="portfolio_error"`）并返回 `(False, "portfolio_error", "组合风控检查异常，按开仓失败关闭")`；
  - 平仓或 `mode=off` → 维持放行。
- `on_order_filled`：开仓成交后调用 `portfolio.release(symbol, side)`（在 Guardian 更新之后）。

### 1.6 回测共享资金账 `src/backtest/portfolio.py`

`simulate_shared_capital`：

- 新参数 `max_heat_pct: float | None = None`。
- 每笔 item 增加 `"risk": projected_risk`；维护 `used_heat`。
- 预算 `heat_budget = capital × max_heat_pct / 100`（≤0 视为不限）。
- 接收顺序不变；在保证金判断之后增加热度判断：
  `used_heat + risk > heat_budget` → 拒绝，`reason="组合风险热度超限"`。
- 输出新增 `heat_budget`、`heat_peak`、`heat_peak_pct`；摘要行增加「组合热度峰值」。
- `run_portfolio_backtest` 读取 `risk.portfolio.max_heat_pct` 并传入。

---

## 2. P3：精简闸门

### 2.1 回测调用同一 Guardian

`src/backtest/limits.py`：

- `allow_open(now, volume, side="", symbol="")` 改为调用 `self.guard.allow(side, volume, symbol=symbol or None, now=now.timestamp())`。
- 保留 `volume <= 0` 与 `apply=False` 的短路。
- `on_open` 在 `guard.on_order_filled` 之后，把 `guard.last_order_ts` 改写为 K 线成交时间的 epoch，确保回测用 K 线时钟而不是墙上时钟。
- 删除重复的熔断/次数/间隔判断。

`src/backtest/engine.py`：开仓调用改为 `run.limits.allow_open(now, open_vol, side=decision.action.value, symbol=symbol)`。

### 2.2 ATR 硬暂停与健康硬停开

- 本轮**保留**，不删除、不改阈值：
  - ATR 硬暂停：`src/risk/dynamic.py` 的 `atr_pause_mult`；
  - 健康硬停开：`src/kernel/risk_chain.py` 的 `strategy_health`（由 `runtime.health_block_entry` 控制）。
- 对照方法写入 TODO：
  - ATR：`risk.dynamic.enabled=true/false` 或把 `atr_pause_mult` 调到极大，跑同一段回测比较；
  - 健康：仿真账号分别设 `runtime.health_block_entry=false/true`，比较被停开次数与错过的盈亏。
- 对照结论出来前不删代码。
- **后续（50.0.0）**：对照显示 ATR 硬暂停在 112 笔样本里一次未触发，已删除 `atr_pause_mult` 与界面「波动几倍暂停」，波动放大只保留减半；健康停开仍保留，等仿真账号 A/B。

### 2.3 固定手数降级为策略上限

`src/risk/sizing.py` `_intent_for_symbol`：

- 删除 `mode == "fixed"` 直接返回固定手数的分支。
- 仍先按 `risk_per_trade_pct`（或凯利）和止损距离算出风险手数；缺有效止损仍返回 0。
- 若 `mode == "fixed"`，最终 `intent = min(risk_lots, fixed_volume)`，说明写「固定手数上限 N 手」。
- 更新模块顶部说明与 `config.example.yaml` 注释：`fixed` 不再绕过风险算仓。

---

## 3. 测试

新增/修改：

1. `tests/test_risk.py`
   - `PortfolioLimits` 解析 `max_heat_pct/pending_ttl_sec`，不再解析 `max_gross_lots`。
   - 热度超限 enforce 拦截、observe 只留痕。
   - 读库失败 / 权益未知时开仓 fail-closed，平仓放行。
   - 两笔不同品种连续检查：第二笔因在途预留被拦；`release` 后恢复。
2. `tests/test_backtest_risk.py`
   - `fixed` 模式受风险上限约束，超过时取小；缺止损返回 0。
   - `allow_open` 与 Guardian 同口径（日亏、次数、间隔）。
3. `tests/test_portfolio.py`
   - 共享资金账：`max_heat_pct` 超限产生「组合风险热度超限」拒开，输出热度峰值。
4. `tests/test_batch2_p1_consistency.py`
   - `_read` 返回值改为可空，补断言。

全量验证：`ruff check src tests`、`mypy src`、`pytest -q`。

---

## 4. 文档与版本

- `VERSION`、`CHANGELOG.md`、`README.md`、`docs/TODO.md`、`docs/多品种实盘方案与实施规划.md`、`docs/配置说明书.md`、`docs/实盘交易简易说明书.md`、`docs/系统架构与演进方案.md` 同步到 49.40.0。
- 运行 `python tools/check_doc_versions.py`，直到版本统计一致。
- 不执行 git commit/push（用户未要求）。

## 5. 验收

1. 默认配置（`max_heat_pct: 0`）下回测与 49.39.0 结果一致。
2. `enforce` 且配置热度上限时，两笔合计超限的第二笔被拒；`observe` 只留痕。
3. 组合读库失败时开仓被拒、平仓正常。
4. 回测 `allow_open` 的熔断/次数/间隔与实盘 Guardian 同一实现。
5. `sizing_mode: fixed` 只能压低风险手数，不能无限开仓。
6. ATR 暂停与健康停开行为不变，文档写明对照方法。
