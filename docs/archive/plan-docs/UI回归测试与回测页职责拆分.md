# UI 自动化回归与回测页职责拆分计划

## 一、目标与范围

继续完成审阅项 6-1 的剩余安全工作，顺序固定为：

1. 先为真实 `AutoTradingApp` 窗口建立自动化回归保护。
2. 在回归保护下机械拆分 `backtest_tab.py`，不改变现有行为。
3. 重新评估 `ConfigModel`；本轮不强行抽取，只记录有代码依据的结论。
4. 完成全量门禁、真实 UI 冒烟及 44.9.2 文档同步。

本轮不增加用户功能，不调整默认值、控件创建顺序、Tk trace/after 回调顺序、配置写回顺序、后台任务流程或保存格式；不引入新的 Controller 上帝类；不提交或推送代码。

## 二、现状与约束

- `src/ui/app.py` 固定创建九个 Tab，并在初始化时注册 300ms、800ms、5000ms 等回调；现有 `_on_close()` 只记录并取消其中三个 ID，因此测试清理必须覆盖所有剩余 `after`。
- `BotWorker` 构造不会启动线程，可安全用于真实窗口构造测试；任何按钮动作测试仍须替换线程、行情、导出和弹窗，禁止真实交易、下载或文件选择交互。
- CI 的测试任务运行在 `windows-latest`，可以执行真实 Tk 测试。Windows 上创建窗口失败必须让测试失败，不能静默 skip；非 Windows 无显示环境时才允许明确 skip。
- `backtest_tab.py` 目前约 1447 行，混合页面构建、配置同步/运行准备和后台动作。所有方法通过 `self.xxx` 共享 app 状态，多个动作含捕获局部变量的嵌套闭包，必须随宿主方法整体搬移。
- `config_vars.py` 与 `config_collect.py` 包含有序 `.set()`、trace 触发、UI 回写、secrets 和旧键清理。仅把数据包进 `ConfigModel` 会形成空壳或改变副作用顺序，本轮没有可证明安全且有收益的抽取边界。

## 三、实施步骤

### 阶段 A：先建立真实窗口回归基线

#### 1. 新增 `tests/conftest.py`

建立函数级真实 app fixture，要求：

- 将 `src.ui.app.project_root` 与 `src.utils.config_loader.project_root` 一致地指向 `tmp_path`，让 `ensure_runtime_layout()`、配置、secrets、复盘及运行产物都落在临时目录。
- 保留 `config_loader.resource_root` 指向仓库资源，以便首次初始化复制现有示例配置、品种表、优化参数表和策略模板；不得复制或修改真实用户配置。
- 将 `src.ui.app.setup_logger` 替换为无操作，避免测试改动全局 Loguru handler；启动告警清理仍在临时 root 下执行。
- 创建 `AutoTradingApp()` 后立即 `withdraw()`，只使用 `update_idletasks()` 完成布局，不调用 `mainloop()`。
- 非 Windows 因显示环境导致 `tkinter.TclError` 时明确 skip；Windows 不捕获为 skip。
- teardown 先标记关闭，遍历 `after_info()` 并取消所有剩余回调，再执行现有关闭/销毁路径；同时覆盖未被 `_on_close()` 命名的 300ms 回调。
- fixture 不启动 `worker.start()`、`worker.run_once()` 或任何后台动作。

#### 2. 新增 `tests/test_ui_regression.py`

使用真实 app fixture，测试按职责分组，弹窗全部 monkeypatch，动作不得访问真实外部服务。

1. **完整页面构造**
   - 断言九个 Tab 的名称与顺序严格为：运行、配置、历史回测、数据管理、交叉测试、策略优化、复盘、账户分析、规律候选。
   - 断言回测结果框包含初始操作说明，主要回测变量和控件已建立。

2. **配置页动态面板切换**
   - 断言当前 13 个策略面板和 5 个通用止损面板均建立。
   - 逐项驱动现有变量和刷新回调，断言目标面板可见、其他面板隐藏，并验证切回后面板对象未重建、变量未丢失。

3. **回测页即时联动**
   - 验证普通数据源与同花顺图来源的提示切换。
   - 修改回测品种并触发既有 trace，断言保证金按品种表同步，无表值时回退到配置页兜底保证金。
   - 验证 `_sync_bt_to_opt()` 复制策略、品种、周期和日期；同花顺图不会成为优化页的实盘扫描来源，弹窗被替换。

4. **配置映射与重载 round trip**
   - 修改一组有代表性的策略、周期、风险和开关变量，经 `_apply_ui_to_cfg()`、临时目录保存/加载及 `_reload_values_only()` 后断言值一致。
   - 断言既有旧键清理不回归，包括 `risk.max_position` 及当前实现已删除的旧止损/行情键；不断言与本改动无关的保存预设或运行中机器人副作用。

5. **回测参数复制与运行准备**
   - `_copy_risk_to_backtest(full=True)`：断言策略、品种、周期、来源、风险数字和开关按原顺序语义复制，配置文件字节不变。
   - `_prepare_backtest_run()`：断言返回独立配置副本、标准化日期和选中品种；关闭“启用限制”时副本固定 1 手且 `apply_limits=False`，原 `app.cfg` 与磁盘不被改写。
   - 保留非法日期/空品种的早期返回与弹窗断言，确保不会启动后台任务。

6. **回测写回实盘配置**
   - 替换确认/通知弹窗，验证 `_write_backtest_to_config_page()` 写回策略、品种、周期、来源、风险和开关。
   - 分别覆盖普通来源与同花顺图来源：后者必须保留原实盘 `data.source`。
   - 明确断言账户权益、回测日期和回测专用“启用限制”不写回实盘配置。

7. **动作入口安全冒烟**
   - 用可控假线程/假执行函数覆盖至少一个后台动作入口，断言准备失败时不创建线程、成功路径只调度预设任务，并通过 `_post_to_main` 回传结果。
   - 不运行真实回测、主力表下载、Excel 导出、系统打开文件或交易动作。

先在未拆分的现有代码上运行这些测试并全部通过，作为后续机械搬移的行为基线。

### 阶段 B：机械拆分回测页

保留对外 `BacktestTabMixin` 名称及 `AutoTradingApp` 的继承方式，不修改调用方。仅新增两个职责 Mixin 并调整现有模块：

#### 1. `src/ui/backtest_tab.py`：页面构建与组合入口

- 保留 `_build_backtest_tab()`、`_refresh_bt_source_hint()`、`_pack_open_limits()`。
- `BacktestTabMixin` 继承下述配置与动作 Mixin，使原有 `self._xxx` 方法入口保持不变。
- 不改任何控件、默认值、pack/grid 顺序、trace 注册、按钮 command 或初始结果文本。

#### 2. 新增 `src/ui/backtest_config.py`：同步、写回与运行准备

整体搬移：

- `_resolve_date_range`、`_initial_start_date`、`_normalize_date`、`_sync_bt_to_opt`。
- `_copy_risk_to_backtest`、`_write_backtest_to_config_page`、`_write_limits_to_config_page`。
- `_apply_backtest_switches`、`_table_margin_for_symbol`、`_sync_bt_margin_from_table`。
- `_align_backtest_trading_params_with_live`、`_apply_backtest_limits`、`_prepare_backtest_run`。

保留方法装饰器、局部导入、赋值/写回顺序和异常处理；不重写算法，不顺手简化分支。

#### 3. 新增 `src/ui/backtest_actions.py`：后台动作与结果展示

整体搬移：

- `_format_limit_lines`、`_format_skipped`。
- `_on_backtest` 及其 `_run`、`_runner` 和全部回调闭包。
- `_main_table_path`、`_refresh_main_table_state`、`_on_open_main_table`、`_on_update_main_table` 及其 `_progress`/`_run`、`_finish_main_table_update`。
- `_on_apply_slippage_hint`、`_on_slippage_scan`、`_on_export_backtest`、`_on_plot_factor_decay`，嵌套闭包随宿主方法原样移动。

线程创建、daemon 属性、`_post_to_main`、日志、弹窗和报告路径保持原样。只按实际使用移动 imports，避免循环依赖；跨职责方法继续通过 app 的 `self` 调用，不新增状态持有者。

#### 4. 搬移一致性检查

以本次修改前已提交的 44.9.1 `HEAD:src/ui/backtest_tab.py` 为有效基线，用 AST 对所有搬移方法逐一比较函数体/装饰器（忽略行号和所属类），确保拆分没有夹带逻辑编辑。若有必须调整的 import 或类型标注，单独检查并说明原因。

### 阶段 C：ConfigModel 结论与文档

- 不新增 `ConfigModel`，不重构 `config_vars.py` / `config_collect.py` 的有序赋值和 trace 行为。
- 保留已完成的轻量构建入口与 Mixin 组合，不再增加按 Tab 的 Controller 层。
- 在 `docs/项目审阅报告.md` 更新 6-1：记录真实 UI 回归数量、回测拆分边界、验证结果，以及 ConfigModel 暂缓的具体原因；把旧的 Git 未提交状态描述改为本次实际状态。
- 同步 `VERSION`、`README.md`、`CHANGELOG.md` 至 44.9.2，并按实际内容更新 `docs/配置说明书.md` 与 `docs/系统蓝图.md` 的测试/模块说明。用户操作和默认配置没有变化，不虚构行为变更。

## 四、明确决策

- 采用“真实 Windows Tk 窗口 + 临时运行 root”的测试，而非再写一批只模拟 `self` 的假 UI 测试。
- 采用两个职责 Mixin 加一个薄组合/构建模块，不采用继续膨胀的单一 Mixin，也不采用新建 BacktestController 上帝类。
- 本轮 ConfigModel 结论为“不抽取”，这是基于副作用和收益评估的完成项，不是遗留一个未决实现选择。
- 测试只模拟系统边界和后台副作用，不 mock 被测的页面构建、变量同步或配置映射本身。
- 不处理审阅项 6-1 之外的代码问题，不做无关格式化，不修改 `.trae/` 本地配置。

## 五、验证与完成标准

### 分阶段验证

1. 阶段 A 后：运行新增 UI 测试，确认在未拆分代码上全部通过；确认仓库真实 `config/config.yaml`、secrets 和日志未被测试触碰。
2. 阶段 B 后：逐文件运行 Ruff、新增 UI 测试和 AST 方法体对比，再运行真实窗口构造/面板切换冒烟。
3. 最终全量门禁：
   - `python -m ruff check .`
   - `python -m mypy src`
   - `python -m pytest --cov=src --cov-report=term-missing`
4. 确认 Windows UI 用例没有 skip，既有测试无新增失败，覆盖率不低于项目门槛 56%。

### 完成标准

- 九个 Tab、13 个策略面板、5 个通用止损面板及关键配置/回测联动均有自动化回归。
- `BacktestTabMixin` 对外入口和所有用户行为不变，回测页按构建、配置、动作三个清晰职责落地。
- ConfigModel 有明确且有证据的暂缓结论。
- 全量 Ruff、mypy、pytest/coverage 和真实 UI 检查通过，版本与文档证据一致。
