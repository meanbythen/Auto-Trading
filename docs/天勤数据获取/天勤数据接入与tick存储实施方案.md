# 天勤数据接入与 tick 存储实施方案

状态：**代码与离线测试已完成（45.2.0）；试用期人工验收未做（2026-11-16）**
目标：以天勤 TqSdk 为主数据源，完成实时 tick 常驻采集、专业版历史 tick 批量下载、tick 合成 K 线入库；新浪仅作备用。

### 当前进度

| 阶段 | 状态 | 说明 |
|---|---|---|
| 1 清洗与 vnpy 存储 | 已完成 | `clean.py`、`vnpy_store.py`，库 `data/ticks/vnpy_data.db` |
| 2 合成 K 线 | 已完成 | `synthesize.py` → `data/klines/tick_bars.sqlite`，键 `@tick` |
| 3 常驻采集 | 已完成代码 | `recorder.py`、CLI/数据管理页；盘中 5 分钟真人挂机未作为本轮证据 |
| 4 专业版下载 | 已完成代码 | `downloader.py`；真实 `tq_dl` 下载样本未交 |
| 5 天勤优先新浪备用 | 已完成 | `fallback_source`、`fallback_allow_entry` |
| 6 文档与版本 | 已完成离线部分 | 45.2.0；试用期四条人工清单仍待做 |

下一步只补人工证据，不再改默认开仓规则：盘中采集 5 分钟、专业版单月下载对照天勤 1 分钟。研究计划 P2.2 已可用合成 1 分钟做止损复核，P2.4 Tick 级执行复核仍未开工。

## 一、背景与目标

当前项目已接入天勤 K 线（`src/data/tq_source.py`）：`TqApi` 进程内复用、`get_kline_serial` 拉分钟/小时/日线、时间转北京时间并右标签。缺口是：

- 没有 tick 层：既没有盘中 tick 落库，也没有历史 tick 批量下载。
- 本地 K 线只来自行情商，缺少可自行积累、可追溯的 tick 基础数据。
- `data.source` 选天勤时下载失败直接报错，没有新浪备用。
- 未安装 vnpy，无法用官方 `save_tick_data()` 入库。

本方案要交付四条能力：

1. **常驻实时 tick 采集**：免费账号盘中订阅，落本地库，断线重连，挂机攒库。
2. **历史 tick 批量下载**：预留专业版接口，试用期按主力分段下载真实合约 tick。
3. **tick 合成 K 线**：由 tick 生成 1/5/15/30/60/日线，写入独立的 `tick_bars` 库供回测。
4. **天勤优先、新浪备用**：实盘继续用天勤 K 线；天勤异常时按配置回退新浪，并留下明确来源标记。

### 已确认取舍

- vnpy 装进主依赖（`vnpy>=4.5` + `vnpy_sqlite`）。
- 采集形态：CLI 常驻 + GUI 启停/状态。
- tick 合成 K 线：独立 `data/klines/tick_bars.sqlite`，来源标记为 `tick`。
- 试用期下载：先单品种单月验证，跑通后由配置放开全量。

### 明确不做

- 不追逐笔成交/逐笔委托（天勤不提供）。
- 不做价格复权，不为换月跳空做平滑；换月按真实合约分段保存。
- 不改现有 CTP 交易、下单、风控链路。
- 不绕过天勤权限限制；免费账号不尝试历史 tick 下载。
- 不在本轮申请或自动使用专业版试用。

## 二、数据流

```
盘中（免费账号）
  TqApi(独立连接) → get_tick_serial → 清洗 → 内存缓冲
      → vnpy save_tick_data() → data/ticks/vnpy_data.db
      → （可选）原始 csv.gz → data/ticks/raw/

收盘后/按需
  vnpy load_tick_data() → 1 分钟聚合 → bars_from_minutes() → 5/15/30/60
      → 按期货交易日聚合 → 日线
      → BarStore.upsert() → data/klines/tick_bars.sqlite（键：fu2701@tick）

试用期（专业版）
  query_his_cont_quotes → 主力分段 → DataDownloader(dur_sec=0)
      → 原始 csv（断点续传）→ 清洗 → vnpy save_tick_data()
      → 同上合成 K 线

实盘开仓
  天勤 K 线（主）→ 失败且允许回退 → 新浪 K 线（备用，标记 fallback）
```

## 三、模块与文件

### 新增 `src/data/tick/`

| 文件 | 职责 |
|---|---|
| `__init__.py` | 导出采集、下载、合成入口 |
| `clean.py` | tick 清洗：空值、非正价格、负成交量/持仓、`bid1 > ask1`、时间戳去重、排序；返回清洗统计 |
| `vnpy_store.py` | vnpy 官方库适配：设置项目内数据库路径、`TickData` 转换、批量 `save_tick_data()`、`load_tick_data()`、tick 库存概览；vnpy 缺失时给出明确安装提示 |
| `recorder.py` | 常驻采集引擎：独立 `TqApi` 连接、订阅、缓冲、定时批量入库、心跳状态文件、断线指数退避重连、优雅停止 |
| `synthesize.py` | tick → 1 分钟 → 多周期 K 线 → `tick_bars` 库；使用累计成交量差分和期货交易日归属 |
| `downloader.py` | 专业版历史 tick 批量下载：主力分段、日期分块、断点续传、权限错误提示、进度状态 |

### 修改

| 文件 | 变更 |
|---|---|
| `requirements.txt` | 增加 `vnpy>=4.5.0`、`vnpy_sqlite>=1.1.3` |
| `src/data/quote_check.py` | 新增来源代码 `tick`（库存后缀 `@tick`、界面名「tick合成」），保持与 `@tq`、新浪库存隔离 |
| `src/data/kline.py` | ① 增加只读的 `tick` 来源，从 `tick_bars` 库取数，不联网；② 天勤失败时按 `data.fallback_source` 回退新浪，标记 `fallback_from=tq`，健康状态记 `fallback` |
| `src/main.py` | 新增 `--tick-record`、`--tick-download`、`--tick-bars START END`、`--tick-status` |
| `app_entry.py` | `_want_gui()` 把上述 tick 参数视为命令行模式 |
| `src/ui/data_tab.py` | 增加「天勤 tick」卡片：采集启停、心跳/最新 tick/今日条数、合成 K 线按钮、tick 库存摘要 |
| `src/ui/app.py` | 注册/刷新 tick 状态变量 |
| `config/config.example.yaml` | 增加 `data.tick`、`data.fallback_source`、`data.tq.history_tick` |
| `.gitignore` | 确认 `data/` 已忽略；新增运行状态文件无需额外规则 |
| `CHANGELOG.md` / `VERSION` | 功能完成后升版本并记录 |

### 复用而非重写

- 合约与交易所映射：`tq_symbol()`、`EXCHANGE_BY_PRODUCT`、`config_symbol_from_tq()`（`src/data/tq_source.py`）。
- 1 分钟→多周期：`bars_from_minutes()`（`src/data/periods.py`），已处理右标签、开盘边、收盘半桶。
- 周线：`resample_ohlc()`。
- 日线归属：`futures_trading_day()`（`src/utils/trading_day.py`）。
- 本地 K 线写入/读取：`BarStore.upsert()` / `BarStore.load()`（`src/data/bar_store.py`）。
- 行情健康：`KlineProvider.fetch()` 的 `market_health` 结构。

## 四、关键正确性规则

1. **时区**：天勤 tick 的 `datetime` 是北京时间 naive。写入 `TickData` 时只附加 `Asia/Shanghai`，不改数值；vnpy 入库时自行转 UTC。读取后转回北京时间。
2. **累计成交量**：天勤 tick 的 `volume` 是当日累计值，**不能按 tick 求和**。单根 K 线成交量 = 该根最后一笔累计量 − 第一笔累计量；跨交易日或数值回落时按当日累计量重置处理，负值钳为 0。
3. **持仓量**：取该根最后一笔 `open_interest`。
4. **右标签**：K 线时间戳沿用现有口径（结束时刻），1 分钟由 `resample(label="right", closed="right")` 生成。
5. **日线归属**：夜盘 21:00 之后属于下一期货交易日，按 `futures_trading_day()` 分组，不按自然日。
6. **未收盘 K 线**：合成入库默认只写已完成的周期；最后一根形成中的 bar 不写库，避免回测混入半根。
7. **去重幂等**：vnpy 的 `save_tick_data()` 按 `(symbol, exchange, datetime)` upsert；采集缓冲在入库前再按时间戳去重。`BarStore.upsert()` 本身按 `(symbol, period, ts)` 覆盖。
8. **真实合约**：tick 一律按真实合约保存，不用主连代码入库；换月只按时间分段，不拼成一条假连续合约。
9. **采集连接隔离**：采集线程使用独立的 `TqApi`，不与 `KlineProvider` 共用；避免 `wait_update` 与 K 线拉取互相阻塞。连接失败按 5s→10s→30s→60s 退避。
10. **失败不阻断交易**：tick 落库失败只告警，不影响实盘 K 线、下单、止损和平仓。
11. **来源隔离**：tick 合成 K 线写 `@tick`；天勤官方 K 线写 `@tq`；新浪写原合约名。三者互不覆盖。
12. **回退可追溯**：天勤失败回退新浪时，`bars.attrs` 和健康记录必须带 `fallback_from=tq`、实际来源、失败原因；配置 `data.fallback_allow_entry` 控制回退行情是否允许新开仓。

## 五、配置设计

```yaml
data:
  source: "tq"                 # 主数据源改为天勤
  fallback_source: "akshare"   # 天勤失败时的备用（新浪）
  fallback_allow_entry: true   # 备用行情是否允许新开仓；false 时只允许平仓/止损
  tq:
    user: ""
    password: ""               # 建议放 config/secrets.local.yaml 或环境变量
    history_tick:
      enabled: false           # 专业版试用期再打开
      main_cont_list:          # KQ.m@交易所.品种
        - "KQ.m@SHFE.fu"
      start: "2026-10-01"
      end: "2026-10-31"
      chunk_days: 1            # 每段下载天数，断点续传粒度
      out_dir: "data/ticks/history"
      keep_raw_csv: true
  tick:
    enabled: false             # 默认不自动常驻，避免未配置账号时反复重连
    symbols: []                # 空=用配置页关注品种；也可填真实合约
    resolve_main: true         # 品种代码自动解析当前主力真实合约
    flush_seconds: 5
    batch_size: 2000
    database: "data/ticks/vnpy_data.db"
    raw_csv: false             # 额外保留原始 csv.gz
    status_file: "data/ticks/recorder_status.json"
    reconnect_seconds: 5
    max_backoff_seconds: 60
    bar_store: "data/klines/tick_bars.sqlite"
    synth_periods: ["1", "5", "15", "30", "60", "D"]
```

所有新增键在代码里都有默认值，旧配置不改也能启动；`source` 的默认值仍保持兼容，但示例配置和 GUI 默认改为天勤。

## 六、实施步骤

### 阶段 1：依赖与存储底座

1. `requirements.txt` 增加 vnpy/vnpy_sqlite，执行 `pip install -r requirements.txt`。
2. 新建 `src/data/tick/vnpy_store.py`：
   - 在导入 `vnpy_sqlite` 前设置 `SETTINGS["database.database"]` 为项目内绝对路径（vnpy 的 `get_file_path` 对绝对路径直接使用）。
   - `to_tick_data(frame, symbol, exchange)`：附加 `Asia/Shanghai`，天勤缺失字段填 0（2~5 档盘口、turnover、涨跌停）。
   - `save_ticks(ticks)`：按批调用 `get_database().save_tick_data()`。
   - `load_ticks(symbol, exchange, start, end)`：返回带时区的 tick 列表，供合成使用。
3. 测试：字段映射、时区数值不变、重复时间戳 upsert、空列表不写库。

### 阶段 2：清洗与 tick 合成 K 线

1. `clean.py` 实现文档中的清洗规则并返回 `{raw, kept, dropped, reasons}`。
2. `synthesize.py`：
   - 从 vnpy 库按区间读 tick，清洗后生成 1 分钟 OHLC（open=first last_price、high/low=极值、close=last、volume=累计量差分、oi=last）。
   - 5/15/30/60 用 `bars_from_minutes()`；日线按 `futures_trading_day()` 分组；周线用 `resample_ohlc()`。
   - 写 `data/klines/tick_bars.sqlite`，键为 `<symbol>@tick`，调用 `BarStore.upsert()`。
   - 记录合成审计：区间、tick 条数、各周期根数、丢弃原因统计。
3. `quote_check.py` 增加 `tick` 来源与 `@tick` 后缀；`KlineProvider` 增加只读 tick 分支（不联网、不参与实盘自动回退）。
4. 测试：累计量差分、跨交易日重置、夜盘归属、右标签、只写完整 bar、重复合成幂等。

### 阶段 3：常驻 tick 采集

1. `recorder.py` 实现：
   - `TickRecorder(cfg)`，独立线程 + 独立 `TqApi`。
   - 订阅：真实合约直接订阅；品种代码经 `main_contract()` 解析为当前主力，每日开盘前重解析一次，换月后新合约自动订阅。
   - 主循环 `wait_update()`，`is_changed()` 后取最新行；按 `(symbol, datetime)` 去重后进缓冲。
   - 定时/定量 flush 到 vnpy 库；失败保留缓冲并告警。
   - 心跳写 `recorder_status.json`：状态、连接时间、订阅合约、今日条数、最后 tick 时间、错误。
   - `stop()` 触发后先 flush 再关闭连接。
2. CLI：`python app_entry.py --tick-record`；`--tick-status` 打印心跳。
3. GUI：数据管理页增加启停按钮与状态标签，定时刷新心跳（复用现有页签刷新节奏）。
4. 测试：假 api 和假 tick 序列，验证订阅、去重、flush、重连、停止、心跳；全程不联网。

### 阶段 4：专业版历史 tick 下载

1. `downloader.py` 实现：
   - `query_his_cont_quotes("KQ.m@EX.product", n=9999)` 取历史主力；按切点切成 `(真实合约, 起, 止)`。
   - 每段再按 `chunk_days` 切块，`DataDownloader(dur_sec=0)` 下载到 `out_dir/<product>/<contract>/`。
   - 断点续传：`download_manifest.json` 记录已完成块；已存在且非空的 csv 直接跳过；下载失败重试有限次数后记录待续。
   - 权限不足时捕获异常并提示「需要天勤专业版/试用权限」，不反复重试。
   - 进度回调写状态文件，CLI 打印百分比。
2. 下载后处理：原始 csv → 清洗 → `save_ticks()` → 合成 K 线 → 更新 manifest 状态。
3. CLI：`--tick-download`（读配置）；支持 `--dry-run` 只列计划不下载。
4. 测试：假 api 返回主力分段、假下载器写文件；验证分段、续传、跳过、权限错误。

### 阶段 5：天勤优先与新浪备用

1. `KlineProvider`：天勤失败且 `fallback_source` 可用时，改拉新浪；返回数据标记 `fallback_from=tq`、`fallback_reason`、`source=akshare`。
2. 行情健康：新增 `status="fallback"`，保留天勤失败原因与耗时。
3. 风控：`fallback_allow_entry=false` 时，回退行情只更新持仓保护，不开新仓。
4. 数据仍按实际来源分库存放，不把新浪 K 线写进 `@tq`。
5. 测试：假天勤抛错 → 假新浪返回；验证标记、库存隔离、允许/禁止开仓两种配置。

### 阶段 6：联调、文档与版本

1. 全量 `pytest`、`ruff`、`mypy`。
2. 本地不联网验证：`--tick-status`、tick 合成、tick 来源回测读取。
3. 更新 `config.example.yaml`、README 数据源说明、`CHANGELOG.md`、`VERSION`。
4. 试用期人工验收清单（见下）。

## 七、验证方案

### 自动化（不联网）

```powershell
python -m pytest -q tests/test_tick_store.py tests/test_tick_clean.py tests/test_tick_synth.py tests/test_tick_recorder.py tests/test_tick_downloader.py tests/test_kline_fallback.py
python -m pytest -q
python -m ruff check src tests
python -m mypy src
```

### 本地手工验证

1. `python app_entry.py --tick-status` 能读到/提示无心跳。
2. 用一份合成 tick 数据跑 `--tick-bars`，`data/klines/tick_bars.sqlite` 出现 `@tick` 键，时间戳右标签、夜盘归属正确。
3. 回测把 `data.source` 设为 `tick`，能读到合成 K 线且不联网。
4. 免费账号盘中运行 `--tick-record` 5 分钟，心跳更新、库里有 tick、断开网络后能重连并续写。
5. 天勤账号故意填错，验证界面/日志出现可读错误，且不阻塞主交易循环。

### 试用期人工验收（需专业版权限，另附证据）

1. 单品种单月下载成功，原始 csv、清洗统计、入库条数一致。
2. 中断下载后重启，已完成块不重下、未完成块继续。
3. tick 合成 1 分钟 K 线与天勤官方 1 分钟 K 线抽样对照，差异有解释（成交量口径、集合竞价、最后一根）。
4. 试用到期后本地数据仍可读、可回测。

## 八、风险与限制

- **依赖体积**：vnpy 会带入 PySide6、ta-lib 等，安装体积明显增大；不影响现有 Tkinter 界面。
- **连接数**：采集与 K 线各用一个 `TqApi` 连接，免费账号并发连接数若受限，需要改为单连接事件循环；本方案先在采集器内隔离，失败时降级为按需采集。
- **tick 口径**：天勤只有一档盘口快照，滑点仿真会偏乐观；成交质量评估仍按原计划等待模拟成交样本。
- **免费账号攒库从启用当天开始**，无法回溯补历史；历史只能依赖专业版试用。
- **试用窗口有限**：先单品种单月验证，再按配置扩品种和年限，避免试用期被大任务占满。
- **换月拼接**：只按时间拼接真实合约，不做复权；回测需按换月分段解释跳空。
- **回退行情**：新浪与天勤时间戳、成交量口径不同，回退时信号可能与主源不一致；默认允许开仓但必须留痕，可按配置收紧。

## 九、交付顺序与验收

1. 阶段 1-2 完成后，tick 入库与合成链路可离线验收。
2. 阶段 3 完成后，免费账号可挂机攒 tick。
3. 阶段 4 完成后，试用期可批量下载；真实下载需另行提供权限与人工证据。
4. 阶段 5 完成后，实盘数据源切换为天勤优先、新浪备用。
5. 全部测试通过后才更新版本号与 CHANGELOG；不自动 commit/push。
