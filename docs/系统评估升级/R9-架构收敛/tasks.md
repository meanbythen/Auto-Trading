# R9 任务清单

对应版本：49.16.0
规格：[spec.md](spec.md)

## 一、OrderExecutor 拆分

- [x] T1 新增 `src/scheduler/order_pricing.py`：搬入价格/保护跳/盘口/限价/滑点拦截，并从 `runner.py` 搬入 `entry_slip_blocked`
- [x] T2 新增 `src/scheduler/order_state.py`：搬入委托意图落盘、未核对委托熔断、订单时间线
- [x] T3 新增 `src/scheduler/fill_reconcile.py`：搬入成交去重、成交入账、补记与报单收尾
- [x] T4 `order_executor.py` 改为三个 mixin 组合，保留编排与人工命令入口；兼容导出 `ctp_opponent_limit` / `ctp_fill_key`
- [x] T5 `runner.py` 保留 `entry_slip_blocked` 再导出，现有测试不改导入

## 二、UI 命令层与只读快照

- [x] T6 `TradingBot` 新增 `apply_runtime_config`、`positions_snapshot`、`note_force_close`、`orders_paused`
- [x] T7 `src/ui/config_support.py` 改调 `apply_runtime_config`，删除直写 `bot.cfg` / `bot.scan_interval` / 私有方法
- [x] T8 `src/ui/run_tab.py` 改用 `positions_snapshot` 与 `note_force_close`
- [x] T9 `src/ui/bot_worker.py` 转发命令，读公开 `orders_paused`

## 三、依赖方向

- [x] T10 `requirements-dev.txt` 增加 import-linter；`pyproject.toml` 增加分层契约（三条 `forbidden`）
- [x] T11 拆 `scheduler` / `account` / `review` / `backtest` 四组循环
- [x] T12 拆 `login` / `ops` 两组循环
- [x] T13 大环拆两处低风险边：`session_bar_times` 下沉、`param_history` 改用 `strategy.factory` 标签
- [x] T14 新增 `tests/test_architecture.py`：分层反向导入、核心循环豁免清单、UI 禁写扫描

## 四、位置状态

- [x] T15 三处内联重建风控净仓统一走 `_rebuild_risk_positions()`
- [x] T16 新增 `tests/test_runtime_config.py`：命令层更新与快照只读

## 五、验证与发版

- [x] T17 全量 `pytest`、改动文件 `ruff`、改动模块 `mypy`、`lint-imports`
- [x] T18 文档冲刷：README、配置说明书、综合升级规划状态
- [x] T19 `VERSION` 49.16.0、`CHANGELOG.md` 新增 49.16.0 段
- [x] T20 验收报告 `git mv` 到 49.16.0，第八节 R9 记 4/4，同步 README 与 `R11-第二阶段/spec.md` 引用
- [x] T21 汇报结果（不自动提交）
