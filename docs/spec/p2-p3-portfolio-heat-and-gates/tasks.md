# P2 / P3 实施任务清单

对应规格：[spec.md](spec.md)

## P2 组合风险热度与在途预留

1. [x] `config/config.example.yaml`
   - [x] `risk.portfolio` 删除 `max_gross_lots`，新增 `max_heat_pct`、`pending_ttl_sec` 与中文注释。
2. [x] `src/risk/portfolio.py`
   - [x] `PortfolioLimits` 改为 `max_heat_pct` / `pending_ttl_sec`。
   - [x] `PortfolioSnapshot` 增加 `PositionRow` 与 `heat_cash()`。
   - [x] `check()` 用热度替换总手数上限。
   - [x] `PortfolioRisk.snapshot()` 失败返回 `None`；开仓失败关闭。
   - [x] 增加在途预留、TTL 清理与 `release()`。
3. [x] `src/kernel/risk_chain.py`
   - [x] `TradeIntent` 增加 `entry_price`、`stop_price`。
4. [x] `src/scheduler/order_executor.py`
   - [x] `_trade_intent` 填入入场价与止损价。
5. [x] `src/risk/service.py`
   - [x] `evaluate_portfolio` 传入价格；异常时开仓 fail-closed、平仓放行。
   - [x] 开仓成交后释放预留。
6. [x] `src/backtest/portfolio.py`
   - [x] `simulate_shared_capital` 增加 `max_heat_pct` 累计热度判断与输出。
   - [x] `run_portfolio_backtest` 读取配置并传入；摘要增加热度峰值。

## P3 精简闸门

7. [x] `src/backtest/limits.py`
   - [x] `allow_open` 改为调用 `RiskGuardian.allow`。
   - [x] `on_open` / `on_close` 用 K 线时间同步 Guardian 时间戳。
8. [x] `src/backtest/engine.py`
   - [x] 开仓调用传入 `side`、`symbol`。
   - [x] 显式 `volume` 改为回测对照组手数，不再改写 `sizing_mode`。
9. [x] `src/risk/sizing.py`
   - [x] 删除 `fixed` 直返分支；固定手数只作为风险手数上限。
   - [x] 更新模块说明。
10. [x] ATR 硬暂停 / 健康硬停开
    - [x] 代码与阈值保持不变。
    - [x] TODO 写明仿真对照方法与决策条件。

## 测试与验证

11. [x] 更新 `tests/test_risk.py`（热度、fail-closed、在途预留）。
12. [x] 更新 `tests/test_backtest_risk.py`（固定手数上限、Guardian 同口径）。
13. [x] 更新 `tests/test_portfolio.py`（共享资金账热度）。
14. [x] 更新 `tests/test_batch2_p1_consistency.py`（`_read` 可空）。
15. [x] `ruff check src tests`、`mypy src`、`pytest -q`。

## 文档与版本

16. [x] `VERSION`、`CHANGELOG.md`、`README.md` 升级到 49.40.0。
17. [x] `docs/TODO.md`、`docs/多品种实盘方案与实施规划.md`、`docs/配置说明书.md`、`docs/实盘交易简易说明书.md`、`docs/系统架构与演进方案.md` 同步。
18. [x] 运行 `python tools/check_doc_versions.py` 直至一致。
19. [x] 不提交、不推送。
