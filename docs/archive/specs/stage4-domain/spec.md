# Stage4-1 领域层抽取规格（44.6.9）

- 依据：`docs/项目审阅报告.md` 阶段 4 第 1、5 条
- 本轮范围：`src/domain/` 抽取 + 消除 `account ⇄ risk` 循环依赖 + mypy 扩围 + 失败路径用例 + 文档版本同步
- 明确不做：`run_backtest` / `BarCursor` / `optimizer.py` / UI Mixin 拆分；Stage4-3 配置声明式留到下一轮单独做（需逐键核对 `config_loader` 与默认值，不与领域层抽取混在一起）
- 约束：业务语义不变；不改配置键；不新增依赖；不 git commit

## 现状事实（已核对）

- 循环依赖只有一处闭环：
  - `src/account/signal_book.py:59` 函数内 `from src.risk.sizing import PositionSizer`（`multiplier_map`）
  - `src/risk/dynamic.py:244-251` 函数内 `from src.account.signal_book import ...`（读账本权益）
- 类型分散：`PositionState` 在 `src/strategy/types.py:168`，`TradeRecord` 在 `src/backtest/metrics.py:13`，持仓 JSON 的写出/读回分别硬编码在 `src/utils/state_store.py:155-177` 与 `:475-502`。
- 账本读取/权益计算散落在 `src/account/signal_book.py`（`read`、`shown_equity`、`equity_and_peak`、`ctp_history_equity`、`_ensure_changes`、`_legacy_changes`、`_equity_points`）。
- 账户文件布局（PROFILES、`book_file`、`state_file`、`intent_file` 等）在 `src/account/workspace.py`，纯路径逻辑，无 account 内部依赖。

## 目标结构

```
src/domain/
├── __init__.py      # 只导出 PositionState / TradeRecord
├── position.py      # PositionState + to_dict/from_dict（持仓唯一序列化入口）
├── trade.py         # TradeRecord + to_dict（成交唯一序列化入口）
├── contracts.py     # 合约乘数：默认表 + 从 cfg 构建
├── accounts.py      # 账本文件布局（哪本账对应哪个文件）
└── ledger.py        # 账本读取 + 余额/峰值/权益曲线（唯一读入口）
```

依赖方向：`strategy/backtest/account/risk/utils/scheduler → domain → 标准库`。domain 不得 import 任何 `src.*` 业务模块。

---

## 改动 1：新增领域模块

### 1.1 `src/domain/__init__.py`

```python
"""领域层：跨模块共用的数据类型与账本读写。"""
from src.domain.position import PositionState
from src.domain.trade import TradeRecord

__all__ = ["PositionState", "TradeRecord"]
```

### 1.2 `src/domain/position.py`

- 把 `src/strategy/types.py` 的 `PositionState` 原样搬入（字段顺序、注释、`reduce_close` 全部不变）。
- 新增 `to_dict(self) -> dict[str, Any]`，输出键固定为：
  `side, volume, entry_price, initial_stop, trail_stop, trail_armed, opened_at, box_high, box_low, box_height`，数值一律 `int/float/bool/str` 强转，与当前 `state_store.snapshot_positions` 完全一致。
- 新增 `@classmethod from_dict(cls, symbol: str, raw: Mapping[str, Any]) -> PositionState`：
  - `side` 不在 `flat/long/short` 时归 `flat`；
  - 数值字段同当前 `restore_positions` 的 `float(... or 0)` / `int(... or 0)`；
  - `symbol` 用传入值；忽略多余键（如 `extreme`）。

### 1.3 `src/domain/trade.py`

- 把 `src/backtest/metrics.py` 的 `TradeRecord` 原样搬入（字段与注释不变）。
- 新增 `to_dict(self) -> dict[str, Any]: return asdict(self)`，作为成交序列化唯一入口。

### 1.4 `src/domain/contracts.py`

- 搬入 `src/risk/sizing.py` 的 `DEFAULT_CONTRACTS`。
- 新增：
  - `build_contracts(cfg) -> dict[str, dict[str, Any]]`：合并默认表与 `cfg["risk"]["contracts"]`，逻辑与当前 `PositionSizer.__init__` 第 103-115 行逐字一致（dict 值保留 name/margin/margin_rate_pct，标量值转 `{"multiplier": float(v), "name": str(k)}`）。
  - `contract_multipliers(cfg) -> dict[str, float]`：`{key: float(info.get("multiplier", 10))}`。

### 1.5 `src/domain/accounts.py`

从 `src/account/workspace.py` 原样搬入（代码不改）：
`LIVE/PAPER/CTP_SIM/CTP_LIVE/ORDER`、`PROFILES`、`normalize_id`、`id_from_label`、`is_ctp_book`、`book_id_for_ctp_kind`、`active_file`、`load_active_id`、`profile_of`、`book_file`、`periods_folder`、`state_file`、`intent_file`。

- 本模块只依赖 `json/os/threading/Path/Any/loguru`，不得 import `src.utils.config_loader` 或 `src.trader.*`。

### 1.6 `src/domain/ledger.py`

从 `src/account/signal_book.py` 搬入并改为公开名（函数体不改）：
- `safe_float`（原 `_safe_float`）
- `read_book_file(path)`（原 `_read_book_file`）
- `ensure_changes`（原 `_ensure_changes`）
- `legacy_changes`（原 `_legacy_changes`）
- `equity_points`（原 `_equity_points`）
- `balance_of`、`shown_equity`、`equity_and_peak`、`ctp_history_equity`
- 新增 `load_active_book(root: str | Path) -> dict[str, Any] | None`：`read_book_file(book_file(root))`。

只允许依赖 `src.domain.accounts` 与标准库/loguru。

---

## 改动 2：改接各调用方（保持旧导入路径可用）

### 2.1 `src/strategy/types.py`

- 删除 `PositionState` 定义，改为 `from src.domain.position import PositionState`。
- 文件内其它内容（`StrategyDecision`、开平仓模式常量等）不动；`from src.strategy.types import PositionState` 继续可用。

### 2.2 `src/backtest/metrics.py`

- 删除 `TradeRecord` 定义，改为 `from src.domain.trade import TradeRecord`；`MARGIN_RATE`、`Performance`、`calc_performance` 等不动。

### 2.3 `src/backtest/models.py`

- `"trades": [asdict(t) for t in self.trades]` → `[t.to_dict() for t in self.trades]`；删掉不再使用的 `asdict` 导入。

### 2.4 `src/risk/sizing.py`

- `from src.domain.contracts import DEFAULT_CONTRACTS, build_contracts`。
- `PositionSizer.__init__` 的 contracts 构建段（103-115 行）替换为 `self.contracts = build_contracts(cfg)`。
- 其余计算逻辑不动，`DEFAULT_CONTRACTS` 仍可从本模块导入（兼容旧引用）。

### 2.5 `src/risk/dynamic.py`

- 模块顶部 `from src.domain.ledger import ctp_history_equity, equity_and_peak, load_active_book, shown_equity`。
- `live_equity_and_peak_or_error` 内部改为 `book = load_active_book(root)` 并直接调用上述函数，删除函数内对 `src.account.signal_book` 的 import。
- 返回值语义不变：账本不存在 `(None, None, "")`；读取异常 `logger.error` + `(None, None, 错误文本)`。

### 2.6 `src/account/workspace.py`

- 改为从 `src.domain.accounts` 导入被搬走的常量与函数并重导出（保留 `src.account.workspace` 全部旧名字）。
- 本文件只保留：`ctp_user_id`、`display_label`、`labels`、`set_active_id` 与 `_LOCK`。

### 2.7 `src/account/signal_book.py`

- 从 `src.domain.ledger` 导入 `balance_of / ctp_history_equity / ensure_changes / equity_and_peak / equity_points / legacy_changes / read_book_file / safe_float / shown_equity`，并从本模块重导出同名函数（`src.account.__init__` 的 `balance_of` 等继续可用）。
- `_safe_float` → `safe_float`（3 处）；`_ensure_changes` → `ensure_changes`（9 处）；`_equity_points` → `equity_points`（2 处）。
- 删除已搬走的函数定义，不保留私有别名。
- `multiplier_map` 改为 `from src.domain.contracts import contract_multipliers`，空配置仍返回 `{}`。
- 其余记账逻辑（`append_trade`、`record_program_trade`、配对、写盘）一律不动。

### 2.8 `src/utils/state_store.py`

- 顶部 `from src.domain.position import PositionState`。
- `snapshot_positions`：`item = pos.to_dict()`，随后仅追加 `extreme` 逻辑（保持原键与顺序）。
- `restore_positions`：`positions[str(sym)] = PositionState.from_dict(str(sym), d)`，`extreme` 处理逻辑不变。

---

## 改动 3：mypy 扩围（Stage4-5）

`pyproject.toml` 的 `[[tool.mypy.overrides]] module` 增加：
`"src.domain.*"`, `"src.account.*"`, `"src.risk.*"`, `"src.utils.*"`, `"src.scheduler.*"`（`ignore_errors = false`）。

规则：
1. 本轮改动涉及的文件必须先修到无错误。
2. 对历史 Mixin 的 `attr-defined/has-type` 类错误，允许像现有 `src.scheduler.order_executor` 一样单独加一条 override 并用 `disable_error_code` 精确收窄，禁止整模块 `ignore_errors = true` 回退。
3. 若某模块出现与本轮无关且需要大改的问题，保留该模块在严格列表外，并在完成后向用户报告具体模块与错误数，不得静默放弃。

---

## 改动 4：失败路径用例

新增 `tests/test_domain_ledger.py`，覆盖：

1. `read_book_file`：文件不存在返回 `None`；损坏 JSON 返回 `None`；非 dict 返回 `None`；`initial_capital` 非法字符串被清洗为 `0.0`。
2. `balance_of`：旧账（无 `changes`）自动补初始资金 + 出入金 + 已平仓盈亏，与旧算法一致。
3. `shown_equity`：CTP 账取 `ctp_snapshot.balance`；balance 缺失时回退 `available`；非 dict 返回 `0.0`。
4. `equity_and_peak`：多笔变动后的余额与峰值。
5. `ctp_history_equity`：`ctp_history` 追加当前 `ctp_snapshot`，同值不重复追加。
6. `PositionState.to_dict/from_dict`：往返一致；未知键被忽略；非法 `side` 归 `flat`；`volume` 为字符串/None 时归 0。
7. `contract_multipliers`：空配置返回 `{}`；自定义品种覆盖默认乘数。
8. `TradeRecord.to_dict`：键集合与 dataclass 字段一致。

现有测试不改；若因搬移导致导入失败，只改 import 路径，不改断言。

---

## 改动 5：文档与版本

- `VERSION`：`44.6.8` → `44.6.9`。
- `CHANGELOG.md` 顶部新增 44.6.9：领域层抽取、循环依赖消除、mypy 扩围、失败路径用例。
- `README.md`：把正文中声明的当前版本号同步为 `44.6.9`（若存在 44.5.0 等旧号一并修正）。
- `docs/系统蓝图.md`：模块清单加入 `src/domain/`，版本号同步。
- `docs/配置说明书.md`：本轮不改配置键，仅在文件中标注的版本号落后时同步。
- `docs/项目审阅报告.md` 阶段 4 第 1 条：追加「已完成（44.6.9）：…」；第 5 条标注 mypy 扩围与失败路径用例完成情况。

---

## 验收标准

1. `python -c "import src.account.signal_book; import src.risk.dynamic"` 与反序导入均成功（无循环依赖）。
2. `python -m ruff check .` 通过。
3. `python -m mypy src` 通过，且 `src.domain.*` 在严格列表内。
4. `python -m pytest -q` 全绿（含新增 `tests/test_domain_ledger.py`）。
5. `git diff` 中不出现 `src/backtest/engine.py` 的 `run_backtest` 改动，不出现 `BarCursor` 改动。
6. 版本号 44.6.9，CHANGELOG/README/系统蓝图/审阅报告同步。

## 需要确认

- 本轮是否按上述范围执行（Stage4-1 + Stage4-5 + 文档），Stage4-3 配置声明式留到下一轮。
