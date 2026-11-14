# Stage4-4 BarCursor 验收清单（44.6.11）

## 代码

- [x] `src/backtest/bar_cursor.py` 存在，只依赖 pandas、`exit_policy`、`data.periods`、`utils.trading_day`，不反向 import engine。
- [x] `BarCursor.at()` 对同一 `i` 的 `signal` 行数、`now`、`px`、`next_open`、`trail`、`trend` 与原循环首段逐一相同（`test_at_matches_legacy_loop_expressions`）。
- [x] `engine.py` 循环体内 `cash`/`pos`/`trades`/`equity`/`limits`/`open_meta` 的读写位置未变。
- [x] 无 `copy()`、无新增 dataframe 全量拷贝。
- [x] diff 不涉及 `optimizer.py`、UI、`fill.py`、`exit_policy.py` 逻辑（`engine` 只保留 `_next_open_price` 旧名字导出）。

## 测试

- [x] `tests/test_bar_cursor.py` 覆盖：索引范围、最后一根无 `next_open`、`fill_next_open=False`、trail 无未来数据、trend 与旧函数一致、`px` 缺失返回 `None`。
- [x] `ruff check .` 通过。
- [x] `python -m mypy src` 184 文件无问题。
- [x] 全量 `pytest` 通过（100%）。

## 等价性

- [x] 逐根视图与旧主循环表达式一致（合成 K 线逐根对照）。
- [x] `run_backtest` 端到端冒烟与成交测试通过（下一根开盘成交、末根强平）。
- [ ] 真实行情区间（联网跑 `tools/run_backtest.py`）与 44.6.10 的成交笔数/最终权益对照 —— 待你在联网环境跑一次（本机无本地 K 线库）。

## 文档

- [x] `VERSION` = 44.6.11。
- [x] CHANGELOG 新增 44.6.11（纯重构说明）。
- [x] README、配置说明书版本同步。
- [x] 审阅报告阶段 4 第 4 条标记 Stage4-4 完成，并注明 4b 未做。
