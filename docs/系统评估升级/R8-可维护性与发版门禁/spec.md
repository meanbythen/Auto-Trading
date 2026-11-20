# R8 可维护性与发版门禁

对应版本：49.15.0
上位文档：[阶段性验收报告-49.16.0.md](../阶段性验收报告-49.16.0.md) 第六节 R8、第八节

## 1. 目标

收口 49.14.0 验收报告第八节 R8 剩下的两项（文档冲刷、重复口径去重），使「口径只有一份实现」「文档与代码现状一致」，并保持所有数值与业务行为不变。

已完成的 2/4 不在本轮重做：P0-3 参数标签已补；`tools/quick_order.py` 已在 R6 加硬门禁（默认不发单，需 `--live` + `QUICK_ORDER_LIVE=1`）。

## 2. 范围与判定

| 编号 | 问题 | 处置 |
|---|---|---|
| D1 | 默认合约乘数 `10` 散落在 9 个模块 | 单一常量 + 各处引用 |
| D2 | 已删引擎名单两份（`_REMOVED_ENGINES` / `_OBSOLETE_STRATEGY_SECTIONS`） | 由一份派生 |
| D3 | 参数节映射与 `ENGINE_ALIASES` 重复 | 用工厂函数替代字典 |
| D4 | 胜率 / 盈利因子四套口径 | 抽到 `src/domain/pnl_stats.py`，调用方只做展示映射 |
| D5 | README 版本停在 49.9.0 / 49.3.0，仍引用已删除的 `preflight_check.py` | 刷新 |
| D6 | 配置说明书停在 45.0.0 | 刷新并核对键 |
| D7 | 规划表把影子盘/评审单标成「缺」，状态行停在 R6 | 刷新 |

不做：`OrderExecutor` 拆分、UI 命令层（R9）；费率与夜盘模型（R10）；任何数值、网格、排序规则变更。

## 3. 实现

### D1 默认乘数单一来源

`src/domain/contracts.py`：

```python
DEFAULT_MULTIPLIER: float = 10.0
```

同文件两处默认值改用它。以下文件把字面量 `10` / `10.0` 换成 `DEFAULT_MULTIPLIER`（仅替换兜底值，不改计算）：

- `src/risk/sizing.py`（`multiplier_for` 兜底、`margin_for` 的 `qty`）
- `src/domain/ledger.py`（旧账补乘数）
- `src/account/signal_book.py`（`record_*` 兜底、`_lookup_multiplier` 找不到时）
- `src/account/signal_stats.py`（明细行兜底）
- `src/utils/state_store.py`（两处快照兜底）
- `src/risk/universe_table.py`（6 处表格读入兜底）
- `src/risk/margin_table.py`（3 处）
- `src/data/quality.py`（1 处）

`src/screen/picker.py` 的 `MAIN_UNIVERSE` 仍是全品种参考表（含流动性标记，配置里写过的数值优先），不搬迁、不合并；新增测试断言 `DEFAULT_CONTRACTS` 里每个品种的乘数与 `MAIN_UNIVERSE` 同名项一致，防止两边漂移。

### D2 删除引擎名单一份

`src/strategy/factory.py`：先保留 `_REMOVED_ENGINES`，再改为派生：

```python
# 已删引擎留在配置里的整节；除这份补充名单外，都来自 _REMOVED_ENGINES。
_EXTRA_OBSOLETE_SECTIONS = {"rotation_scan"}
_OBSOLETE_STRATEGY_SECTIONS = set(_REMOVED_ENGINES) | _EXTRA_OBSOLETE_SECTIONS
```

删除原来的字面量名单。`_OBSOLETE_STRATEGY_SECTIONS` 的用途只是在加载/保存时清掉已删策略的旧小节，扩充后仍不会碰到任何在册引擎小节。

`src/ui/config_collect.py`：把硬编码的 `st.pop("float_hold", None)` 改成遍历 `REMOVED_ENGINES` 逐个 `pop`，不再手写单个引擎名。

### D3 参数节映射一份

`src/strategy/factory.py` 新增公开函数：

```python
def param_section(name: Any) -> str:
    """策略别名 → 参数小节名。未登记的名字原样返回。"""
    key = str(name or "").strip().lower()
    return _ENGINE_ALIASES.get(key, key)
```

- `src/backtest/optimizer.py`：删除 `_ENGINE_PARAM_SECTION` 字典；三处使用改调 `param_section()`；原 `elif engine in _ENGINE_PARAM_SECTION` 改成对 `ENGINE_LABELS` 里的已登记引擎判断，保持「未知引擎不进这一支」。
- `src/backtest/stress.py`：改导入并调用 `param_section()`。
- `src/strategy/__init__.py`：补导出 `param_section`。

### D4 胜率 / 盈利因子一份

新增 `src/domain/pnl_stats.py`：

```python
@dataclass(frozen=True)
class PnlSummary:
    trades: int
    wins: int
    losses: int
    win_rate: float
    gross_profit: float
    gross_loss: float
    profit_factor: float | None   # 无亏损：有盈利为 inf，无盈亏为 None

def summarize_pnl(pnls, *, zero_as_loss: bool = False) -> PnlSummary
```

口径：`wins = 盈亏 > 0`；`losses = 盈亏 < 0`（`zero_as_loss=True` 时把 0 也算进去）；`win_rate = wins / (wins + losses)`；盈利因子 = 总盈利 ÷ 总亏损。

调用方保持现有展示与数值：

| 调用方 | 参数 | 映射 |
|---|---|---|
| `src/backtest/metrics.py::calc_performance` | `zero_as_loss=True` | `inf→99.0`，`None→0.0` |
| `src/ops/health.py` | `zero_as_loss=True` | `inf→99.0`，`None→0.0`；删掉本地 `_profit_factor` / `_win_rate` |
| `src/review/shadow.py` | `zero_as_loss=True` | `inf/None→None`（界面显示「—」） |
| `src/account/signal_stats.py::_slice_from_closes` | `zero_as_loss=False` | 原样透传（`inf` 显示「没有亏损」，`None` 显示「—」） |

`src/backtest/metrics.py` 的 `payoff` / `expectancy` / 打分逻辑不动。

### D5–D7 文档冲刷

- `README.md`：顶部版本、版本一节两处 `现在是` 全部改 49.15.0；删除 `preflight_check.py` 那一行，改为运行页「测试工具」的现用入口；验收报告链接改 49.15.0。
- `docs/配置说明书.md`：版本改 49.15.0，追加一条 49.15.0 复核记录（说明按 `config/config.example.yaml` 核对过键）。核对用一次性脚本放在系统临时目录、不进仓库；只修「键已删除/改名/层级变了」的条目，不重写控件说明。
- `docs/系统评估升级/回测实盘一致性与评估体系升级方案.md`：第一节对照表「影子盘 / 仿真」的「还缺的」改为「已实现（R5）：两本 CTP 账并排对照，见 `src/review/shadow.py`」；S5 标题标注已实施；S1/S3/S4 中与现状矛盾的标题一并改为已实施。
- `docs/系统评估升级/综合升级规划-49.7.0.md`：状态行改为「R1–R7 已实施；R8 进行中，R9/R10 待做」。

### 发版收尾（仓库惯例）

- `VERSION` → `49.15.0`；`CHANGELOG.md` 顶部新增 49.15.0 段。
- `docs/系统评估升级/阶段性验收报告-49.14.0.md` 用 `git mv` 改为 `阶段性验收报告-49.15.0.md`；追加 49.15.0 更新记录；第八节 R8 改 4/4；P2 文档漂移条目标记已处理；结论段同步。
- 同步引用：`README.md` 链接、`docs/系统评估升级/R11-第二阶段/spec.md` 第 4 行。

## 4. 验收

1. `python -m pytest -q -p no:cacheprovider` 全量 0 失败。
2. 新增 `tests/test_pnl_stats.py` 覆盖：零盈亏计入/不计入、无亏损有盈利、全空。
3. 新增测试断言：`DEFAULT_CONTRACTS` 与 `MAIN_UNIVERSE` 乘数一致；`param_section` 别名换算；`_OBSOLETE_STRATEGY_SECTIONS` 覆盖全部已删引擎。
4. 改动文件 `ruff` 通过；改动模块 `mypy` 通过。
5. 文档自检：`README.md` 不再出现 `49.9.0`、`49.3.0`、`preflight_check`；配置说明书版本为 49.15.0；规划表不再写「影子盘……未做」；仓库内除 CHANGELOG 历史段外无 `阶段性验收报告-49.14.0` 引用。

## 5. 风险

- D2 会让 UI 保存时多清掉一些已删引擎的旧小节：这些名字本就不在注册表，删掉不改变任何可用配置。
- D4 只是把同一段算式搬到一处，四个调用点各自保留原有「无亏损」的展示映射，回测摘要、账户页、影子盘、健康卡的数字不变。
