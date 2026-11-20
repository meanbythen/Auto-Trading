# R11 第二阶段（续）任务清单

> 依据：[spec.md](spec.md)。一次一项，完成即勾。全部完成后统一跑门禁与全量测试；提交与推送等用户指示。

- [x] T1 进度合并刷新
  - `src/ui/config_vars.py`：新增 `_pick_progress_pending`、`_pick_progress_after_id`。
  - `src/ui/picker_tab.py`：`_note_pick_progress` / `_flush_pick_progress` / `_reset_pick_progress`；`_show_pick_progress` 只重画；开跑、完成、失败处复位。

- [x] T2 事件总线
  - `src/kernel/events.py`：新增 `BACKTEST_DONE`、`CROSS_TEST_DONE`。
  - `src/ui/app.py`：构造 `self.events`、订阅、两个处理函数（经 `_post_to_main` 写日志）。
  - `src/ui/backtest_actions.py`、`src/ui/picker_tab.py`：完成处改发事件。

- [x] T3 K 线窗口
  - 新增 `src/ui/kline_chart.py`：Toplevel + FigureCanvasTkAgg（不用 pyplot），后台取数、主线程画图，关闭销毁画布。

- [x] T4 打开入口
  - `src/screen/picker.py::list_rows`：每行补 `code`、`timeframe`。
  - `src/ui/picker_tab.py`：行双击绑事件、`_open_pick_chart`、结果表下方提示。

- [x] T5 测试
  - `tests/test_symbol_picker.py`：`list_rows` 的 `code`/`timeframe` 断言（基准行 + 交叉行）。

- [x] T6 版本与日志
  - `VERSION` → 49.10.0；`CHANGELOG.md` 顶部新增 49.10.0 段。

- [x] T7 验收
  - `python -m pytest -q -p no:cacheprovider`
  - `python -m ruff check <改动文件>`
  - `python -m mypy <改动模块>`
  - 导入冒烟 `python -c "import src.ui.kline_chart, src.ui.app"`
  - 汇报改动文件与结果，等待用户决定是否提交。
