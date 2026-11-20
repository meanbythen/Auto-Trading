# P4 规格：研究内核统一 + 可观测性

对应文档：`docs/系统架构与演进方案.md` 的 2.10 / 2.11 / P4（L465-L482、L589-L593）。
状态：已完成（2026-11-21）。
范围约束：只做「统一运行清单 + 结果口径 + 日志/审计/追踪」，不重构调度、不改策略计算、不动 P3 已完成的插件与数据目录。

---

## 1. 目标与验收

### 1.1 目标

1. 回测、交叉测试、优化、复盘、规律扫描共享同一份「研究运行清单」（run manifest）。
2. 同一策略、同一份 K 线，在各页得到一致的净盈亏与交易笔数；差异只允许来自显式传入的配置（手数、资金、滑点等）。
3. 结构化日志、`trace_id`、审计表、`research_runs`、`signals` 落库。
4. 一笔单可从 `signals` 追到 `audit_log` 的报单/成交/风控决策。

### 1.2 验收（可执行）

- `run_backtest(..., source="backtest")` 返回结果带 `result.run`，字段完整且 `run_id` 稳定写入 `research_runs`。
- 交叉测试表格行的 `trades`、`net_pnl` 与 `result.performance` 完全相等（由同一取值函数产出）。
- 用同一 `trace_id` 依次写 `signal_generated`、`risk_decision`、`order_submitted`、`order_filled` 后，`trace_events(trace_id)` 按时间返回四类事件。
- `setup_logger(tmp)` 产出 `logs/structured_YYYY-MM-DD.jsonl`，每行含 `record.extra.trace_id`。
- 连续两次 `trading_db.connect()` 不报错，新增表存在（幂等建表）。
- `pytest`、`ruff`、`mypy` 通过。

---

## 2. 设计决策

### 2.1 目录与依赖

| 新增/修改 | 位置 | 说明 |
|---|---|---|
| trace 上下文 | `src/kernel/trace.py`（新增） | 仅标准库；内核可被所有层依赖 |
| 研究运行清单 | `src/research/kernel.py`（新增） | 组装 manifest、统一结果摘要，不直接写库 |
| 审计/信号/运行落库 | `src/ops/journal.py`（新增） | 写 `data/trading.db`，失败只记日志 |
| 表结构 | `src/account/schema.sql` | 新增 3 张表 + `risk_decisions.trace_id` |
| 列迁移 | `src/account/sqlite_migrate.py` | `_ensure_columns` 补 `risk_decisions.trace_id` |

依赖方向保持现有约束：`src.kernel` 不依赖业务模块；`src.research`、`src.ops` 不依赖 `src.scheduler` / `src.ui`。

### 2.2 运行清单字段

`build_run_manifest()` 返回：

```python
{
    "run_id": "RS-20261121T153000-1a2b3c4d",
    "trace_id": "OT-...",              # 未设置上下文时新生成
    "source": "backtest",              # backtest/cross_test/optimizer/review/regularity
    "status": "ok",                    # ok/failed
    "started_at": "2026-11-21T15:30:00+08:00",
    "finished_at": "2026-11-21T15:30:12+08:00",
    "duration_ms": 12034.5,
    "strategy": {"id": "...", "version": "...", "engine": "..."},
    "params_sha256": "<64 hex>",
    "data_catalog": {"...": "..."},    # 复用 P3 catalog_reference
    "symbols": ["rb0"],
    "period": "15",
    "commit": {"version": "49.18.2", "git_commit": "...", "git_dirty": false},
    "metrics": {"bars": 1000, "trades": 42, "net_pnl": 1234.5},
    "message": "",
    "extra": {},
}
```

规则：

- `run_id` 由调用方传入时原样使用；否则 `new_run_id(source)` 生成。
- `started_at` 由 `datetime.now().astimezone()` 生成；`duration_ms` 由调用方计时后传入。
- `strategy` / `params_sha256` / `data_catalog` 允许调用方传入已算好的值，避免重复计算。
- 所有值必须可 JSON 序列化（复用 `experiment._json_safe` 思路，不额外造轮子）。

### 2.3 结果结构统一

- `BacktestResult` 增加 `run: dict[str, Any] = field(default_factory=dict)`，`to_dict()` 增加 `"run": self.run`。
- `StrategyReplay` 增加 `run: dict[str, Any] = field(default_factory=dict)`。
- `result_summary(result) -> dict` 固定返回：
  `{"trades", "net_pnl", "win_rate", "profit_factor", "max_drawdown"}`，全部来自 `result.performance`。
- 交叉测试行、优化结果展示、复盘摘要都通过 `result_summary` 取值，禁止各页自行四舍五入或改口径。

### 2.4 trace_id 语义

- 前缀沿用 `OT-`，由 `src.kernel.trace.new_trace_id()` 唯一实现；`src/trader/order_timeline.py` 改为从这里导入并继续导出同名函数，避免两套编号。
- `contextvars.ContextVar` 保存当前 `trace_id`；提供 `get_trace_id()`、`set_trace_id()`、`ensure_trace_id()`、`bind_trace()`。
- 调度线程在生成非 HOLD 信号时设置上下文；报单、风控、成交、日志自动带同一个 `trace_id`。
- 线程边界（CTP 回调线程）不依赖 contextvar：成交侧优先读 `decision.meta["trace_id"]` 或回报里的 `trace_id`，与现有 `fill_reconcile.py` L507 做法一致。

### 2.5 日志与审计分层

| 通道 | 内容 | 实现 |
|---|---|---|
| 控制台/文本日志 | 人读 | 现有 loguru 保留 |
| 结构化日志 | 机器读，含 trace_id | 新增 `logs/structured_{time}.jsonl`，`serialize=True` |
| `data/order_timeline.jsonl` | 订单阶段证据 | 现有，不合并 |
| SQLite `signals` | 策略信号事实 | 新增 |
| SQLite `audit_log` | 信号→风控→报单→成交审计 | 新增 |
| SQLite `research_runs` | 研究运行清单 | 新增 |
| `data/alerts.jsonl` | 需人工看的告警 | 现有，不重复造 |

---

## 3. 详细改动

### 3.1 新增 `src/kernel/trace.py`

```python
def new_trace_id() -> str: ...
def get_trace_id() -> str: ...
def set_trace_id(trace_id: str) -> str: ...
def ensure_trace_id() -> str: ...
@contextmanager
def bind_trace(trace_id: str | None = None) -> Iterator[str]: ...
```

- 空值统一返回 `""` / 自动生成，不抛异常。
- 仅使用 `contextvars`、`uuid`、`contextlib`、`datetime`。

### 3.2 新增 `src/research/kernel.py`

```python
def new_run_id(source: str) -> str: ...
def build_run_manifest(
    source: str,
    cfg: dict[str, Any] | None,
    *,
    symbols: list[str] | None = None,
    period: str = "",
    started_at: str = "",
    duration_ms: float | None = None,
    status: str = "ok",
    message: str = "",
    run_id: str | None = None,
    strategy: dict[str, Any] | None = None,
    data_catalog: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]: ...
def result_summary(result: Any) -> dict[str, float | int]: ...
def record_run(manifest: dict[str, Any], *, root: str | Path | None = None) -> bool: ...
```

- `build_run_manifest` 内部按需延迟导入：`strategy_identity`、`catalog_reference`、`sanitize_config`、`config_sha256`、`_code_identity`。
- 任何延迟导入失败都降级为空 dict/空串，不阻断回测。
- `record_run` 转调 `src.ops.journal.record_research_run`。

### 3.3 新增 `src/ops/journal.py`

```python
def db_path_for(root: str | Path | None = None) -> Path: ...
def record_signal(
    *,
    trace_id: str,
    account_id: str = "",
    strategy_id: str = "",
    symbol: str,
    bar_time: str = "",
    action: str,
    price: float = 0.0,
    score: float = 0.0,
    reason: str = "",
    payload: dict[str, Any] | None = None,
    root: str | Path | None = None,
) -> bool: ...
def record_audit(
    event: str,
    *,
    message: str = "",
    trace_id: str = "",
    account_id: str = "",
    strategy_id: str = "",
    symbol: str = "",
    order_id: str = "",
    payload: dict[str, Any] | None = None,
    root: str | Path | None = None,
) -> bool: ...
def record_research_run(manifest: dict[str, Any], *, root: str | Path | None = None) -> bool: ...
def trace_events(trace_id: str, *, root: str | Path | None = None) -> list[dict[str, Any]]: ...
def metrics_snapshot(day: str = "", *, root: str | Path | None = None) -> dict[str, Any]: ...
```

- 写库使用 `src.account.trading_db.connect` + `DB_LOCK`，短连接、自动建表。
- 所有写函数 `try/except Exception`，`logger.warning` 后返回 `False`，绝不影响交易/研究主流程。
- `signal_id` 生成规则：`f"{trace_id}:{symbol}:{bar_time}:{action}"`；重复写入用 `INSERT OR REPLACE`。
- `audit_log` 自增主键，纯追加。
- `trace_events` 把 `signals`、`risk_decisions`、`audit_log` 三处按 `ts` 合并排序，供「一笔单全链路」查询。
- `metrics_snapshot` 从 `order_timeline.jsonl` 与 `alerts.jsonl` 聚合：
  `orders_sent`、`fills`、`rejected`、`avg_send_ms`、`avg_fill_ms`、`avg_slippage`、`freeze_alerts`、`reconcile_alerts`；文件缺失返回零值，不报错。

### 3.4 `src/account/schema.sql`

新增（全部 `IF NOT EXISTS`）：

```sql
CREATE TABLE IF NOT EXISTS research_runs (
    run_id          TEXT PRIMARY KEY,
    trace_id        TEXT NOT NULL DEFAULT '',
    source          TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT '',
    started_at      TEXT NOT NULL DEFAULT '',
    finished_at     TEXT NOT NULL DEFAULT '',
    duration_ms     REAL NOT NULL DEFAULT 0,
    strategy_id     TEXT NOT NULL DEFAULT '',
    strategy_version TEXT NOT NULL DEFAULT '',
    params_sha256   TEXT NOT NULL DEFAULT '',
    period          TEXT NOT NULL DEFAULT '',
    symbols_json    TEXT NOT NULL DEFAULT '[]',
    commit_hash     TEXT NOT NULL DEFAULT '',
    message         TEXT NOT NULL DEFAULT '',
    payload_json    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_research_runs_source ON research_runs(source, started_at);

CREATE TABLE IF NOT EXISTS signals (
    signal_id    TEXT PRIMARY KEY,
    trace_id     TEXT NOT NULL DEFAULT '',
    account_id   TEXT NOT NULL DEFAULT '',
    strategy_id  TEXT NOT NULL DEFAULT '',
    symbol       TEXT NOT NULL DEFAULT '',
    bar_time     TEXT NOT NULL DEFAULT '',
    action       TEXT NOT NULL DEFAULT '',
    price        REAL NOT NULL DEFAULT 0,
    score        REAL NOT NULL DEFAULT 0,
    reason       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_signals_trace ON signals(trace_id);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    trace_id     TEXT NOT NULL DEFAULT '',
    account_id   TEXT NOT NULL DEFAULT '',
    strategy_id  TEXT NOT NULL DEFAULT '',
    symbol       TEXT NOT NULL DEFAULT '',
    order_id     TEXT NOT NULL DEFAULT '',
    event        TEXT NOT NULL,
    message      TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_audit_trace ON audit_log(trace_id);
CREATE INDEX IF NOT EXISTS ix_audit_event ON audit_log(event, ts);
```

`risk_decisions` 增加列：

```sql
trace_id TEXT NOT NULL DEFAULT ''
```

已有库由 `sqlite_migrate._ensure_columns(conn, "risk_decisions", {...})` 补列。

### 3.5 `src/backtest/models.py` 与 `src/backtest/engine.py`

- `models.py`：新增 `run` 字段与 `to_dict()` 输出。
- `engine.py::run_backtest` 新增仅关键字参数：

```python
source: str = "backtest",
run_id: str | None = None,
```

- 进入函数时记录 `started_perf = time.perf_counter()`、`started_at = datetime.now().astimezone().isoformat(timespec="seconds")`。
- 现有 `details` 计算完成后构建：

```python
manifest = build_run_manifest(
    source, cfg,
    symbols=[symbol], period=tf,
    started_at=started_at,
    duration_ms=(time.perf_counter() - started_perf) * 1000.0,
    run_id=run_id,
    strategy=details.get("strategy"),
    data_catalog=details.get("data_catalog"),
    metrics={"bars": max(0, n - int(trade_from)), "trades": perf.trades, "net_pnl": perf.net_pnl},
    extra={"data_source": details.get("data_source"), "ran_at": ran_at},
)
```

- `BacktestResult(..., run=manifest)`。
- `save_backtest_report` 的 `details` 增加 `"run": result.run`。
- 不在 `run_backtest` 内自动落库，避免优化器每次试参都写库；落库由入口决定。

### 3.6 `src/screen/picker.py`

- `_fill_backtest_row` 改为先用 `result_summary(result)`：

```python
summary = result_summary(result)
row["trades"] = int(summary["trades"])
row["net_pnl"] = float(summary["net_pnl"])
row["win_rate"] = float(summary["win_rate"])
row["profit_factor"] = float(summary["profit_factor"])
row["max_drawdown"] = float(summary["max_drawdown"])
```

  其余字段（payoff/avg_win/losses/expectancy/score）保持现状。
- 行增加 `row["run_id"] = str((getattr(result, "run", {}) or {}).get("run_id") or "")`。
- `run_cross_test(...)` 增加关键字参数 `source: str = "cross_test"`，调用 `runner(..., source=source)`。
  - 若自定义 runner 不接受该参数，测试应同步调整；实现时先跑相关测试确认。
- 交叉测试结束时构建一份聚合清单并 `record_run`：`source="cross_test"`、`symbols=本轮品种`、`period=本轮周期`、`metrics={"rows": len(rows), "errors": 错误行数, "trades": 合计}`；`status="ok"`。
- 单行失败只写 `row["error"]`，不影响聚合清单落库。

### 3.7 `src/backtest/optimizer.py`

在三个返回最终结果的 `run_backtest` 调用（L1808、L1849、L1992 附近）统一传：

```python
source="optimizer"
```

- 仅最终结果带 `source="optimizer"`；试参循环保持 `"backtest"`，避免污染运行清单。
- 优化完成后由调用方对 `best_result.run` 调 `record_run`；若返回对象无 `run` 则跳过。

### 3.8 `src/review/replay.py`

- `StrategyReplay` 增加 `run: dict[str, Any] = field(default_factory=dict)`。
- `replay_strategy` 开始时 `started_perf/started_at`，返回前构建：

```python
result.run = build_run_manifest(
    "review", cfg,
    symbols=[symbol], period=str(getattr(cfg, "get", lambda *_: "")("strategy", {}).get("timeframe", "")),
    ...
    metrics={"events": len(result.events)},
)
record_run(result.run, root=cfg.get("_project_root"))
```

- 复盘不产生成交，`metrics` 只记事件数与 `opened`。

### 3.9 `src/research/scan.py`

- `scan_regularities` 增加仅关键字参数：

```python
cfg: dict[str, Any] | None = None,
period: str = "",
record: bool = False,
```

- 函数结束时若 `record and cfg is not None`，构建并 `record_run`：
  `source="regularity"`、`symbols=list(bars_by_symbol)`、`metrics={"symbols": n, "regularities": len(out)}`。
- 返回值保持 `list[Regularity]` 不变；UI 现有调用不受影响，后续可显式传 `record=True`。

### 3.10 实盘信号与审计接入

#### `src/scheduler/runner.py`

新增私有方法：

```python
def _journal_signal(self, decision: StrategyDecision, bar_time: str) -> str:
    """非 HOLD 决策落 signals + audit；返回 trace_id。"""
```

- 取 `trace_id = ensure_trace_id()`，写回 `decision.meta["trace_id"]`。
- `account_id` 取 `getattr(self, "account_id", "")`；`strategy_id` 取 `getattr(self.strategy, "name", type(self.strategy).__name__)`。
- 调 `journal.record_signal(...)` 与 `journal.record_audit("signal_generated", ...)`。
- 在 `scan_symbol` 内、`decision` 经交割月闸门处理之后、`log_scan_brief` 之前调用：

```python
if decision.action != StrategyAction.HOLD:
    trace_id = self._journal_signal(decision, signal_bar_time or str((decision.meta or {}).get("bar_id") or ""))
```

- 三个提前返回分支（行情过期、回退禁止开仓、滑点过大）在现有 `_record_event(...)` 后追加：

```python
journal.record_audit("signal_blocked", trace_id=..., symbol=symbol, message=reason, payload={"gate": "stale"|"fallback"|"slippage"})
```

#### `src/scheduler/order_executor.py`

- `_send_order` 中 `trace_id = self._timeline_trace(decision)` 之后：

```python
journal.record_audit("order_submitted", trace_id=trace_id, symbol=decision.symbol,
                     strategy_id=..., order_id="", message=decision.reason,
                     payload={"volume": int(vol), "price": price, "side": decision.action.value})
```

- `result` 返回后：

```python
journal.record_audit(
    "order_accepted" if bool(getattr(result, "ok", False)) else "order_rejected",
    trace_id=trace_id, symbol=decision.symbol, order_id=str(getattr(result, "order_id", "") or ""),
    message=str(getattr(result, "message", "") or ""),
    payload={"assumed_fill": bool(getattr(result, "assumed_fill", False))},
)
```

#### `src/scheduler/fill_reconcile.py`

- `_apply_fill_accounting` 开头（拿到 `trace_id` 与 `fill_price` 后）写：

```python
journal.record_audit("order_filled", trace_id=trace_id, symbol=symbol,
                     order_id=..., message=decision.reason,
                     payload={"volume": vol, "fill_price": fill_price, "fill_key": fill_key})
```

- 成交侧 `trace_id` 取值顺序：`meta["trace_id"]` → `报告["trace_id"]` → 空串；空串时仍记录，不伪造关联号。

#### `src/risk/service.py` 与 `state_store.py`

- `record_decision` 构造的 `record` 增加 `"trace_id": get_trace_id()`。
- `append_decision` SQL 增加 `trace_id` 列与参数。
- `list_decisions` 返回值已含该列，无需改签名。

### 3.11 `src/utils/logger.py`

```python
from src.kernel.trace import get_trace_id

def _inject_trace(record: dict[str, Any]) -> None:
    record["extra"].setdefault("trace_id", get_trace_id() or "-")

logger.configure(patcher=_inject_trace)
```

- 控制台格式追加 `| <cyan>{extra[trace_id]}</cyan>`。
- 新增 JSONL sink：

```python
logger.add(
    Path(log_dir) / "structured_{time:YYYY-MM-DD}.jsonl",
    level=level, rotation="00:00", retention="14 days",
    encoding="utf-8", enqueue=True, serialize=True,
)
```

### 3.12 `src/trader/order_timeline.py`

- 删除本地 `new_trace_id` 实现，改为：

```python
from src.kernel.trace import new_trace_id  # noqa: F401  旧调用方继续从这里导入
```

- `record()` 行为不变。

---

## 4. 测试计划

新增 `tests/test_research_kernel.py`：

1. `build_run_manifest` 字段齐全、`run_id` 前缀为 `RS-`、`params_sha256` 为 64 位十六进制。
2. `run_backtest`（复用现有合成 K 线条目）返回 `result.run["source"] == "backtest"`，`run_id` 等于传入值。
3. `result_summary` 与 `result.performance` 的 trades/net_pnl 相等。
4. `_fill_backtest_row` 行内 trades/net_pnl 等于 `result_summary`。
5. `record_run` 在临时 `trading.db` 落一行，重复写同一 `run_id` 不报错。

新增 `tests/test_observability.py`：

1. `trace`：`get_trace_id()` 初始为空；`ensure_trace_id()` 生成 `OT-`；`bind_trace` 退出后恢复。
2. `record_signal` + `record_audit("order_submitted")` + `record_audit("order_filled")` 后，`trace_events(trace_id)` 返回事件顺序正确。
3. `record_audit("risk_decision")` 与 signals 同 trace 时，`trace_events` 含 4 类事件（signal/risk/order/fill）。
4. 幂等建表：同一临时库调用 `trading_db.connect()` 两次成功，`sqlite_master` 含 `signals/audit_log/research_runs`。
5. `setup_logger(tmp_path)` 后写一条带 `set_trace_id` 的日志，JSONL 行含 `record.extra.trace_id`；测试结束恢复 `setup_logger()`。
6. `metrics_snapshot` 在空目录返回零值；写入一条 `order_sent` 时间线后 `orders_sent == 1`。

补充/调整现有测试：

- `src/screen/picker.py` 若因 `source` 参数影响自定义 runner，更新对应测试替身签名（`**kwargs`）。
- 任何断言 `BacktestResult.to_dict()` 精确键集合的测试，补充 `"run"`。

---

## 5. 不做的事

- 不把 `data/order_timeline.jsonl`、`data/events.jsonl`、`alerts.jsonl` 合并进 SQLite。
- 不改变实盘报单顺序、风控闸门顺序、线程模型。
- 不做新的事件总线、不引入 Kafka/Redis。
- 不改优化器算法、不改交叉测试评分规则。
- 不对每个优化试参写 `research_runs`。
- 不新增告警规则；沿用 `src/ops/alerts.py`。

---

## 6. 验证命令

```powershell
python -m pytest tests/test_research_kernel.py tests/test_observability.py -q
python -m pytest -q
python -m ruff check src tests
python -m mypy
python -m pytest --cov=src --cov-report=term-missing -q   # 覆盖率不低于 56
```

若仓库使用 pre-commit，则以其钩子为准。

---

## 7. 文档更新

`docs/系统架构与演进方案.md`：

- P4 条目标记「已完成」，补充落地文件：
  `src/research/kernel.py`、`src/ops/journal.py`、`src/kernel/trace.py`。
- 2.11 的日志/审计/指标段落改为「已实现」，与本节字段一致。
