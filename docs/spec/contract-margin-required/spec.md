# 保证金比例缺失即拒绝开仓 Spec（55.0.0）

## 1. 背景与已确认结论

2026-11-26 实盘：`ag2702` 不在「品种与合约」表里，查不到该品种的保证金比例。

- `src/risk/sizing.py` 的 `margin_for` 在「无比例、无表内金额」时回落到 `risk.margin_per_lot`（配置页「兜底每手保证金(元)」，当时 33821.0）。
- `_lots_by_margin_cap` 因此算出「每手占用 33821 元」，按 80% 权益预算正常放行，最终仓位到 98.8%，等于绕过了仓位上限风控。
- 用户决定：删除「兜底每手保证金(元)」字段。品种表查不到该品种任何保证金数据（既无比例、也无每手保证金金额）时，**直接拒绝开仓并提示补比例**。
- 用户确认的两个口径：
  1. 表里只填了旧列「每手保证金」金额、没填「保证金比例%」→ **放行**，按该固定金额算每手占用。
  2. 回测页「兜底每手保证金」字段 **一起删除**，回测与实盘同口径（缺数据即拒绝，结果里写明跳过原因）。

## 2. 目标

1. 删除配置页与回测页的「兜底每手保证金(元)」输入框，以及 `risk.margin_per_lot` 配置键。
2. 实盘与回测共用 `PositionSizer`：缺保证金数据时返回 0 手，拒单原因明确写出「没有保证金比例 / 每手保证金，拒绝开仓，请补比例」。
3. 交叉测试等研究场景保留「显式关闭保证金上限」的开关（`risk.ignore_margin_cap`），不伪造金额、不需要保证金数据。
4. 升版本 55.0.0，同步 CHANGELOG / README / VERSION / 配置说明书 / 架构文档 / 测试文档。

## 3. 非目标

- 不改每笔风险%、止损距离、凯利、日亏损熔断等其它算仓规则。
- 不改回测「收益率分母」的 `backtest.margin_rate`（默认 10%，只用于指标，不参与开仓手数）。
- 不改 `src/backtest/portfolio.py` 的「配置每手保证金」估算来源（读的是 `risk.contracts.*.margin` 金额，不是被删的键）。
- 不新增界面字段替代兜底金额。

## 4. 口径定义（本次唯一口径）

| 品种表数据 | 每手占用 | 是否开仓 |
| --- | --- | --- |
| 有「保证金比例%」 | 开仓价 × 每手数量 × 比例% | 开，按仓位上限算手数 |
| 无比例、有「每手保证金」金额 | 该固定金额 | 开，按仓位上限算手数 |
| 两者都没有 | — | **拒绝**，原因含「保证金比例」和「请补」 |
| `risk.ignore_margin_cap = true`（仅研究/交叉测试） | — | 本层不限制（`margin_lots=None`） |

补充：
- `margin_usage_pct <= 0` 仍表示「按全部权益估预算」，但**不再免除**保证金数据要求；缺数据照样拒绝。
- 已持仓品种在「已占用」汇总里若缺数据，沿用本笔每手占用兜底（`held_margin = need`），只影响估算，不改变新开仓的拒绝逻辑。
- 拒绝原因经 `order_executor._size_open` → `TradeIntent.detail` → `_volume` 闸门（`size_skip`）落进「最近拒单」，用户可见。

## 5. 具体改动

### 5.1 `src/risk/sizing.py`

1. 模块 docstring：把「没填比例、只写了每手保证金金额：仍按那个固定金额」「仓位上限% ≤ 0，或本品种没填比例也没填金额：本层不限制」改成新口径（金额可用；两者都没有拒绝；`ignore_margin_cap` 才不限）。
2. 删除 `bind_run_margin`，替换为：

```python
def disable_margin_cap(cfg: dict[str, Any]) -> None:
    """只管这一次运行：不做保证金上限判断（交叉测试/研究用）。不写磁盘、不伪造金额。"""
    risk = cfg.setdefault("risk", {})
    risk["ignore_margin_cap"] = True
```

3. `__init__`：删除 `self.margin_per_lot`，新增：

```python
self.ignore_margin_cap = bool(risk.get("ignore_margin_cap", False))
```

4. `margin_for`：保留「比例 → 表内 `margin` 金额」，末尾 `return 0.0`（删除 `return self.margin_per_lot`），docstring 同步。
5. `calc_open`：在算 `_lots_by_margin_cap` 之前加拒绝分支：

```python
if self.ignore_margin_cap:
    margin_lots, lot_margin, margin_note = None, 0.0, ""
else:
    lot_margin = self.margin_for(symbol, entry_price)
    if lot_margin <= 0:
        return SizeResult(
            0,
            (
                f"品种与合约表没有 {symbol} 的保证金比例或每手保证金，"
                "无法估算仓位上限，拒绝开仓。请先在「品种与合约」表里补上保证金比例%。"
            ),
            risk_cash=risk_cash,
            risk_per_lot=risk_per_lot,
            stop_distance=stop_dist,
            intent_lots=intent,
            margin_lots=0,
        )
    margin_lots, lot_margin, margin_note = self._lots_by_margin_cap(
        symbol, current_abs_position, positions, entry_price
    )
```

6. `_lots_by_margin_cap`：删除 `if need <= 0: return None, 0.0, ""`（调用前已保证 `need > 0`），docstring 改为「缺数据由 calc_open 拒绝，本函数只处理有占用数的情况」。

### 5.2 `src/screen/picker.py`

- `from src.risk.sizing import bind_run_margin` → `disable_margin_cap`。
- L310 `bind_run_margin(out, 0)` → `disable_margin_cap(out)`。
- L353 `bind_run_margin(out, float(spec.get("margin_per_lot") or 0))` → `disable_margin_cap(out)`。
- `_attach_universe_contracts` 不变；不再清空品种表保证金。
- docstring「保证金清成 0」改为「不做保证金上限判断」。

### 5.3 `src/backtest/limits.py`

`skip_reason_key` 增加分支（放在「仓位上限/保证金」之前）：原因含「保证金比例」或「拒绝开仓」→ `"保证金比例缺失，拒绝开仓"`。回测结果的跳过原因汇总即可读懂。

### 5.4 界面删除

| 文件 | 改动 |
| --- | --- |
| `src/ui/config_tab_strategy.py` | 删除 L849–855 的「兜底每手保证金(元)」`_entry_row` |
| `src/ui/config_vars.py` | 删除 L137 `self.margin_var`、L472 `self.bt_margin_var`、L941 回填 |
| `src/ui/config_collect.py` | 删除 L754–757 写 `risk_pre["margin_per_lot"]`；保存时 `risk_pre.pop("margin_per_lot", None)` 清掉旧配置残留 |
| `src/ui/backtest_tab.py` | 删除 L516–523 输入框、L52 `bt_symbol_var.trace_add(... _sync_bt_margin_from_table)`、L55 初始同步 |
| `src/ui/backtest_config.py` | 删除 `_table_margin_for_symbol` / `_sync_bt_margin_from_table`（L353–391）、L167 调用、L217 `margin` 解析、L241 确认弹窗行、L268 `self.margin_var.set`、L270/L276 `margin_per_lot` 写入、L433 `generic_margin`、L438 写入、L440 日志字段 |
| `src/ui/config_support.py` | 删除 L782 `self._sync_bt_margin_from_table()` |
| `src/ui/backtest_actions.py` | 删除 L75「兜底每手保证金…」一行 |
| `src/ui/optimize_tab.py` | 删除 L756 `"margin_per_lot"` 快照键 |
| `src/backtest/engine.py` | 删除 L1755 快照键 `margin_per_lot` |
| `src/ui/account_tab.py` | `_account_margin_settings` 返回 `(margins, rates)`；删除 `default_margin`；调用处改为 `margins, rates = ...`，`analyze_book(..., default_margin=0.0)` |
| `config/config.example.yaml` | 删除 L797–798 注释与 `margin_per_lot: 0` |

### 5.5 文档

- `README.md` L866：删掉「配置页『兜底每手保证金』只给没填比例的品种用」，改为「表里既没有比例也没有每手保证金金额的品种，直接拒绝开仓并提示补比例」。
- `docs/配置说明书.md`：CF-34 整行删除（字段已移除）；BT-20 与 AC-13 去掉 `risk.margin_per_lot` 引用。

## 6. 测试

新增/改写（`tests/test_sizing_margin.py`）：

1. `_cfg` 帮助函数去掉 `margin_per_lot`，改为用 `contracts.<品种>.margin` 提供金额；现有 8 个保证金用例按新写法保持通过。
2. 新增：品种不在表（如 `ag2702`），无比例无金额 → `volume == 0`，`reason` 含「保证金比例」与「拒绝开仓」。
3. 新增：只有 `margin` 金额、无比例 → 按金额算占用并允许开仓。
4. 新增：`disable_margin_cap(cfg)` 后 → `margin_lots is None`、`volume == intent_lots`，即便品种不在表。
5. 新增：有比例时价格越高每手占用越大（沿用 `test_margin_rate_grows_with_price`，确保未被删逻辑破坏）。

同步改写：

- `tests/test_symbol_picker.py`：`margin_per_lot` 断言改为 `ignore_margin_cap is True`，`contracts.margin` 不再断言被清零。
- `tests/test_ui_regression.py`：删除 `margin_var` / `bt_margin_var` 的赋值与断言（L439–444、L479、L869、L892）；`_apply_ui_to_cfg` 结果断言补「旧键被清掉」。
- `tests/test_backtest_fill.py` L344/350：去掉 `margin_per_lot`，改用 `contracts.<品种>.margin` 或 `margin_rate_pct`。
- `tests/test_backtest_risk.py`、`tests/test_kelly.py`、`tests/test_ctp_channel.py`：删除 `margin_per_lot` 键（无行为变化）。
- 全仓 `grep margin_per_lot` 必须无业务引用（仅允许出现在迁移清理逻辑与历史文档中）。

定向回归：

```bash
pytest tests/test_sizing_margin.py tests/test_symbol_picker.py tests/test_ui_regression.py tests/test_backtest_fill.py tests/test_backtest_risk.py tests/test_kelly.py tests/test_portfolio_run.py tests/test_portfolio.py -q
```

全量与门禁：

```bash
python tools/testkit.py baseline
python tools/testkit.py gate --coverage
python tools/check_doc_versions.py
```

## 7. 版本与文档（项目固定流程）

- `VERSION` → `55.0.0`（开仓规则变更，大更新）。
- `CHANGELOG.md` 顶部新增 55.0.0：删除兜底每手保证金；缺保证金数据即拒绝开仓；回测页同步；交叉测试改用 `ignore_margin_cap`。
- `README.md` 同步版本号、用例数与保证金口径说明。
- `docs/TODO.md` 记录本次完成项。
- 两份架构 HTML 的版本号、行数、测试文件数同步。
- `docs/软件测试/软件测试方案.md`、`docs/软件测试/软件测试用例.md`、`docs/测试报告/软件测试报告-*.md` 同步版本与用例数。

## 8. 风险与回滚

- 风险 1：品种表漏填比例/金额时，该品种从此完全不开仓。属用户明确要求（宁可不开，也不能靠兜底越风控）；拒单原因会指出要补哪一列。
- 风险 2：交叉测试/优选因缺数据大量 0 手。已用 `ignore_margin_cap` 显式关闭该层。
- 风险 3：旧配置文件里残留 `margin_per_lot`。保存配置时主动 `pop`，运行时不读取，不会复活旧行为。
- 风险 4：账户分析里「资金使用率」少了兜底金额来源，缺数据品种的占用显示为 0/估算，不再虚构金额。
- 回滚：恢复 `margin_for` 末尾的兜底分支并还原字段即可；无数据库/账本结构变更。

## 9. 验收标准

1. `ag2702` 这类「表里完全没有」的品种：实盘算仓返回 0 手，运行台「最近拒单」能看到含「保证金比例」的明确原因；不再出现 98.8% 仓位。
2. 表里只有「每手保证金」金额的品种仍可正常开仓。
3. 配置页、回测页均看不到「兜底每手保证金(元)」；`config.yaml` 不再写出 `margin_per_lot`。
4. 交叉测试仍可按固定手数跑（`ignore_margin_cap`）。
5. 全量 `pytest` 通过，`tools/testkit.py gate --coverage` 6 步通过，`tools/check_doc_versions.py` 通过，VERSION = 55.0.0。
