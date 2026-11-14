# Stage5 任务清单：数据管理页与 K 线时段补数

版本：44.7.0（新增功能）

## 1. 数据层

- [x] 新增 `src/data/coverage.py`
  - `expected_bar_times` / `expected_bar_count`：复用 `session_bar_times`，午休、10:15 小休、隔夜、周末不算缺口
  - `coverage_report`：应有根数、已有根数、缺失根数、缺失区间（最多 5 段）
  - `detect_period` / `ensure_period` / `PeriodMismatchError`：读图周期与所选不一致时报错
  - `require_timestamps`：空表、时间戳缺失、重复时间戳拒绝落库
- [x] `BarStore` 增加 `ingest_log` 表、`record_ingest`、`list_ingests`
- [x] `ths_chart.scrape_ths_klines_range`：按时段估算根数，受单次上限约束，读完校验周期
- [x] `kline.import_ths_chart_batch`：读图 → 周期校验 → 原样库 → 清洗库 → 覆盖统计 → 批次记录 → 库存 Excel
- [x] `kline.import_ths_chart` 保留旧入口与返回类型，内部走同一套入库
- [x] `inventory` 增加「读取记录」页，`collect_kline_inventory` 带 `ingests`

## 2. 界面

- [x] 新增 `src/ui/data_tab.py`（`DataTabMixin`）
  - 合约、K 线周期、开始/结束时间（日历 + 时分下拉）、预计根数
  - 从同花顺读取K线 / 停止读图（ESC）
  - 查看库存、删除、查看这根K线、检查行情质量
- [x] `app.py` 注册「数据管理」页签并接入 Mixin
- [x] `app_state.py` 增加 `dm_*` 变量；ESC 改绑 `_dm_scraping`
- [x] 历史回测页移除 K 线管理控件与回调，保留跳转按钮

## 3. 测试与质量

- [x] `tests/test_kline_coverage.py`
- [x] `tests/test_kline_ingest.py`（两批接续、批次记录、Excel 读取记录页）
- [x] `ruff check .`
- [x] `python -m mypy src`
- [x] `python -m pytest -q -p no:warnings`

## 4. 文档与版本

- [x] `VERSION` → 44.7.0
- [x] CHANGELOG 增加 44.7.0
- [x] README 页签说明与注意事项同步
- [x] 配置说明书新增「数据管理」DM-01~DM-11
