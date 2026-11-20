# P3 任务清单

来源：[spec.md](spec.md)。按顺序执行；第 1 组与第 2 组可独立回退。

## A. 策略插件

- [x] A1 新增 `src/strategy/plugins/__init__.py`
  - `StrategySpec`、`discover_specs()`、`param_schema()`、`validate_params()`、`strategy_identity()`。
  - 内置来源：`REGISTRY`（正式名）+ `ENGINE_LABELS` / `ENGINE_SHORT_LABELS` / `ENGINE_AUDIT_MODES` / `ENGINE_SECTION_DEFAULTS`。
  - 插件目录默认 `src/strategy/plugins/`；单文件加载；异常只记 warning，永不抛出。
  - 尊重 `strategy.plugins_enabled`（默认 true）。
- [x] A2 新增目录 `src/strategy/plugins/`（空目录占位即可，不放示例策略）。
- [x] A3 改 `src/strategy/factory.py`
  - `create_strategy()`：`REGISTRY` 未命中时用 `discover_specs()` 的插件 factory；仍失败则按现有逻辑回退 `DEFAULT_ENGINE`。
  - `normalize_engine()`：插件 code 合法。
  - `sanitize_strategy_config()`：插件 code 不被重置成默认引擎。
  - 新增导出 `strategy_identity(cfg)`。
- [x] A4 改 `src/ui/app_common.py`
  - `ENGINE_CHOICES` 由 `discover_specs()` 生成（`multi_factor` 优先、内置注册表顺序、插件追加）。
  - `ENGINE_LABELS` 补插件中文名。
  - 发现失败时保留现有静态拼法。
- [x] A5 改 `src/backtest/engine.py`
  - `details` 增加 `"strategy": strategy_identity(cfg)`。

## B. 数据目录

- [x] B1 在 `src/account/schema.sql` 追加 `instrument_snapshots`、`instruments`、`data_catalog` 三张表（CREATE IF NOT EXISTS）。
- [x] B2 新增 `src/data/catalog.py`
  - `refresh_instruments()`、`latest_instrument_snapshot()`、`list_instruments()`。
  - `refresh_kline_catalog()`、`refresh_tick_catalog()`、`refresh_all()`。
  - `catalog_reference()`、`main()` CLI。
  - 所有入口先查 `storage_settings()`；SQLite 关闭/读表失败返回空值，只记 warning。
- [x] B3 改 `src/backtest/engine.py`
  - `details` 增加 `"data_catalog": catalog_reference(cfg)`（与 A5 同一处编辑）。
- [x] B4 改 `src/main.py`
  - `load_config()` 后调用 `refresh_instruments(root, cfg)`，包 try/except，失败不影响启动。

## C. 测试与验证

- [x] C1 新增 `tests/test_strategy_plugins.py`（发现、坏插件回退、工厂创建、sanitize、identity）。
- [x] C2 新增 `tests/test_data_catalog.py`（SQLite 关闭、快照幂等、K 线目录 upsert、reference）。
- [x] C3 `python -m pytest tests -q`
- [x] C4 `ruff check src tests`
- [x] C5 `mypy src`
- [x] C6 人工核对：配置页/回测页/优化页下拉仍能看到全部内置策略；新增临时插件后重启界面能看到。

## D. 文档

- [x] D1 更新 `docs/系统架构与演进方案.md` §3 P3 为已完成，写明回退方式与新增命令。
