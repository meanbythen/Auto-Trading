# P3 规格：数据目录 + 策略插件

基线：49.19.x（P2 已提交 `fe1b012`）　目标来源：[系统架构与演进方案.md](../../系统架构与演进方案.md) §3 P3
原则：最小改动、可单独回退；插件发现失败一律回退现有 `REGISTRY`；SQLite 关闭时目录功能自动静默。

---

## 1. 目标与验收

对应方案 P3 两句话：

1. 合约表导入 `instruments` 快照；K 线/tick 建目录表；研究统一走快照。
2. 策略插件清单 + 参数 schema + 自动发现；UI 自动列策略。

验收：

1. 新增一个策略只需在 `src/strategy/plugins/` 放一个 `.py` 文件（模块内导出 `SPEC`），配置页/回测页/优化页下拉框自动出现，不改 `REGISTRY`、不改 UI。
2. 插件文件写坏或导入抛异常时，程序照常启动，下拉框和回测仍用内置 `REGISTRY`，并记一条 warning。
3. `refresh_instruments()` 能把 `config/品种与合约.xlsx` 固化成一个不可变快照（同内容不重复生成）；可按快照 ID 查回全部行。
4. `refresh_kline_catalog()` / `refresh_tick_catalog()` 把现有 K 线库、tick 库的库存范围写进 `data_catalog`，可重复执行（upsert）。
5. 新产生的回测结果 `details` 里带 `strategy: {code,label,version}` 与 `data_catalog: {instruments_snapshot,kline_digest,tick_digest}`，可追到策略版本和数据版本。
6. `storage.sqlite_enabled: false` 时上述函数不建库、不写库，返回空值，交易与回测不受影响。
7. `python -m pytest tests -q`、`ruff check src tests`、`mypy src` 全部通过；import-linter 三条约束不破。

回退：

- 策略插件：删除 `src/strategy/plugins/` 目录内容（或整个目录），其余代码自动回到 `REGISTRY`；也可把 `strategy.plugins_enabled` 设为 `false`。
- 数据目录：目录表与快照表随 `data/trading.db` 一起可删除重建；关闭 `storage.sqlite_enabled` 即完全不启用。

---

## 2. 现状事实（已核对）

| 位置 | 事实 |
|---|---|
| `src/strategy/factory.py` | `REGISTRY` 正式名 + 别名；`ENGINE_LABELS`、`ENGINE_AUDIT_MODES`、`ENGINE_SECTION_DEFAULTS`、`PRIMARY_ENGINES` 都在此处/`params.py` |
| `src/strategy/params.py` | `ENGINE_SECTION_DEFAULTS` 是各策略参数默认值唯一来源 |
| `src/ui/app_common.py` | `ENGINE_CHOICES` 在 import 时由 `REGISTRY` 静态拼出；配置页、回测页、优化页共用 |
| `src/ui/review_tab.py` | 复盘下拉用 `src/review/replay.py` 的 `ENGINE_BY_LABEL`（静态） |
| `src/backtest/engine.py` L1777-L1798 | 回测 `details` 已含 engine/时间/数据源/研究数据集引用；`experiment` manifest 已含代码版本、配置 sha、K 线指纹 |
| `src/risk/universe_table.py` | `read_universe_table()` 读 Excel；`resolve_universe_path()` 解析路径 |
| `src/data/inventory.py` | `collect_kline_inventory()` 汇总 `data/klines/*.sqlite`；`split_stored_symbol()` 拆来源 |
| `src/data/tick/vnpy_store.py` | `tick_overview(database=...)` 已能汇总 tick 库存 |
| `src/account/schema.sql` | `connect()` 每次建连接都会执行，新增表可直接追加 |
| `src/account/trading_db.py` | `storage_settings()` / `connect()` / `DB_LOCK` 已可复用；SQLite 默认开启 |

结论：P3 不需要改交易链路，只新增两个只读/旁路模块，并在三处接线（工厂、UI 名单、回测 details）。

---

## 3. 设计：策略插件（`src/strategy/plugins.py`，新增）

### 3.1 数据结构

```python
@dataclass(frozen=True)
class StrategySpec:
    code: str                  # 正式引擎名，如 "daily_body" / "my_breakout"
    label: str                 # 中文名
    short_label: str           # 交叉测试短名
    factory: Callable[[dict], Any] | None
    section: str               # 参数小节名，默认等于 code
    audit_mode: str | None
    version: str               # 内置 "builtin-1"；插件必须在 SPEC 里声明
    source: str                # "builtin" | "plugin"
    params: dict[str, Any]     # 额外参数 schema；默认取 ENGINE_SECTION_DEFAULTS[section]
```

### 3.2 发现规则

`discover_specs(directory: str | Path | None = None) -> dict[str, StrategySpec]`

1. **内置**：遍历 `REGISTRY` 的正式名（排除 `_ENGINE_ALIASES` 的键），用 `ENGINE_LABELS`、`ENGINE_SHORT_LABELS`、`ENGINE_AUDIT_MODES`、`ENGINE_SECTION_DEFAULTS` 填 spec；`version="builtin-1"`、`source="builtin"`。
2. **插件目录**：默认 `src/strategy/plugins/`（新目录）。只扫描 `*.py`、跳过 `_` 开头文件；用 `importlib.util.spec_from_file_location` 按文件加载，不要求包导入。
3. 模块契约：必须导出 `SPEC`（dict 或 `StrategySpec`）。必填 `code`、`label`、`factory`、`version`；可选 `short_label`、`section`、`audit_mode`、`params`。
4. **任何插件异常**（语法错误、导入失败、缺字段、code 与内置/别名冲突）只 `logger.warning`，跳过该插件，继续返回其他 spec；函数本身永不抛出。
5. 不缓存扫描结果（目录很小），避免新文件要重启才生效；`discover_specs` 可被测试用参数指定目录。

### 3.3 参数 schema

```python
def param_schema(code: str) -> list[dict[str, Any]]
```
- 返回 `[{key, type, default, required: False}, ...]`。
- 默认从 `ENGINE_SECTION_DEFAULTS[section]` 推导：`bool/int/float/str/object`；插件 `SPEC.params` 优先并追加。
- `def validate_params(code: str, params: dict) -> list[str]`：只检查已知键的类型是否匹配；未知键**不报错**（旧配置常带历史键），返回消息供日志，不阻断创建。

### 3.4 接线（只改必要位置）

| 文件 | 改动 |
|---|---|
| `src/strategy/factory.py` | `create_strategy()` 在 `REGISTRY.get()` 失败后，尝试 `discover_specs()` 里同 code 且有 factory 的插件；插件加载异常时保持现有“回退 `DEFAULT_ENGINE`”逻辑。`normalize_engine()` 与 `sanitize_strategy_config()` 认插件 code（用 `discover_specs()`），避免保存配置时被重置成日内实体。 |
| `src/strategy/factory.py` | 新增 `strategy_identity(cfg) -> dict`，返回 `{code,label,version}`；认不出时 version 为空。 |
| `src/ui/app_common.py` | `ENGINE_CHOICES` 改为由 `discover_specs()` 生成：`multi_factor` 优先，其余按 `REGISTRY` 顺序，插件追加在后；发现失败时退回现有静态拼法。`ENGINE_LABELS` 同步补插件中文名。 |
| `src/backtest/engine.py` | L1777 的 `details` 增加 `"strategy": strategy_identity(cfg)`。 |

不改 `review_tab` 的静态 `ENGINE_BY_LABEL`：复盘页仍只对照内置策略，插件策略在配置/回测/优化三个入口可选。若后续需要，另开小改。

### 3.5 插件示例（不随代码交付，仅文档说明）

`src/strategy/plugins/example_breakout.py`：

```python
from src.strategy.bollinger_break import BollingerBreakStrategy

SPEC = {
    "code": "example_breakout",
    "label": "示例突破",
    "factory": BollingerBreakStrategy,
    "version": "1.0.0",
    "params": {"lookback": {"type": "int", "default": 20}},
}
```

新增策略 = 新增一个这样的文件；不需要改 `factory.py`。

---

## 4. 设计：数据目录（`src/data/catalog.py`，新增）

### 4.1 Schema（追加到 `src/account/schema.sql`）

```sql
CREATE TABLE IF NOT EXISTS instrument_snapshots (
    snapshot_id   TEXT PRIMARY KEY,          -- inst-YYYYMMDDTHHMMSS-<sha8>
    source_path   TEXT NOT NULL DEFAULT '',
    source_sha256 TEXT NOT NULL DEFAULT '',
    row_count     INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instruments (
    snapshot_id           TEXT NOT NULL,
    symbol                TEXT NOT NULL,
    product               TEXT NOT NULL DEFAULT '',
    name                  TEXT NOT NULL DEFAULT '',
    multiplier            REAL,
    margin_rate_pct       REAL,
    margin                REAL,
    commission            REAL,
    commission_close      REAL,
    commission_close_today REAL,
    price_tick            REAL,
    trading_sessions      TEXT NOT NULL DEFAULT '',
    flatten_times         TEXT NOT NULL DEFAULT '',
    enabled               INTEGER NOT NULL DEFAULT 1,
    payload_json          TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (snapshot_id, symbol)
);

CREATE TABLE IF NOT EXISTS data_catalog (
    catalog_id   TEXT PRIMARY KEY,          -- kline:clean:fu2701:5 / tick:SHFE.fu
    kind         TEXT NOT NULL,             -- kline / tick
    source       TEXT NOT NULL DEFAULT '',
    symbol       TEXT NOT NULL,
    period       TEXT NOT NULL DEFAULT '',
    rows         INTEGER NOT NULL DEFAULT 0,
    ts_from      TEXT NOT NULL DEFAULT '',
    ts_to        TEXT NOT NULL DEFAULT '',
    digest       TEXT NOT NULL DEFAULT '',  -- 目录指纹（行数+起止），不是逐行内容 hash
    updated_at   TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
```

追加即生效：`src/account/trading_db.py::connect()` 每次连接都执行 `schema.sql`。

### 4.2 函数

| 函数 | 行为 |
|---|---|
| `enabled(root, cfg=None) -> bool` | 复用 `storage_settings()` 的 `sqlite_enabled` |
| `refresh_instruments(root, cfg=None, table_path=None, force=False) -> str` | 解析总表路径；文件指纹相同且非 force 时直接返回最近 snapshot_id；否则 `read_universe_table()` 后在**同一事务**写 `instrument_snapshots` + `instruments`；SQLite 关闭/表缺失/读表失败返回 `""` 并记 warning |
| `latest_instrument_snapshot(root) -> dict \| None` | 最近一条快照元数据 |
| `list_instruments(root, snapshot_id=None) -> list[dict]` | 指定快照（默认最近）的全部行，字典字段与建表列一致 |
| `refresh_kline_catalog(root) -> int` | `BarStore(market.sqlite).summarize()` + `RawBarStore(raw.sqlite).summarize()`，按 `kline:{clean|raw}:{symbol}:{period}` upsert；返回写入行数 |
| `refresh_tick_catalog(root, cfg=None) -> int` | `tick_overview(database=...)`，按 `tick:{exchange}.{symbol}` upsert；库不存在返回 0 |
| `refresh_all(root, cfg=None) -> dict` | 依次执行上面三项，返回各自结果 |
| `catalog_reference(root) -> dict` | 读最近快照 ID + `data_catalog` 各 kind 的整体指纹（`sha256(kind + rows + 起止)`），作为回测 details 的“数据版本”；读不到返回 `{}` |
| `main()` | CLI：`python -m src.data.catalog --root . [--instruments] [--klines] [--ticks] [--all]` |

### 4.3 “研究统一走快照”

不重写现有研究编排。现有 `src/data/research_dataset.py` 已经生成不可变数据集并在 `experiment` manifest 记录指纹；P3 只补齐两件事：

1. 合约表有 `instruments` 快照，按 ID 可回查；
2. 每次回测 `details` 带 `data_catalog` 引用（最近合约快照 + K 线/tick 目录指纹）。

这样“回测结果 → 策略版本 + 数据版本”两步可查，且不改回测输入路径。

---

## 5. 配置开关（可选，不新增 YAML 也可用）

- `strategy.plugins_enabled`：默认 `true`；设为 `false` 时 `discover_specs()` 只返回内置。
- 目录功能沿用 `storage.sqlite_enabled`，不新增开关。

---

## 6. 测试与验证

新增：

- `tests/test_strategy_plugins.py`
  - 所有 `PRIMARY_ENGINES` 都有 spec、中文名和（有默认值时）非空 schema。
  - 临时目录放一个合法插件：`discover_specs(tmp)` 能发现；`monkeypatch PLUGIN_DIR` 后 `create_strategy` 能创建；`normalize_engine`/`sanitize_strategy_config` 不把它重置。
  - 临时目录放坏插件（语法错/缺 `factory`）：不抛异常，内置策略仍可用。
  - `strategy_identity` 返回 code/label/version。
- `tests/test_data_catalog.py`
  - `sqlite_enabled: false`：所有 refresh 返回空/0，且不生成 `trading.db`。
  - 用 openpyxl 写一张最小总表 → `refresh_instruments` 生成快照；重复调用返回同一 ID；`list_instruments` 行数/字段正确。
  - 用 `BarStore.upsert` 写两行 K 线 → `refresh_kline_catalog` 写入目录；再次执行行数不变（upsert）。
  - `catalog_reference` 含 `instruments_snapshot` 与 `kline_digest`。

回归：

```powershell
python -m pytest tests -q
ruff check src tests
mypy src
```

---

## 7. 风险与边界

1. 插件在 import 时执行任意代码：只扫描本地可信目录，异常全部吞掉并记 warning，不影响启动。
2. `ENGINE_CHOICES` 仍是模块级列表：插件目录变化后需要重启界面（与现有 `register_rule_engines` 行为一致），不在本阶段做热加载。
3. `data_catalog.digest` 是目录指纹（行数+起止），不是逐行内容哈希；逐行内容指纹仍由 `experiment.archive_report()` 的 `sha256` 负责。
4. tick 库可能很大：只调用现有 `tick_overview` 聚合查询，不逐笔读取。
5. 快照表只增不删；需要清理时直接删 `data/trading.db` 重建，JSON 账本与 Excel 不受影响。
