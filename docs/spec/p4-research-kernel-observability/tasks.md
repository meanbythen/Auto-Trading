# P4 任务清单

规格：`docs/spec/p4-research-kernel-observability/spec.md`
原则：每批可独立运行测试；先内核与表结构，再接实盘信号，最后补文档。

## 批次 1：基础设施与表结构

- [x] T1.1 新增 `src/kernel/trace.py`
  - `new_trace_id / get_trace_id / set_trace_id / ensure_trace_id / bind_trace`
  - 仅标准库；`OT-` 前缀；contextvar 隔离。
- [x] T1.2 `src/trader/order_timeline.py` 改为从 `src.kernel.trace` 导入 `new_trace_id`，保留同名导出。
- [x] T1.3 `src/account/schema.sql` 新增 `research_runs`、`signals`、`audit_log` 及索引；`risk_decisions` 增加 `trace_id` 列。
- [x] T1.4 `src/account/sqlite_migrate.py` 的 `_ensure_columns` 为 `risk_decisions` 补 `trace_id`。
- [x] T1.5 `src/utils/logger.py` 增加 trace patcher 与 `structured_{time}.jsonl` sink。

验证：`python -m pytest tests/test_observability.py -q`（先建空用例跑导入），手工确认临时库建表。

## 批次 2：研究运行清单

- [x] T2.1 新增 `src/research/kernel.py`
  - `new_run_id`、`build_run_manifest`、`result_summary`、`record_run`。
  - 延迟导入策略身份/数据目录/配置哈希，失败降级。
- [x] T2.2 新增 `src/ops/journal.py`
  - `record_signal`、`record_audit`、`record_research_run`、`trace_events`、`metrics_snapshot`。
  - 写库失败返回 `False`，不抛异常。
- [x] T2.3 `src/backtest/models.py` 增加 `BacktestResult.run` 与 `to_dict()["run"]`。
- [x] T2.4 `src/backtest/engine.py::run_backtest` 增加 `source`、`run_id` 参数，构建并附加 manifest；`save_backtest_report` 写入 `run`。
- [x] T2.5 `src/screen/picker.py`
  - `_fill_backtest_row` 改用 `result_summary`，行增加 `run_id`。
  - `run_cross_test` 增加 `source`，结束时落一份聚合 `research_runs`。
- [x] T2.6 `src/backtest/optimizer.py` 三个最终结果调用传 `source="optimizer"`；调用方对 `best_result.run` 落库。
- [x] T2.7 `src/review/replay.py` 增加 `StrategyReplay.run`，构建并落库。
- [x] T2.8 `src/research/scan.py::scan_regularities` 增加 `cfg/period/record`，可选落库。

验证：`python -m pytest tests/test_research_kernel.py -q`；再跑 picker/optimizer/replay 相关既有测试。

## 批次 3：实盘 signals 与审计

- [x] T3.1 `src/scheduler/runner.py` 新增 `_journal_signal`，非 HOLD 决策写 `signals + signal_generated` 并设置 trace 上下文。
- [x] T3.2 `runner.py` 三个提前返回分支补 `signal_blocked` 审计（stale/fallback/slippage）。
- [x] T3.3 `src/scheduler/order_executor.py` 写 `order_submitted`、`order_accepted/order_rejected` 审计。
- [x] T3.4 `src/scheduler/fill_reconcile.py::_apply_fill_accounting` 写 `order_filled` 审计。
- [x] T3.5 `src/risk/service.py::record_decision` 写 `trace_id`；`src/risk/state_store.py::append_decision` 落该列。

验证：`python -m pytest tests/test_observability.py -q`，重点跑 signal→risk→order→fill 同 trace 用例。

## 批次 4：测试与收尾

- [x] T4.1 新增 `tests/test_research_kernel.py`（规格第 4 节 1-5 项）。
- [x] T4.2 新增 `tests/test_observability.py`（规格第 4 节 1-6 项）。
- [x] T4.3 修复因新增 `run` 字段或 `source` 参数受影响的既有测试。
- [x] T4.4 运行全量验证：
  - `python -m pytest -q`
  - `python -m ruff check src tests`
  - `python -m mypy`
  - 覆盖率不低于 56。
- [x] T4.5 更新 `docs/系统架构与演进方案.md`：P4 勾选完成，2.11 段落改为已落地。
- [x] T4.6 汇总改动与验证结果，等待用户明确指示后再提交。

## 风险与回退

| 风险 | 处理 |
|---|---|
| 新增 DB 写入拖慢扫描 | 只在非 HOLD 决策与报单/成交时写；单条短连接，异常吞掉 |
| 优化器试参写库过多 | 仅最终结果传 `source="optimizer"` 并落库 |
| 自定义 runner 不接受 `source` | 先跑 picker 相关测试；必要时测试替身改为 `**kwargs` |
| 日志重复初始化 | 测试结束恢复 `setup_logger()`；JSONL 与文本共用同一 loguru 实例 |
| contextvar 在 CTP 回调线程丢失 | 成交侧始终优先读 `decision.meta["trace_id"]` / 回报字段 |
