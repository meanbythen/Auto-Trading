# R11 第三阶段 — 实施规格

状态：已批准并实现（49.11.0）
前置：49.10.0（R11 第二阶段已完成并推送）
范围：交叉测试结果表分批渲染、K 线窗口补全、事件总线扩面、每组合配置复用、冷启动测量。
不改策略、回测口径、风控规则；不新增第三方依赖。

---

## 1. 目标与验收

1. **结果表分批渲染**：交叉测试结果首屏只建前 100 行，底部「显示更多」每次追加 100 行；新一轮结果先清空旧的「显示更多」再从头画。
2. **K 线窗口补全**：窗口内可切周期、可叠加均线；同合约同周期再次双击时复用已开窗口（还原并置顶），不再开第二个。
3. **事件总线扩面**：交叉测试的开始、进度、停止、失败改由事件发布，界面在 `app.py` 订阅后切回 Tk 主线程更新；`BACKTEST_DONE` / `CROSS_TEST_DONE` 现有行为不变。
4. **每组合配置复用**：子进程内按（策略, 周期）缓存一次已准备的回测配置，不再每个组合都 `deepcopy` 整份配置。
5. **冷启动**：先量基线；只做低风险延迟导入，没有明显收益就不改。
6. 门禁：`pytest` 0 失败；改动文件 `ruff` 通过；改动模块 `mypy` 无新增错误；导入冒烟通过。
7. 版本：`VERSION` → 49.11.0，`CHANGELOG.md` 顶部新增 49.11.0 段。

---

## 2. 核实结论（决定不改的两处）

### 2.1 子进程共享已解析 K 线：不改

原方案假设「同一份 K 线会在多个 spawn 进程里重复读盘解析」。核实结果：

- `_cross_plan` 的分块键是（品种, 周期），`src/screen/picker.py#L1123-L1133`：每个（品种, 周期）只生成一个块，只会交给一个子进程。不同进程之间不存在同一 (symbol, period) 的重复解析。
- 块内多套策略共用同一份 K 线，走 `KlineProvider.fetch` 的进程内解析缓存（`src/data/kline.py#L258-L315`，`_fetch_cache_get/_put`），已经只解析一次。
- 唯一重复是父进程 `_prefetch_cross_bars` 下载时解析一次、子进程再解析一次，发生在不同进程、并行进行，对总耗时影响很小。
- 注意：默认开「主力换月」时，回测走 `run_backtest_rollover`，按主力合约表用真实月份合约（如 rb2701）分段，与预取的 `rb0` 不是同一个代码。要让预取数据真正被复用，需要改回测引擎和换月流程，收益不确定、风险不小。

结论：保持现有预取 + 进程内缓存，不引入临时文件数据通道。此条只记录结论，不改代码。

### 2.2 冷启动（T6）：先量后改

先量 `import src.ui.app` 的耗时和 `-X importtime` 前几名。只有出现「耗时明显、且只在函数体内用到」的模块才延迟导入；否则只记录基线，不改启动路径。

---

## 3. 设计

### 3.1 结果表分批渲染

`src/screen/picker.py`

- 删除 `WINDOW_ROW_LIMIT` 与 `window_rows`（只有 `picker_tab` 和一处测试在用），改为：
  ```python
  BOARD_PAGE = 100

  def board_page(rows, shown, page=BOARD_PAGE) -> tuple[list[dict[str, str]], int, str]:
      """结果表分批显示：从 shown 起取一页，返回（这一页、新的 shown、剩余说明）。"""
  ```
  - `page_items = items[shown:shown+page]`；`next_shown = min(len(items), shown+page)`。
  - 还有剩余时说明为「这一轮一共 N 行，已显示前 M 行，还有 K 行。较好的在前面，点「显示更多」继续；全部都会导出。」
  - 正好画完且总数超过一页时说明「这一轮一共 N 行，已全部显示。全部都会导出。」；不足一页返回空串。
  - 空列表返回 `([], 0, "")`。

`src/ui/config_vars.py`（`_init_vars`，紧随 `_pick_progress_after_id`）

- `self._pick_board_rows: list[dict[str, str]] = []`
- `self._pick_board_shown = 0`
- `self._pick_board_more: list[Any] = []`

`src/ui/picker_tab.py`

- `_render_pick_board`：清空 `pick_table_body` 子控件后，记下 `_pick_board_rows = list(rows)`、`_pick_board_shown = 0`、销毁 `_pick_board_more`，空表仍画提示，非空调用新方法 `_append_pick_rows()`。
- 新增 `_append_pick_rows()`：
  - 先销毁上一页留下的按钮/说明（`_pick_board_more`），清空列表；
  - `page, shown, note = board_page(self._pick_board_rows, self._pick_board_shown)`，逐行调 `_pack_pick_metric_line`（参数与现在一致，含 `on_open`）；
  - `_pick_board_shown = shown`；
  - 还有剩余：在 body 末尾放「显示更多（还有 K 行）」按钮，`command=self._append_pick_rows`，记入 `_pick_board_more`；
  - 有说明文字：放一个 `TEXT_DIM` 标签，记入 `_pick_board_more`。

### 3.2 K 线窗口补全（`src/ui/kline_chart.py`）

- 纯函数 `moving_average(values: Sequence[float], window: int) -> list[float | None]`：滑动求和；前 `window-1` 个点返回 `None`；`window <= 0` 全 `None`。
- 常量 `MA_WINDOWS = (20, 60)`，颜色取 `T.ACCENT` 和 `T.WARN`（避开红绿，免得和阴阳实体混在一起），图内图例用 ASCII `MA20` / `MA60`。
- 窗口注册表 `_WINDOWS: dict[tuple[str, str], _KlineChartWindow]`：
  - `open_kline_chart(master, cfg, symbol, period)`：已有且 `winfo_exists()` 为真时 `deiconify()/lift()/focus_force()` 并返回旧窗口；否则新建并登记。
  - `_on_close`：从注册表移除自己（只有登记的是自己才删）。
- 顶部工具条（状态栏上方）：
  - 周期下拉 `CTkOptionMenu`，选项取 `PERIOD_LABELS`（不含「自定义」），默认当前周期；
  - 「均线 MA20/MA60」开关 `CTkSwitch`，默认开。
- 周期切换 `_switch_period(code)`：
  - 新键已被别的窗口占用 → 置顶那个窗口并把下拉还原，不切换；
  - 否则登记表改键、改标题、清掉当前画布、状态置「正在取K线…」，`_load_id += 1` 后另起线程取数。
- `_load` 用代次号防串台：回调里 `gen != self._load_id` 直接丢弃，关窗/切周期后旧结果不覆盖新图。
- `_draw(bars)`：记住 `self._bars = bars`；先 `_clear_chart()` 再建 FigureCanvas；画 K 线、成交量，开关打开时按 `MA_WINDOWS` 叠均线。
- 均线开关切换：有 `self._bars` 时直接 `_draw(self._bars)` 重画，不重新取数。

### 3.3 事件总线扩面

`src/kernel/events.py`

- 新增 `CROSS_TEST_START = "cross_test_start"`、`CROSS_TEST_PROGRESS = "cross_test_progress"`、
  `CROSS_TEST_STOPPED = "cross_test_stopped"`、`CROSS_TEST_FAILED = "cross_test_failed"`，并加一行注释：「进度类事件带序号和文案，不带结果对象」。

`src/ui/app.py`（`__init__` 订阅，处理函数紧跟 `_on_cross_test_done`）

- 订阅四个新事件：
  - `_on_cross_test_start`：`_post_to_main(lambda: self._append_log(str(d.get("text") or "开始交叉测试")))`
  - `_on_cross_test_progress`：`_post_to_main(lambda: self._note_pick_progress(int(d["index"]), int(d["total"]), str(d["label"])))`
  - `_on_cross_test_stopped`：写一行日志（取 `text`）
  - `_on_cross_test_failed`：写一行「交叉测试失败: {message}」日志
- 订阅回调只做「切回主线程 + 写日志/进度」，不碰回测状态。

`src/ui/picker_tab.py`

- `_on_pick_start`：开跑日志改 `self.events.publish(CROSS_TEST_START, {"text": 那一长行})`；「最多用 N 个进程」仍走 `_post_to_main`。
- `on_progress`：改 `self.events.publish(CROSS_TEST_PROGRESS, {"index": i, "total": n, "label": name})`，不再自己 `_post_to_main`。
- `_on_pick_stop`：真正置停止时 `self.events.publish(CROSS_TEST_STOPPED, {"text": "将在当前组合测完后停止"})`；「现在没有在做交叉测试」仍直接写日志。
- 失败分支：`self.events.publish(CROSS_TEST_FAILED, {"message": err})`，界面渲染和弹窗保持原样（日志由订阅方写，避免重复）。
- `_done` 仍发 `CROSS_TEST_DONE`。

### 3.4 每组合配置复用（`src/screen/picker.py`）

- 模块级 `_WORKER_PREPARED: dict[tuple[str, str], tuple[dict[str, Any], int]] = {}`。
- `_cross_worker_init` 里清空它（每个子进程一份）。
- 新增 `_prepared_cross_cfg(cfg, spec, engine, period) -> tuple[dict[str, Any], int]`：按（engine, period）缓存 `prepare_screen_cfg` 的结果；键不存在才准备。
- `_cross_worker_run` 改用该函数，再调 `_run_cross_backtest(..., prepared=缓存)`。
- `_run_cross_backtest` 增加可选参数 `prepared: tuple[dict[str, Any], int] | None = None`；给了就直接用，不再调 `prepare_screen_cfg`。
- 安全性依据：`run_backtest` 入口自己 `_deepcopy_cfg`（`src/backtest/engine.py#L608`），`run_backtest_rollover` 只读配置并转调，缓存模板不会被回测改写。

### 3.5 冷启动测量

- 记录 `python -c "import time; t=time.perf_counter(); import src.ui.app; print(time.perf_counter()-t)"` 的基线。
- 如某个重模块只在函数体内使用且耗时明显，改为在函数内导入；否则不动。

### 3.6 版本

- `VERSION` → `49.11.0`；`CHANGELOG.md` 顶部新增 `## 49.11.0 — 2026-11-20`，按实际完成的条目录入，不写未实测的耗时数字。

---

## 4. 精确改动清单

| 文件 | 改动 |
|---|---|
| `src/screen/picker.py` | 删 `window_rows`/`WINDOW_ROW_LIMIT`；新增 `BOARD_PAGE`、`board_page`；新增 `_WORKER_PREPARED`、`_prepared_cross_cfg`；`_cross_worker_init`/`_cross_worker_run`/`_run_cross_backtest` 接缓存 |
| `src/ui/config_vars.py` | 新增 `_pick_board_rows`、`_pick_board_shown`、`_pick_board_more` |
| `src/ui/picker_tab.py` | `_render_pick_board` 分批；新增 `_append_pick_rows`；交叉测试开始/进度/停止/失败改发事件 |
| `src/ui/kline_chart.py` | `moving_average`、`MA_WINDOWS`、窗口注册表、周期下拉、均线开关、取数代次号 |
| `src/kernel/events.py` | 新增 4 个交叉测试事件常量 |
| `src/ui/app.py` | 订阅并处理 4 个新事件 |
| `tests/test_symbol_picker.py` | `window_rows` 测试改为 `board_page`；补 `_prepared_cross_cfg` 复用断言 |
| `tests/test_kline_chart.py`（若不存在则新建） | `moving_average` 纯函数测试 |
| `VERSION` / `CHANGELOG.md` | 49.11.0 |

---

## 5. 验证步骤

1. `python -m pytest -q -p no:cacheprovider` → 0 失败。
2. `python -m ruff check src/screen/picker.py src/ui/picker_tab.py src/ui/config_vars.py src/ui/kline_chart.py src/kernel/events.py src/ui/app.py tests/test_symbol_picker.py tests/test_kline_chart.py`
3. `python -m mypy src/screen/picker.py src/ui/picker_tab.py src/ui/kline_chart.py src/kernel/events.py src/ui/app.py`
4. 导入冒烟：`python -c "import src.ui.kline_chart, src.ui.app, src.screen.picker, src.kernel.events"`。
5. 冷启动基线记录（导入耗时，前后各一次）。
6. 人工（有桌面时）：跑一轮组合超过 100 个的交叉测试，首屏 100 行、「显示更多」每次加 100；双击两行同合约同周期的结果只出现一个窗口；窗口里切周期、开关均线；进度日志仍逐条出现且界面不卡。
7. `git status` 确认无临时文件；提交与推送等用户指示。

## 6. 风险与回滚

- 事件化后若某处仍有直接写日志，会出现重复行：实现时逐一核对上面 3.3 列出的调用点。
- 配置模板复用依赖「回测入口会深拷贝」；已核实 `engine.py#L608`。若以后有 runner 直接改传入配置，需要在 `_run_cross_backtest` 里退回深拷贝。
- 分批渲染只影响控件数量，数据仍全部在内存和导出文件里。
- 回滚：全部为代码改动，无配置/数据格式变更，按文件回退即可。
