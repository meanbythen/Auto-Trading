# R11 第三阶段任务清单

> 依据：[spec.md](spec.md)。一次一项，完成即勾。全部完成后统一跑门禁与全量测试；提交与推送等用户指示。

- [x] T1 结果表分批渲染
  - `src/screen/picker.py`：删 `window_rows`/`WINDOW_ROW_LIMIT`，新增 `BOARD_PAGE`、`board_page`。
  - `src/ui/config_vars.py`：`_pick_board_rows`、`_pick_board_shown`、`_pick_board_more`。
  - `src/ui/picker_tab.py`：`_render_pick_board` 重置并调用新 `_append_pick_rows`；追加页与「显示更多」按钮。

- [x] T2 共享 K 线核实
  - 结论已写入 spec 2.1：分块键唯一、进程内已有解析缓存，不改代码。

- [x] T3 K 线窗口补全
  - `src/ui/kline_chart.py`：`moving_average`、`MA_WINDOWS`、窗口注册表复用、周期下拉、均线开关、取数代次号。

- [x] T4 事件总线扩面
  - `src/kernel/events.py`：`CROSS_TEST_START/PROGRESS/STOPPED/FAILED`。
  - `src/ui/app.py`：订阅 + 四个处理函数（经 `_post_to_main`）。
  - `src/ui/picker_tab.py`：开始、进度、停止、失败改发事件，去掉重复日志。

- [x] T5 每组合配置复用
  - `src/screen/picker.py`：`_WORKER_PREPARED`、`_prepared_cross_cfg`；`_cross_worker_run` 用缓存；`_run_cross_backtest` 支持 `prepared`。

- [x] T6 冷启动测量
  - 基线：`import src.ui.app` 约 1.0 秒，主要是 pandas、customtkinter、tkinter；没有低风险且收益明显的可推迟项，未改启动路径。

- [x] T7 测试
  - `tests/test_symbol_picker.py`：`board_page` 断言（首页 100、追加、说明、不足一页）；`_prepared_cross_cfg` 同键复用、异键不共用。
  - `tests/test_kline_chart.py`：`moving_average` 纯函数（含前 window-1 个 None、窗口为 0）。

- [x] T8 版本与日志
  - `VERSION` → 49.11.0；`CHANGELOG.md` 顶部新增 49.11.0 段。

- [x] T9 验收
  - `python -m pytest -q -p no:cacheprovider`：全部通过。
  - `python -m ruff check <改动文件>`：通过。
  - `python -m mypy <改动模块>`：通过。
  - 导入冒烟通过。
  - 已汇报改动文件与结果，等待用户决定是否提交。
