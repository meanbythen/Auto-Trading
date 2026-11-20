# R11 第二阶段（续）— 实施规格

状态：已批准并实现（49.10.0）
上位文档：[阶段性验收报告-49.12.0.md](../阶段性验收报告-49.12.0.md) 第六节 R11
范围：只做第二阶段剩下的三项（进度合并刷新、事件总线接入、K 线图表窗口试点）和版本更新。
不动第一阶段已完成的「K 线解析缓存」「任务分块」，不改策略、回测、风控口径。

---

## 1. 目标与验收

1. **进度合并刷新**：交叉测试运行中，结果区最多每 100 毫秒重画一次（约 10Hz），不再每跑完一套策略就清空重建一次控件；跑完/失败/停止后不再有滞后的进度覆盖结果表。
2. **事件总线接入**：`AutoTradingApp` 持有唯一的 `EventBus` 实例；交叉测试完成、历史回测完成由发布方发事件，订阅方切回 Tk 主线程写日志。`src` 内首次出现 `EventBus(` 实例（此前只有定义与测试）。
3. **K 线图表窗口试点**：双击交叉测试结果的一行，弹出该合约、该周期的 K 线（含成交量）；关闭窗口销毁画布，可反复打开关闭不报错。不新增第三方依赖（matplotlib 已在 `requirements.txt`）。
4. 门禁：`python -m pytest -q -p no:cacheprovider` 0 失败；改动文件 `ruff` 通过；改动模块 `mypy` 无新增错误。
5. 版本：`VERSION` 由 49.9.0 升到 49.10.0，`CHANGELOG.md` 顶部新增 49.10.0 段（与仓库既有习惯一致；版本号只读 `VERSION` 一处）。

---

## 2. 设计

### 2.1 进度合并刷新（`src/ui/picker_tab.py`）

现状：`screen_cross_parallel` 每跑完一套策略回调一次 `on_progress`；`_on_pick_start` 里每条都 `_post_to_main` 到 `_show_pick_progress`，后者写日志并 `_render_pick_board`（销毁并重建行控件）。

改动：

- `src/ui/config_vars.py::_init_vars`，在 `self._pick_running` 后新增两个状态：
  - `self._pick_progress_pending: tuple[int, int, str] | None = None`
  - `self._pick_progress_after_id: str | None = None`
- 新增 `PickerTabMixin._note_pick_progress(index, total, label)`（主线程执行）：
  - `_pick_running` 为假时直接返回；
  - 照旧 `self._append_log(f"交叉测试 {index}/{total} {label}")`（日志保留逐策略一条，写入是队列操作，代价低）；
  - 把最新的 `(index, total, label)` 存进 `_pick_progress_pending`；
  - 若 `_pick_progress_after_id is None`，`self.after(100, self._flush_pick_progress)` 并记下 id。
- 新增 `PickerTabMixin._flush_pick_progress()`：清空 after id，取出并清空 pending，有值才调 `_show_pick_progress(*pending)`。
- `_show_pick_progress` 去掉 `_append_log`（日志移到 `_note_pick_progress`），保留 `_pick_running` 守卫，只负责重画进度文案。
- 新增 `PickerTabMixin._reset_pick_progress()`：`after_cancel` 挂起的回调（忽略 TclError），两个字段复位。
- `_on_pick_start`：开跑前、`_done`、`_fail` 各调一次 `_reset_pick_progress()`；`on_progress` 改调 `_note_pick_progress`（仍在 `_post_to_main` 里）。

效果：控件重画最多 10 次/秒；完成事件先复位再渲染结果，不会出现「结果表刚出来又被进度盖掉」。

### 2.2 事件总线接入

- `src/kernel/events.py`：在现有三个事件名后新增
  - `BACKTEST_DONE = "backtest_done"`
  - `CROSS_TEST_DONE = "cross_test_done"`
  - 同步补一行注释说明「完成类事件只带汇总数字，不带结果对象」。
- `src/ui/app.py::AutoTradingApp.__init__`：
  - `from src.kernel.events import BACKTEST_DONE, CROSS_TEST_DONE, EventBus`；
  - 在 `self.worker = BotWorker()` 附近新增 `self.events = EventBus()`；
  - 订阅：`self.events.subscribe(BACKTEST_DONE, self._on_backtest_done)`、`self.events.subscribe(CROSS_TEST_DONE, self._on_cross_test_done)`。
- `src/ui/app.py` 新增两个订阅处理函数（放在 `_on_main_tab_change` 之后）：
  - `_on_backtest_done(event)`：从 `event.data` 取 `header / symbol / bars / trades / win_rate / payoff / expectancy / net_pnl`，拼出与现在相同语义的一行摘要，`self._post_to_main(lambda: self._append_log(text))`。
  - `_on_cross_test_done(event)`：取 `rows / better`，拼「交叉测试完成，测了 N 个组合，较好 M 个」，同样 `_post_to_main`。
- `src/ui/backtest_actions.py::_start_backtest._run`：把现有 `self._post_to_main(lambda: self._append_log(f"{header}完成 ..."))` 换成
  `self.events.publish(BACKTEST_DONE, {...汇总字段...})`；其余（写报告、填结果框、异常分支）不动。
- `src/ui/picker_tab.py::_on_pick_start._done`：把 `self._append_log(f"交叉测试完成，测了 …")` 换成 `self.events.publish(CROSS_TEST_DONE, {"rows": n, "better": m})`。
- 异常/失败路径不发完成事件，保持现有直接写日志与弹窗。

### 2.3 K 线图表窗口试点

新增 `src/ui/kline_chart.py`（新文件是必要的：UI 组合根 `app.py` 不承载画图逻辑）：

- `open_kline_chart(master, cfg, symbol, period)`：创建 `_KlineChartWindow` 并返回；不做单例限制，允许同时开多个。
- `_KlineChartWindow(ctk.CTkToplevel)`：
  - 标题 `K线 {symbol} {period_label(period)}`，980×620，深色主题（`T.BG`）。
  - 顶部状态标签：`正在取K线…` → 成功后显示「N 根（起 ~ 止）」或失败原因。
  - 后台线程 `KlineProvider(cfg).fetch(symbol, period, max_bars=600, keep_history=True)`；不得在后台线程碰 Tk 控件，统一 `self.after(0, …)` 回主线程并吞 `TclError`（窗口已关）。
  - 画图：`matplotlib.figure.Figure` + `matplotlib.backends.backend_tkagg.FigureCanvasTkAgg`，**不 import pyplot、不改全局 backend**，避免覆盖 `factor_decay_plot` 的 Agg 用法。
  - 两个 axes：上 价格（影线 `vlines` + 实体 `bar`，红涨绿跌），下 成交量（同色）。深色背景、网格用 `T.BORDER` 低透明度；x 轴取约 8 个刻度，标签 `%m-%d %H:%M`；标题与刻度文案保持 ASCII（品种代码 + 周期代码 + 价格），避免中文字体缺字。
  - 关闭：`WM_DELETE_WINDOW` → 置 `_closed`、销毁 canvas widget、`figure.clf()`、`self.destroy()`。
- 取数用 `dict(self.cfg)` 并显式 `cfg["_project_root"] = str(self.root_dir)`，保证 `KlineProvider` 找到 `data/klines`。

入口（先只做交叉测试结果行，回测页暂不加）：

- `src/screen/picker.py::list_rows`：每行补两个纯文本字段
  - `code`：原始合约代码（基准行取 `base["symbol"]`，交叉行取 `row["symbol"]`）；
  - `timeframe`：周期代码（基准行取 `settings.timeframe`，交叉行取 `row["timeframe"]`，缺失回退 `period`）。
  只加字段，不改现有列与排序。
- `src/ui/picker_tab.py::_pack_pick_metric_line`：新增关键字参数 `on_open: Callable[[], None] | None = None`；非表头且传入时，给整行 frame 和每个 label 绑 `<Double-Button-1>`。
- `src/ui/picker_tab.py::_render_pick_board`：按行传 `on_open=lambda r=item: self._open_pick_chart(r)`。
- 新增 `PickerTabMixin._open_pick_chart(row)`：`code`/`timeframe` 任一为空时写日志说明；否则调用 `open_kline_chart`。
- 结果表下方新增固定提示：「双击一行，看这个合约、这个周期的K线。」（`right` 第 3 行，不占滚动区）。

### 2.4 版本

- `VERSION` → `49.10.0`；`CHANGELOG.md` 顶部新增 `## 49.10.0 — 2026-11-20`，写明：进度合并刷新、事件总线接入、K 线窗口试点，各一条；不写未实测的耗时数字。

---

## 3. 精确改动清单

| 文件 | 改动 |
|---|---|
| `src/kernel/events.py` | 新增 `BACKTEST_DONE`、`CROSS_TEST_DONE` 常量与注释 |
| `src/ui/app.py` | 构造 `self.events`；订阅两个完成事件；新增两个处理函数 |
| `src/ui/backtest_actions.py` | 完成摘要改发 `BACKTEST_DONE` |
| `src/ui/picker_tab.py` | 进度合并（`_note/_flush/_reset_pick_progress`）；完成改发 `CROSS_TEST_DONE`；行双击入口；结果表提示行 |
| `src/ui/config_vars.py` | 新增两个进度状态字段 |
| `src/screen/picker.py` | `list_rows` 每行补 `code`、`timeframe` |
| `src/ui/kline_chart.py` | 新文件：K 线窗口与画图 |
| `tests/test_symbol_picker.py` | 断言 `list_rows` 输出含 `code`/`timeframe`（基准行与交叉行各一例） |
| `VERSION` / `CHANGELOG.md` | 49.10.0 |

---

## 4. 验证步骤

1. `python -m pytest -q -p no:cacheprovider` → 0 失败。
2. `python -m ruff check src/ui/app.py src/ui/picker_tab.py src/ui/backtest_actions.py src/ui/config_vars.py src/screen/picker.py src/kernel/events.py src/ui/kline_chart.py tests/test_symbol_picker.py` → 无告警。
3. `python -m mypy src/ui/kline_chart.py src/ui/picker_tab.py src/ui/app.py src/ui/backtest_actions.py src/kernel/events.py` → 无新增错误（以仓库现有配置为准）。
4. 导入冒烟：`python -c "import src.ui.kline_chart, src.ui.app, src.kernel.events"` 不报错。
5. 人工（有桌面时）：跑一轮小交叉测试，确认进度大约每 100ms 刷新一次、完成后结果表不被进度覆盖；双击一行出 K 线；关窗、再开、再关无异常；完成日志仍各有一条。
6. `git status` 确认无临时文件；提交信息按 `49.10.0：一句话`，提交与推送等用户另行指示。

## 5. 风险与回滚

- 风险：matplotlib 在 Tk 主线程首次渲染约百毫秒级。缓解：取数放后台、最多 600 根、只画收盘价序列。
- 风险：事件订阅写在 `__init__`，若测试用 mixin 假 self 会缺 `events`。已排查：现有测试只 monkeypatch `_start_backtest`，无此用法。
- 风险：`list_rows` 多两个字段可能影响导出/断言。缓解：纯新增键，导出列由表头固定，不遍历所有键。
- 回滚：无配置/数据格式变更，回退代码即可；K 线窗口为独立新文件，删除不影响其他页。

## 6. 待批准（两项自行决定）

1. 版本升到 **49.10.0** 并写 CHANGELOG（而不是继续挂在已发布的 49.9.0 下）。
2. K 线入口本期只做「交叉测试结果行双击」，历史回测页暂不加按钮。
