# 第五批（P3/P4 收尾）任务清单

- 规格：[spec.md](./spec.md)
- 版本目标：`49.23.0`
- 状态：已完成

## 任务

- [x] R1 扩展 `src/research/kernel.py::result_summary`（三输入 + 展示字段）
- [x] R1 回测结果页 `src/ui/backtest_actions.py` 核心口径改走 `result_summary`
- [x] R2 `build_run_manifest` 增 `data_sha256`，主回测链路用 `fingerprint_bars`
- [x] R2 `schema.sql` + `src/ops/journal.py` 增 `research_runs.data_sha256` 与自动补列
- [x] R3 `src/main.py` 启动记录 `metrics_snapshot` 汇总
- [x] R3 新增 `tools/ops_report.py`（指标 + trace 时间线）
- [x] R4 `src/main.py` 启动改为 `refresh_all`
- [x] R5 `src/strategy/factory.py` 增 `validate_strategy_params`
- [x] R5 `src/ui/config_collect.py` 保存时统一校验并记 warning
- [x] 新增 `tests/test_review_7_5_consistency.py`
- [x] 全量验证：pytest / lint-imports / ruff / mypy / import ui.app
- [x] 更新报告 §0.5 与 §2、架构文档 §1.8/§0
- [x] 更新 CHANGELOG.md / README.md / VERSION

## 约束

- 不改 JSON 账本 / SQLite 投影权威关系。
- 不动开平仓规则。
- 不修改 `docs/说明文本改进.md`。
- 不 commit、不 push。
