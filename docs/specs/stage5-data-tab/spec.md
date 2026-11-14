# Stage5 数据管理页与 K 线库补数规格（44.7.0）

## 1. 目标

1. 把「本地 K 线库建立/管理」从历史回测页切出，新增独立「数据管理」页签。
2. 同花顺读图从「填根数」升级为「选周期 + 选时段」：按时段自动算根数，读完校验周期与覆盖完整性。
3. 每次读取都记批次（时间戳、请求时段、实读范围、根数、缺口），写入 sqlite 并同步 `data/K线库存.xlsx`。
4. 多批次数据按时间戳 upsert 接续，不重复、不乱序；历史回测与交易逻辑不变。

## 2. 现状

- 主窗口页签：`src/ui/app.py:202-230`，`ctk.CTkTabview` 固定八个页签。
- 历史回测控件：`src/ui/backtest_tab.py:329-434`；回调 `_on_show_kline_inventory` / `_on_purge_local_klines` / `_on_inspect_local_kline` / `_on_check_quote_quality` / `_on_scrape_ths_chart` / `_on_stop_ths_chart`（`backtest_tab.py:1640-1970`）。
- 读图实现：`src/data/ths_chart.py:461-522` 打开主图，`walk_tooltips`（L232-280）从当前光标按左方向键逐根往回读，最多 `limit` 根；周期由用户在期货通里手选，程序只当标签。
- 入库：`src/data/kline.py:772-789` `import_ths_chart()` 写原样库 + 清洗库；`BarStore` 主键 `(symbol, period, ts)`（`src/data/bar_store.py:267-288`），天然去重。
- 库存 Excel：`src/data/inventory.py:89-294`，只在点「查看本地K线库存」时生成 `data/K线库存.xlsx`（最新 + 带时间戳各一份）。
- 现成控件：`src/ui/datetime_pickers.py` 的 `DateTimeField`（日期 + 时/分下拉），`src/data/periods.py` 的 `PERIOD_MENU` / `canonical_period`。

## 3. 页面设计（数据管理）

新增 `src/ui/data_tab.py`，`DataTabMixin`，在 `app.py` 页签列表里插到「历史回测」之后。

自上而下：

1. **读取同花顺 K 线**
   - 合约输入（默认取配置里第一个关注合约，可改）。
   - 周期：`PERIOD_MENU`（1/5/15/30/60/日线/自定义），自定义时出现分钟数输入。
   - 开始 / 结束：各一个 `DateTimeField`（日期 + 时 + 分），默认结束=现在、开始=结束前 7 天。
   - 按钮：`从同花顺读取K线` / `停止读图`；说明文字写清「期货通里先把合约和周期点成一致，程序会校验」。
2. **本地库存**
   - `查看本地K线库存`（生成并打开 Excel）、`删除本地K线`（沿用现有弹窗）、`检查行情质量`、`查看这根K线`。
3. 状态行：读图进度、实读范围、新增根数、缺口数、Excel 路径。

历史回测页：移除上述 K 线管理控件，保留一行提示「K 线库建立/管理已移到『数据管理』页」。

## 4. 数据层设计

### 4.1 时段 → 根数

新增 `src/data/coverage.py`：

```python
def expected_bar_times(start: datetime, end: datetime, period: str) -> pd.DatetimeIndex: ...
def expected_bar_count(start, end, period) -> int: ...
def coverage_report(bars, start, end, period) -> CoverageReport: ...
def detect_period(bars) -> str | None: ...   # 按相邻时间差中位数判断 1/5/15/30/60/D
```

- 预期时间点复用 `session_bar_times`（`src/backtest/engine.py`）的交易日/交易时段口径；`period` 用 `canonical_period` 归一。
- `CoverageReport`：`expected_n` / `have_n` / `missing_n` / `missing_ranges`（前 5 段）/ `first_ts` / `last_ts`。
- `detect_period` 用相邻时间差中位数比对所选周期；不一致时抛 `PeriodMismatchError`，**不落库**，提示实际读到的周期。

### 4.2 读图入口

`src/data/ths_chart.py` 新增：

```python
def scrape_ths_klines_range(cfg, symbol, period, start, end, *, max_bars=5000) -> pd.DataFrame
```

- 需求根数 = `expected_bar_count(start, end, period) + 缓冲`，封顶 `data.ths_chart.max_bars`（默认 5000）；超限时只告警并截断。
- 调现有 `scrape_ths_klines(..., limit=want)`；读完做 `detect_period` 校验。
- 结束时间是目标边界，不是导航指令：实际从当前主图位置往左读，允许读到比 `start` 更早的根（多存无害）。

`src/data/kline.py` 新增：

```python
def import_ths_chart_batch(self, symbol, period, *, start, end, limit=None) -> dict[str, Any]
```

返回 `{"added": int, "read_n": int, "bar_from": str, "bar_to": str, "coverage": dict, "inventory_path": str}`；内部复用 `_save_vendor_raw` / `_archive_bars`，完成后：
1. 调 `coverage_report` 算覆盖；
2. 调 `BarStore.record_ingest(...)` 记批次；
3. 调 `write_kline_inventory(root)` 更新 Excel。
原 `import_ths_chart()` 保留，行为不变（内部转调新方法，`start/end=None`）。

### 4.3 批次表

`src/data/bar_store.py` 在 `market.sqlite` 增加：

```sql
CREATE TABLE IF NOT EXISTS ingest_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL, source TEXT NOT NULL, symbol TEXT NOT NULL, period TEXT NOT NULL,
  requested_from TEXT, requested_to TEXT, read_from TEXT, read_to TEXT,
  read_n INTEGER, added_n INTEGER, expected_n INTEGER, missing_n INTEGER, note TEXT
);
```

`BarStore.record_ingest(...)` / `BarStore.list_ingests(limit=200)`。不删旧表、不迁移旧数据。

### 4.4 库存 Excel

`src/data/inventory.py`：

- `collect_kline_inventory` 额外读 `list_ingests()`，放进 `book["ingests"]`。
- `write_inventory_excel` 增加「读取记录」页：`读取时间 / 数据源 / 合约 / 周期 / 请求时段 / 实读范围 / 根数 / 新增 / 应有 / 缺失 / 说明`。
- 每次 `import_ths_chart_batch` 成功后自动调用 `write_kline_inventory`。

### 4.5 连续性与去重

- 依赖现有 `PRIMARY KEY (symbol, period, ts)` + `_archive_bars` 合并；新增测试：两批 `[09:05..09:25]`、`[09:15..09:35]` 合并后 7 根、时间升序、无重复。
- 每根必须有时间戳：`bars_from_tooltips` 产出的 `DatetimeIndex` 若为空或含 `NaT`，直接报错不落库。

## 5. UI 迁移清单

| 现有位置 | 处理 |
|---|---|
| `backtest_tab.py:329-434` K 线管理控件 | 从回测页删除，迁到 `data_tab.py` |
| `_on_show_kline_inventory` 等 6 个回调 | 迁移到 `DataTabMixin`，日志/弹窗逻辑不变 |
| `bt_ths_bars_var` | 数据管理页不再使用（改为时段）；回测页保留变量避免状态恢复报错 |
| `bt_symbol_var` / `bt_tf_var` | 新页签用自己的 `dm_symbol_var` / `dm_period_var` / `dm_period_custom_var` |
| `_period_code` / `_ths_bar_limit` / `_set_ths_scraping` | 前两个迁到数据页；`_set_scraping` 改名 `_set_dm_scraping` |

`app_state.py` 新增 `dm_*` 变量初始化；`app.py` 注册页签并 `_build_data_tab()`。
ESC 停止读图绑定保持窗口级，仅在 `_dm_scraping` 时生效。

## 6. 非目标

- 不让程序自动切换期货通的合约/周期（只校验、提示）。
- 不改 `run_backtest`、策略、optimizer、下单链路。
- 不做分钟级自动补历史（跨多屏自动翻页）；本轮仍以一次读图覆盖目标时段为限。
- 不迁移历史数据到新表；`ingest_log` 从本轮开始记。

## 7. 测试

- `tests/test_kline_coverage.py`：时段算根数（含夜盘/午休）、缺口识别、周期识别、周期不一致报错。
- `tests/test_kline_ingest.py`：`record_ingest` / `list_ingests` 落库、两批合并接续去重、库存 Excel 含「读取记录」页。
- 现有回测/数据测试全量回归；`ruff`、`mypy src` 全绿。
- UI 手工验收：新页签可见、回测页不再有 K 线管理控件、读图后自动出 Excel。

## 8. 版本

新增功能 → 第二位：`44.6.12` → `44.7.0`。同步 CHANGELOG、README、配置说明书（新增 `data.ths_chart` 说明如涉及）、审阅报告。
