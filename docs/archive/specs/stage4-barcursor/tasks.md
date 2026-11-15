# Stage4-4 BarCursor 任务清单（44.6.11）

- [x] T1 新增 `src/backtest/bar_cursor.py`：`BarView` + `BarCursor`（`indices` / `at` / `__len__`），复用现有纯函数，不反向 import engine。
- [x] T2 改 `engine.py` 主循环：创建 `cursor`，循环首段改用 `view.*`，删除原 `next_open` 计算行；其余循环体变量名与顺序不动。
- [x] T3 新增 `tests/test_bar_cursor.py`：索引范围、末尾 `next_open`、关闭下一根成交、trail 对齐、trend 与旧函数一致、收盘价缺失返回 `None`、逐根对照旧表达式。
- [x] T4 跑 `ruff check .`、`python -m mypy src`、全量 `python -m pytest -q -p no:warnings`。
- [x] T5 等价性核对：本轮以「逐根对照旧表达式」用例 + 全量回测冒烟/成交测试替代实盘区间人工比对（本机无本地 K 线库，未跑真实行情区间）。
- [x] T6 版本 44.6.11；CHANGELOG、README、配置说明书、`docs/项目审阅报告.md` 阶段 4 第 4 条同步。
- [x] T7 检查 diff：不含 `optimizer.py`、UI、`fill.py`、`exit_policy.py` 行为改动。

## 备注

- 等价性核对记录：`test_at_matches_legacy_loop_expressions` 对 11 根合成 K 逐根比对 `signal`/`now`/`px`/`trail`/`trend`/`next_open`，全部一致；`test_backtest_fill.py`、`test_backtest_smoke.py` 实际跑通 `run_backtest`（含下一根开盘成交、末根强平）。
- 真实行情区间（沥青/玉米）的人工比对建议你在联网环境跑一次 `python tools/run_backtest.py --symbol bu2612`，对照 44.6.10 的成交笔数与最终权益。
- Stage4-4b（`while cursor.advance()` + 循环体再拆）不在本轮，需要时另写规格。
