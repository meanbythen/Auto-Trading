# 审查报告 §2 第五批（P3/P4 收尾）规格

- 基线版本：49.22.0（2026-11-21）
- 范围来源：`docs/项目全面审查报告-2026-11-21.md` §2 中 P3/P4 的 5 处未完成项
- 目标：统一结果口径、给研究运行补 K 线内容哈希、接通已实现但无调用方的可观测接口、启动自动刷新数据目录、把插件参数校验统一到界面保存入口。
- 原则：能复用就复用，不新建抽象；不改 JSON 账本 / SQLite 投影的权威关系；不动开平仓规则；不修改用户未跟踪文档 `docs/说明文本改进.md`；不 commit / 不 push。

---

## 1. 决策摘要

| ID | 决策 | 是否改变交易/回测结果 |
|----|------|------------------------|
| R1 | `result_summary` 扩展为可接受“结果对象 / Performance / performance 字典”，并补展示字段；回测结果页的核心口径改为从它取值 | 否（仅展示口径） |
| R2 | `research_runs` 增加 `data_sha256`（K 线内容哈希）；清单顶层同步写 `data_sha256`，主回测链路用 `fingerprint_bars()` 计算 | 否（只增留痕字段） |
| R3 | 给 `trace_events()` / `metrics_snapshot()` 接上真实调用方：启动日志汇总 + 运维查询工具 | 否（只读） |
| R4 | 启动时由“只刷合约快照”改为刷新合约 + K 线目录 + tick 目录 | 否（只写 `data_catalog`） |
| R5 | 新增 `validate_strategy_params(cfg)`，配置页保存时统一调用 `plugins.validate_params()` 并记录告警 | 否（只做提示） |

版本号：`49.23.0`（新增字段、工具与启动行为，属功能更新）。

---

## 2. R1 统一 result_summary

### 2.1 `src/research/kernel.py::result_summary`

改为可接受三种输入：带 `.performance` 的结果对象、Performance 对象本身、performance 字典。返回固定键：

```python
{
    "trades", "net_pnl", "win_rate", "profit_factor", "max_drawdown",
    "wins", "losses", "avg_win", "avg_loss", "payoff", "expectancy",
    "max_drawdown_pct",
}
```

- 输入缺失或为 `None` 时，全部返回 0 / 0.0，保持现有零值结构。
- 取值统一走一个内部 `_get(source, key, default)`：字典用 `.get()`，对象用 `getattr(..., default)`。
- `expectancy` 保留 `None` 原值（标志着“样本不足算不出”），不强行转 0。
- 现有五个键含义不变，`tests/test_research_kernel.py` 的断言继续成立。

### 2.2 `src/ui/backtest_actions.py::_format_result_report`

- 在 `p = result.performance` 旁新增 `summary = result_summary(result)`（延迟导入，避免 UI 顶层依赖）。
- 报告文本中的 `成交`、`胜率`、`净盈亏`、`盈利因子`、`最大回撤` 改为读 `summary`。
- `wins/losses/avg_win/avg_loss/payoff/expectancy/max_drawdown_pct` 仍由 `p` 提供（这些不是统一口径键），不做无关改动。
- 用途：回测结果页与策略编辑器共用同一份报告，改一处两处生效。

### 2.3 优化 / 复盘

- 优化链路（`src/backtest/optimizer.py`）本就统一用 `calc_performance()`，行内是 `perf.to_dict()`，不重复改造口径。
- 复盘（`src/review/replay.py`）按点数统计、不含手续费滑点，属已标注的独立口径（报告 B8），不强行套金额型 `result_summary`。
- 结论写入架构文档与报告，避免“均走 result_summary”的过期表述。

### 验收

- `result_summary(result)` 与 `result.performance` 的 core 五键相等。
- `result_summary(perf)`、`result_summary(perf.to_dict())` 与上者一致。
- `result_summary(None)` 全零且键齐全。
- 回测结果页文本中的净盈亏/成交笔数等于 `result_summary`。

---

## 3. R2 research_runs 补 K 线内容哈希

### 3.1 清单

`src/research/kernel.py::build_run_manifest` 新增可选参数 `data_sha256: str = ""`，写入清单顶层 `"data_sha256"`；为空时不报错。

### 3.2 计算

- 主回测 `src/backtest/engine.py` 生成清单处（约 1874-1906）：用已加载的 K 线 DataFrame 调 `src/backtest/experiment.py::fingerprint_bars()`，取其 `"sha256"` 传入；取不到时传空串，不影响回测。
- 其它研究入口（复盘 `replay.py`、选股 `picker.py`、规律 `scan.py`、换月 `rollover.py`）在已有 K 线帧时同样传，拿不到就留空。

### 3.3 落库

- `src/account/schema.sql` 的 `research_runs` 增加 `data_sha256 TEXT NOT NULL DEFAULT ''`。
- `src/ops/journal.py::record_research_run`：
  - 写入前确保列存在：`PRAGMA table_info(research_runs)`，缺列则 `ALTER TABLE research_runs ADD COLUMN data_sha256 TEXT NOT NULL DEFAULT ''`；失败只记 warning，不阻断。
  - INSERT 列与参数增加 `data_sha256`，取 `manifest.get("data_sha256")`。
  - 旧库首次写入自动补列；新库由 schema.sql 建好。

### 验收

- 传入 `data_sha256` 的清单落库后，`SELECT data_sha256 FROM research_runs` 取回同一值。
- 不传时为空串，不报错。
- 老库（无该列）首次 `record_research_run` 后列自动出现并可查询。

---

## 4. R3 接通 trace_events / metrics_snapshot

### 4.1 启动汇总（生产调用方）

`src/main.py` 在日志就绪、配置加载后，`try/except` 调 `src.ops.journal.metrics_snapshot()`（当天），把 `orders_sent / fills / rejected / avg_send_ms / avg_fill_ms` 记成一条 info 日志，读不到就跳过。不改交易流程。

### 4.2 运维查询工具

新增 `tools/ops_report.py`：

- `--day YYYY-MM-DD`：打印 `metrics_snapshot(day)` 中文键值；
- `--trace TRACE_ID`：打印 `trace_events(trace_id)` 时间线（信号 → 风控 → 审计）；
- `--root`：项目根，默认当前目录。
- 无参数时打印当天指标。只读，不写库。

---

## 5. R4 启动自动刷新 K 线/tick 目录

`src/main.py` 的 P3 启动块由 `refresh_instruments(root, cfg)` 改为 `refresh_all(root, cfg)`：

- 继续用 `try/except` 包住，失败只 warning；
- 日志分别输出合约快照 ID、K 线写入条数、tick 写入条数；
- `refresh_all` 内各函数已有“SQLite 关闭 / 库不存在返回 0”的静默语义，无需改 `catalog.py`。

### 验收

- SQLite 开启时，启动后 `data_catalog` 同时含 `kline`、`tick` 与合约快照。
- SQLite 关闭或行情库不存在时返回 0，不报错、不建库。

---

## 6. R5 策略参数 schema 的 UI 校验统一

### 6.1 `src/strategy/factory.py`

新增：

```python
def validate_strategy_params(cfg: dict[str, Any]) -> list[str]:
    """按当前 strategy.engine 的插件参数 schema 校验配置；只返回消息，不抛异常。"""
```

- 引擎名走与 `create_strategy` 相同的别名归一与 `_REMOVED_ENGINES` 处理；
- 取 `param_section(engine)` 对应段落，缺失或非字典返回 `[]`；
- 调 `plugins.validate_params(engine, params)`；非插件引擎返回 `[]`。

### 6.2 `src/ui/config_collect.py::_apply_ui_to_cfg`

- 在 `sanitize_strategy_config(self.cfg)` 之后调用 `validate_strategy_params(self.cfg)`；
- 每条消息 `logger.warning("策略参数与 schema 不一致: {}", msg)`；
- 结果存入 `self._strategy_param_warnings`（列表），供后续界面提示复用；不阻断保存。

### 验收

- 插件参数类型写错时，`validate_strategy_params()` 返回中文消息；
- 正确参数返回空列表；
- 内置策略 / rule_dsl / 未知引擎返回空列表，不误报。

---

## 7. 回归测试

新增 `tests/test_review_7_5_consistency.py`：

1. R1：结果对象、Performance、字典三种输入的 `result_summary` 一致；`None` 全零。
2. R1：`_format_result_report` 文本中的净盈亏/成交笔数与 `result_summary` 相等。
3. R2：`build_run_manifest(data_sha256=...)` 清单含该值。
4. R2：`record_research_run` 落库后 `data_sha256` 可查；老库自动补列。
5. R3：`metrics_snapshot` 能从临时 JSONL 聚合出订单/成交/告警数。
6. R3：`trace_events` 按 trace 聚合信号与审计（用临时库写入）。
7. R4：`refresh_all` 在 SQLite 关闭时返回三个 0 分支。
8. R5：插件参数类型错误返回消息；正确参数返回空。

---

## 8. 验证与交付

```powershell
python -m pytest -q
lint-imports
python -m ruff check src tests
python -m mypy src
python -c "import src.ui.app; print('ok')"
```

版本与文档：

1. `VERSION` → `49.23.0`（单行）。
2. `CHANGELOG.md` 顶部新增 `## 49.23.0 — 2026-11-21`，中文逐条。
3. `README.md` 第 3 行与版本说明改为 `49.23.0`。
4. `docs/项目全面审查报告-2026-11-21.md` 增加 `§0.5 第五批修复复核（49.23.0）`，并更新 §2 对应行状态。
5. `docs/系统架构与演进方案.md` 更新 §1.8（result_summary / K 线哈希）与 §0 表述。

不 commit、不 push。

---

## 9. 需要确认

1. 版本号 `49.23.0`。
2. R2 采取“清单 + `research_runs.data_sha256` 列，老库首次写入自动 ALTER”的最小迁移方式。
3. R3 以“启动日志汇总 + `tools/ops_report.py`”作为调用方，不新增界面页。
