# Stage4-1 领域层抽取任务清单

## A. 新增 src/domain

- [x] `src/domain/__init__.py` 导出 `PositionState` / `TradeRecord`
- [x] `src/domain/position.py`：搬入 `PositionState`，加 `to_dict` / `from_dict`
- [x] `src/domain/trade.py`：搬入 `TradeRecord`，加 `to_dict`
- [x] `src/domain/contracts.py`：`DEFAULT_CONTRACTS` + `build_contracts` + `contract_multipliers`
- [x] `src/domain/accounts.py`：搬入账本文件布局（PROFILES 与路径函数）
- [x] `src/domain/ledger.py`：搬入账本读取与权益计算，新增 `load_active_book`

## B. 改接调用方

- [x] `src/strategy/types.py` 改为从 domain 导入 `PositionState`
- [x] `src/backtest/metrics.py` 改为从 domain 导入 `TradeRecord`
- [x] `src/backtest/models.py` 使用 `t.to_dict()`
- [x] `src/risk/sizing.py` 使用 `build_contracts`
- [x] `src/risk/dynamic.py` 改用 `domain.ledger`，删除 account 函数内 import
- [x] `src/account/workspace.py` 重导出 domain.accounts 的名字
- [x] `src/account/signal_book.py` 改用 domain.ledger / domain.contracts，删除搬走的定义
- [x] `src/utils/state_store.py` 使用 `to_dict` / `from_dict`

## C. 工程化

- [x] `pyproject.toml` mypy 严格列表加入 domain/account/risk/utils/scheduler
- [x] 修复新增严格模块的类型错误（必要时按现有模式精确 `disable_error_code`）
- [x] 新增 `tests/test_domain_ledger.py`（8 组失败路径/边界用例）
- [x] 验证正反顺序 import 无循环依赖

## D. 收尾

- [x] `python -m ruff check .`
- [x] `python -m mypy src`
- [x] `python -m pytest -q`
- [x] `VERSION` 44.6.9
- [x] `CHANGELOG.md` 新增 44.6.9
- [x] `README.md` 版本同步
- [x] `docs/系统蓝图.md` 增加 domain 层并同步版本
- [x] `docs/配置说明书.md` 版本同步（如有）
- [x] `docs/项目审阅报告.md` Stage4 第 1、5 条更新完成状态
- [x] 确认未改 `run_backtest` / `BarCursor`
