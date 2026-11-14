# Stage4-1 领域层抽取验收清单

- [x] 正反顺序导入均成功：`import src.account.signal_book, src.risk.dynamic` 与反序均无循环依赖
- [x] `python -m ruff check .` → All checks passed
- [x] `python -m mypy src` → Success: no issues found in 182 source files（`src.domain.*` 在严格列表内）
- [x] `python -m pytest -q` → 全绿（含新增 `tests/test_domain_ledger.py`）
- [x] 未改动 `run_backtest` / `BarCursor`（本轮未编辑 `src/backtest/engine.py`）
- [x] 版本 44.6.9；CHANGELOG、README、系统蓝图、配置说明书、审阅报告已同步
- [x] `docs/specs/stage4-domain/tasks.md` 全部勾选
