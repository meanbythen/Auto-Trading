# 期权交易支持：任务清单

> 关联文档：`docs/期权交易/spec.md`
> 原则：风控始终是开仓最高优先级；缺数据一律拒开并告警；不动主循环、执行抽象、对账、告警。

## 阶段 A：评估报告

- [x] A1 把 `spec.md` 的结论、逐层证据、改动清单、缺口、用例路径、验证方案整理进
      `docs/期权交易/0.可行性评估.md`，逐条回答原有 6 个问题。
- [x] A2 在评估文档中明确「B 档低侵入可行、无需大改架构」的判定，以及三项硬性前置
      （符号解析、期权保证金口径、到期生命周期）。

## 阶段 B：符号与元数据

- [x] B1 新增 `src/domain/options.py`：
      `parse_option_symbol / is_option / option_product / option_kind / strike / expiry`，
      覆盖 `rb2601C3500`、`m2601-C-3000`、`SR605C6000`、`IO2512-C-4000`。
- [x] B2 新增 `src/domain/option_contract.py`：期权合约元数据与 `lot_premium / lot_margin`。
- [x] B3 改 `src/data/tq_source.py` `tq_symbol()`，支持期权交易所前缀；
      品种归属表改由 `src/domain/exchanges.py` 统一提供。
- [x] B4 改 `src/data/quote_check.py` `product_code()`、`src/risk/sizing.py` `_product_key()`，
      统一走 `options.option_product()`。
- [x] B5 改 `src/data/kline.py` `_to_sina_symbol()`，期权走新浪时明确报错。
- [x] B6 改 `src/domain/contracts.py` `build_contracts()`：透传 `price_tick` 及期权字段，
      支持完整合约键覆盖品种键。
- [x] B7 改 `src/trader/ctp_symbols.py` 交易所映射，补 `IO/HO/MO` 等期权品种；
      `ctp_instrument_id()` 对期权保留交易所原生写法。
- [x] B8 改 `src/scheduler/order_pricing.py` `_price_tick_for()`，先按完整合约取 tick。
- [x] B9 改 `config/品种与合约.xlsx`、`src/data/catalog.py`、`src/ui/ctp_probe.py`：
      新增 合约类型/标的/到期日/行权价/方向 五列并支持落表（catalog 的 `payload_json` 已自动保存这些字段，
      无需改表结构）。

## 阶段 C：算仓与风控

- [x] C1 新增 `src/risk/option_margin.py`：买方权利金、卖方保证金、缺数据拒开原因。
- [x] C2 改 `src/risk/sizing.py` `margin_for()`：买方 = 权利金，卖方 = 期权保证金；
      保持 `calc_open()` 的取小结构不变。
- [x] C3 增加「期权权利金总预算%」（`risk.option_premium_budget_pct`，默认 20），
      避免买方口径下一次性买满。

## 阶段 D：策略与生命周期

- [x] D1 新增 `src/strategy/option_long.py`（末日期权买方），在 `factory.REGISTRY` 登记。
- [x] D2 新增 `src/strategy/option_hedge.py`（期货持仓黑天鹅保护），在 `factory.REGISTRY` 登记。
- [x] D3 新增 `src/account/option_lifecycle.py`：最后交易日/到期日检查、到期前强平、拒开提示。
- [x] D4 改 `src/scheduler/order_executor.py`：算仓前调用 `assert_tradable()`，
      并把开仓方向传给算仓层。
- [x] D5 改 `src/domain/trade.py` `TradeRecord`：追加可选期权字段。
- [x] D6 补 `src/strategy/profile.py` 规则卡、`docs/系统评估升级/策略档案.md`、
      `src/ui/config_tab_strategy.py` 参数面板。

## 阶段 E：验证与发布

- [x] E1 新增 `tests/test_option_symbols.py`：四种写法解析、`tq_symbol`、CTP 代码、新浪拒绝。
- [x] E2 新增 `tests/test_option_sizing.py`：买方=权利金、卖方保证金、缺数据拒开、权利金预算、期货口径不变。
- [x] E3 新增 `tests/test_option_lifecycle.py`：到期解析、最后交易日拒开、到期强平、策略注册与开仓。
- [x] E4 回归：`pytest tests -q` 全通过；`ruff check src tests`、`mypy src` 通过。
- [x] E5 本地仿真冒烟（无真实柜台账号）：算仓 20 手 → `SimTrader.buy_open` 写入模拟成交 →
      到期日强平原因正确。真实 CTP 仿真需账号，按 README「CTP 上线四步」人工执行。
- [x] E6 已按更新规则更新 `CHANGELOG.md`、`README.md`、`VERSION`（57.3.0）。

## 二期（本期不做）

- 卖方策略、组合保证金、Greeks、CTP 行权 `ReqExercise`、期权回测器。
