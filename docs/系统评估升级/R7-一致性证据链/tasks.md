# R7 一致性证据链 — 任务清单

对应规格：[spec.md](spec.md)

## 代码

- [x] T1 P1-1 `src/review/consistency.py`：镜像命中按复盘窗口过滤，结论不再跨窗口相减
- [x] T2 P1-2 `src/ops/health.py`：实盘样本按品种过滤；缺口径不累计 `watch_days`；支持 `cfg/account`
- [x] T3 P1-3 `src/review/release_sheet.py`：证据摘要哈希、签名绑定、缺证据禁签「允许小资金挂机」
- [x] T4 P1-3 `src/ui/review_tab.py`：签评审单处捕获 `ValueError` 并提示
- [x] T5 P1-4 `src/account/slippage_log.py`：旧表头迁移 + 读取归一
- [x] T6 P1-5 `src/backtest/slippage.py`、`src/review/release_sheet.py`：改读根级 `evaluation`
- [x] T7 P1-6 `src/backtest/engine.py`、`src/review/release_sheet.py`：快照补 `bars` 并加回退
- [x] T8 P1-7 `src/review/release_sheet.py`、`src/ops/health.py`：补数据源/滑点/成交方式/手数/换月链口径
- [x] T9 P1-7 `src/scheduler/runner.py`、`src/review/live_eval.py`：健康检查传 `cfg`
- [x] T10 P1-8 `src/ui/dashboard.py`：首页健康卡传引擎/品种/周期

## 测试

- [x] T11 更新 `tests/test_consistency.py`、`tests/test_health.py`
- [x] T12 更新 `tests/test_release_sheet.py`、`tests/test_slippage_log.py`、`tests/test_backtest_slippage.py`
- [x] T13 全量 `pytest` 0 失败；改动文件 `ruff`；改动模块 `mypy`

## 发版

- [x] T14 `VERSION` / `CHANGELOG.md` 升 49.14.0
- [x] T15 验收报告改名 49.14.0，勾选 P1，R7 记 8/8，同步两处引用
- [x] T16 汇报结果（不自动提交）
