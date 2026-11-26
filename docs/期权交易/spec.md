# 期权交易可行性评估与最小改造方案（Spec）

> 目标：回答「不修改基座、只新增模块或少量改动各基础模块」能否支持期权交易。
> 结论先行：**架构不需要大改；但不改代码无法直接交易期权。**
> 最小方案 = 1 个统一合约解析模块 + 2 个期权策略 + 1 个期权保证金口径 + 1 个到期生命周期模块，
> 外加 8 处小改；主循环、执行抽象、对账、告警、回测引擎全部复用。

## 1. 结论与判定口径

判定分三档：

| 档位 | 含义 | 本次结论 |
| --- | --- | --- |
| A 零改动 | 配置里填期权代码即可交易 | 否 |
| B 低侵入 | 新增模块 + 各层少量改动，不动架构 | **是，推荐** |
| C 大改 | 必须重写数据/执行/账本主链路 | 否 |

B 档的硬性前置只有三件事：

1. **合约代码解析统一**：现有十几处按「去掉所有数字当品种」的写法会把 `rb2601C3500` 解析成 `rbc`，
   导致合约表、保证金、tick、交易所全部查不到。
2. **期权保证金/权利金口径**：`PositionSizer` 只认「价格 × 乘数 × 保证金比例%」的期货口径。
3. **到期/行权/被指派**：全代码没有到期日和行权处理，到期不处理会变成实值行权或作废。

其余能力（策略注册、多腿路由、下单、撤单、对账、告警、账本、调度）均可原样复用。

## 2. 现有基座逐层核对（证据）

### 2.1 数据层：可复用，但符号解析和备用源要改

- `KlineProvider.fetch()` / `storage_symbol()` 以「合约名」为键存本地库，
  天然能存任意合约（`src/data/kline.py`、`src/data/quote_check.py` L121-136）。
- **阻断点**：`src/data/tq_source.py` `tq_symbol()`（L102-121）把合约拆成「全部字母 = 品种、全部数字 = 月份」，
  `rb2601C3500` → 品种 `rbc`、数字 `26013500`，天勤代码必然错误。
- **阻断点**：`src/data/quote_check.py` `product_code()`（L139-141）返回「所有字母」，
  同样得到 `rbc`；`src/risk/sizing.py` `_product_key()` 是同一写法。
- 新浪源（akshare）没有商品期权分钟线；`src/data/kline.py` `_to_sina_symbol()`（L415-442）
  也会把期权拼成 `RBC26013500`。期权只能走天勤，备用源需显式拒绝并提示。

### 2.2 合约元数据：CTP 已经能查，落表缺列

- `src/trader/ctp_td.py` `OnRspQryInstrument`（L600-614）已返回
  `InstrumentID / ProductClass / VolumeMultiple / PriceTick / OpenDate / ExpireDate`，
  其中 `ProductClass` 可区分期货/期权。
- `OnRspQryInstrumentMarginRate`（L618-631）已返回多头/空头保证金比例（按金额、按手数），
  卖方保证金数据源现成。
- `config/品种与合约.xlsx` 当前列见 `src/data/catalog.py` `_INSTRUMENT_COLUMNS`（L32-48）：
  `symbol / product / name / multiplier / margin_rate_pct / margin / commission / price_tick / ...`，
  **没有 期权类型 / 标的 / 到期日 / 行权价**。
- `src/domain/contracts.py` `build_contracts()`（L27-40）只保留
  `multiplier / name / margin_rate_pct`，**会丢弃 `price_tick`**；
  实盘 `_price_tick_for()` 因此只能退回全局 1 跳。这是现存缺陷，做期权前必须一起修。

### 2.3 策略层：注册即用

- `src/strategy/factory.py` 的 `REGISTRY`（L240-287）登记即生效；
  `ENGINE_LABELS` / `ENGINE_AUDIT_MODES` 各加一行即可在配置页出现。
- `src/strategy/base.py`（L15-60）只保存信号去重和最近开仓 K 线，不含期货假设。
- `src/strategy/types.py` `StrategyDecision`（L231-242）只有
  `action / entry_price / stop_price / meta`；期权买方把「权利金」当 entry、把「权利金止损价」当 stop 即可复用。
- `src/strategy/leg_router.py` `create_leg_router()`（L124-158）按 `strategy.legs` 每条腿建实例，
  期权腿与期货腿可混排，共享同一份持仓与信号状态。

### 2.4 风控与算仓：买方几乎零改动，卖方要新口径

- `src/risk/sizing.py` `calc_open()`（L175-220 起）取
  `min(意图手数, 保证金上限手数, 单笔上限)`，该结构对期权同样成立。
- `margin_for()`（L155-173）= `价格 × 乘数 × 保证金比例%`。
  - **买方**：权利金支出 = `价格 × 乘数`。把该合约的 `margin_rate_pct` 配成 100，公式结果正好等于权利金，
    买方算仓和仓位上限零改动可用。
  - **卖方**：保证金不是权利金，必须换成 CTP 口径或保守比例，需要新增函数。
- `cash_available`、组合腿配额、日亏损熔断、最小开仓间隔等全部与品种无关，可直接复用。

### 2.5 执行层：期权限价单字段与期货一致

- `src/trader/ctp_td.py` `insert()`（L219-252）只填
  `InstrumentID / Direction / CombOffsetFlag / LimitPrice / VolumeTotalOriginal` 等，
  期权标准限价单使用同一套字段，**不需要改**。
- `src/trader/ctp_symbols.py` `ctp_instrument_id()`（L48-54）已处理
  「上期所/大商所小写、郑商所/中金所大写」；`product_code()`（L6-12）取前导字母，
  `rb2601C3500 → rb` 恰好正确。
- 但 `_CTP_EXCHANGE_BY_PRODUCT`（L21-40）没有 `IO / HO / MO` 等中金所期权品种，
  股指期权会拿不到交易所。
- `has_working_order()`、`is_terminal()`、三方对账、告警均与品种无关，直接复用。

### 2.6 计价与盯市：按合约配置 tick 即可

- `src/scheduler/order_pricing.py` `ctp_opponent_limit()`（L14-61）按 tick 对齐、
  夹在涨跌停之间，对期权同样适用。
- `_price_tick_for()`（L97-113）先查 `risk.contracts`，但键是 `product_key(symbol)`；
  期权必须改成「按合约查」，否则同一品种不同行权价的 tick 会互相串。

### 2.7 账本、持仓、到期：缺口集中在这里

- `src/domain/trade.py` `TradeRecord`（L13-46）无期权字段，但都是可选字段，追加不破坏旧导出。
- `src/account/signal_book.py` 的 CTP 资金快照已含 `option_value`（L152），
  说明柜台侧字段已采到，只是账本没有期权口径。
- `src/utils/trading_day.py` 只有交易日/交易时段，**没有任何到期日、最后交易日、行权、被指派逻辑**。

### 2.8 回测：暂不复用

- `src/backtest/engine.py`、`src/backtest/rollover.py` 面向期货连续合约换月。
  期权回测需要权利金历史序列、到期处理和行权，属于二期，不阻塞实盘。

## 3. 最小改动清单（文件 + 位置 + 原因）

### 3.1 新增文件

| 新文件 | 内容 | 原因 |
| --- | --- | --- |
| `src/domain/options.py` | `parse_option_symbol()`、`is_option()`、`option_product()`、`option_kind()`（C/P）、`strike()`、`expiry()`；兼容 `rb2601C3500`、`m2601-C-3000`、`SR605C6000`、`IO2512-C-4000` 四种写法 | 全项目唯一的期权代码解析入口，替换十几处「去字母」写法 |
| `src/domain/option_contract.py` | 期权合约元数据 dataclass + 从 `品种与合约.xlsx` 读取；`lot_premium()`、`lot_margin()` 接口 | 期权按「合约」而非「品种」配置乘数/tick/保证金 |
| `src/risk/option_margin.py` | 买方权利金、卖方保证金（CTP 比例优先、缺数据时拒开）、组合保证金占用的保守估算 | 期货口径不能表示卖方保证金；风控仍是最高优先级拦截 |
| `src/strategy/option_long.py` | 方向性/末日期权买方策略：权利金入场、权利金止损、到期前强平 | 用例（2）赌末日期权 |
| `src/strategy/option_hedge.py` | 期货持仓黑天鹅保护：按持仓名义金额选虚值 put/call，输出买入信号 | 用例（1）期货对冲 |
| `src/account/option_lifecycle.py` | 到期日/最后交易日检查、到期自动平仓或放弃、行权/被指派对账钩子、拒开提示 | 唯一完全缺失的业务闭环 |
| `tests/test_option_symbols.py` 等 | 符号解析、tq 代码、买方/卖方算仓、tick、注册表、生命周期拒单 | 覆盖新增逻辑 |

### 3.2 修改文件（小改，不动架构）

| 文件 | 位置 | 改什么 | 原因 |
| --- | --- | --- | --- |
| `src/data/tq_source.py` | `tq_symbol()` L102-121；`_EXCHANGES` L26-36 | 增加期权分支，按 `交易所.合约` 直出；补 `IO/HO/MO` 等品种 | 现在会把期权拆成错误品种和月份 |
| `src/data/quote_check.py` | `product_code()` L139-141 | 改调 `options.option_product()` | 现返回 `rbc`，合约表/交易所全查不到 |
| `src/data/kline.py` | `_to_sina_symbol()` L415-442 | 遇到期权直接报错并提示改用天勤 | 新浪没有期权分钟线，避免静默拉错数据 |
| `src/domain/contracts.py` | `build_contracts()` L27-40 | 透传 `price_tick`、`option_type`、`strike`、`expiry`、`underlying`；支持「合约级」键覆盖品种级 | 现在丢 tick 且只能按品种配乘数 |
| `src/risk/sizing.py` | `margin_for()` L155-173；`_product_key()` | 买方返回权利金、卖方调 `option_margin.seller_margin()`；`_product_key` 改调统一解析 | 期货公式不适用于卖方 |
| `src/scheduler/order_pricing.py` | `_price_tick_for()` L97-113 | 先按完整合约查 tick，再退回品种 | 同品种不同行权价 tick 可能不同 |
| `src/trader/ctp_symbols.py` | `_CTP_EXCHANGE_BY_PRODUCT` L21-40 | 补 `IO/HO/MO` 等期权品种 | 否则中金所期权拿不到交易所 |
| `src/scheduler/order_executor.py` | 开仓算仓前（`calc_open()` 之前，L615-645 附近） | 调 `option_lifecycle.assert_tradable(symbol)`；到期/最后交易日限制拒开 | 防止到期日误开、行权不可控 |
| `src/strategy/factory.py` | `REGISTRY` L240-287、`ENGINE_LABELS`、`ENGINE_AUDIT_MODES` | 登记 `option_long`、`option_hedge` | 新增策略的唯一登记处 |
| `src/domain/trade.py` | `TradeRecord` L13-46 | 追加可选 `option_type/strike/expiry/underlying` | 复盘和导出能区分期权 |
| `config/品种与合约.xlsx` + `src/data/catalog.py` L32-48 | 表结构 | 新增 `合约类型 / 标的 / 到期日 / 行权价 / 期权方向` 列 | 期权元数据必须持久化 |
| `src/ui/ctp_probe.py` | 「写入品种表」L1719 起 | 写表时带上期权字段 | 从柜台查询结果一键落表，避免手填错 |

### 3.3 明确不改

- 主循环与调度：`src/scheduler/runner.py`
- 执行抽象与柜台：`TradingBot.place_order`、`src/trader/ctp_trader.py`
- 三方对账与冻结：`fill_reconcile.py`、`order_state.py`
- 告警体系：`alerts`、`_ops_alert` / `_ops_recovery`
- 账本主体：`src/account/signal_book.py`
- 回测引擎：`src/backtest/*`（二期）

## 4. 两个用例的落地路径

### 用例（1）期货黑天鹅对冲

- `option_hedge` 作为独立策略腿，读取现有 `risk.positions` 的期货持仓与名义金额。
- 触发条件：持仓名义金额 × 保护比例 → 选择虚值 1-2 档、剩余 20-45 天的 put（多头保护）或 call（空头保护）。
- 下单：复用 `BUY_OPEN` + 对手价限价；买方权利金计入亏损上限。
- 风控：权利金预算单独设上限（建议 `risk.option_premium_budget_pct`），不挤占期货保证金配额。
- 到期：`option_lifecycle` 在最后交易日前 3 个交易日提示，到期日强制平仓，避免行权。

### 用例（2）末日期权投机

- `option_long` 选择剩余 1-5 天、平值附近合约，按权利金突破/IV 变化入场。
- 入场价 = 权利金，止损价 = 权利金回撤比例；`margin_rate_pct=100` 使买方算仓直接等于权利金。
- 单笔最大亏损 = 权利金 + 手续费，天然符合「以损定开」。
- 到期日当天只平不开；最后交易日收盘前强平，禁止进入行权。

## 5. 能力缺口与二期

| 缺口 | 现状 | 本期处理 | 二期 |
| --- | --- | --- | --- |
| 期权 K 线/tick | 天勤理论支持，格式需修正；新浪不支持 | 修 `tq_symbol`，实盘只用天勤 | 补 tick 录制与 IV 序列 |
| 希腊值 | 全项目无 | 本期不做，策略只用价格/IV 阈值 | 本地 B-S 计算 delta/gamma/theta/vega |
| 到期/行权/被指派 | 无 | 新增 `option_lifecycle`，只做「到期前平仓」 | 接 CTP `ReqExercise`，处理实值行权与被指派 |
| 卖方保证金 | 有柜台查询，未入口径 | `option_margin` 优先用柜台比例，缺数据拒开 | 组合保证金（跨式/宽跨式）优化 |
| 期权回测 | 无权利金历史 | 不做 | 独立期权回测器 |
| 交易权限 | 柜台/交易所开通 | 非代码问题，实盘前确认 | — |

## 6. 验证方案

1. **符号解析单测**：`rb2601C3500`、`m2601-C-3000`、`SR605C6000`、`IO2512-C-4000`
   的品种、到期、方向、行权价、交易所全部正确。
2. **数据源单测**：`tq_symbol("rb2601C3500") == "SHFE.rb2601C3500"`；
   新浪源遇到期权抛错。
3. **算仓单测**：买方 `margin_for()` = 权利金；卖方缺保证金数据时 `calc_open()` 返回 0 并给出拒开原因；
   组合腿配额仍生效。
4. **策略注册单测**：`create_strategy({"strategy": {"engine": "option_long"}})` 成功；
   `option_hedge` 能在多腿路由中与期货腿共存。
5. **生命周期单测**：到期日/最后交易日拒开；到期前强平路径可执行。
6. **回归与静态检查**：
   `./.venv/Scripts/python.exe -m pytest tests -q`
   `./.venv/Scripts/python.exe -m ruff check src tests`
   `./.venv/Scripts/python.exe -m mypy src`
7. **仿真冒烟**：CTP 查询合约（`ProductClass` 为期权）→ 下 1 手虚值期权限价单 → 撤单 →
   三方对账 → 告警正常 → 账本出现期权成交记录。

## 7. 风险与边界

- **风控优先级不变**：缺保证金比例、缺到期日、缺 tick 一律拒开并告警，不做「估算放行」。
- **买方满仓风险**：权利金口径下 100% 保证金比例会让仓位上限看起来「很便宜」，
  必须额外设「期权权利金总预算%」，否则可能一次性买满。
- **流动性**：末日期权盘口极薄，`ctp_opponent_limit` 的让价跳数需要按合约单独配置。
- **卖方尾部风险**：本期建议只支持买方策略；卖方需先有组合保证金与强平保护。
- **回测不可用**：期权策略上线初期只能小仓位实盘/仿真验证，不能用现有期货回测背书。

## 8. 需要用户确认的决策点

1. 本期范围：只做**买方**（对冲 + 末日），还是同时支持卖方？
2. 数据源：确认天勤账号有期权行情权限；是否接受「期权只走天勤、新浪不兜底」？
3. 合约表：是否同意在 `config/品种与合约.xlsx` 增加 5 列（类型/标的/到期日/行权价/方向）？
4. 策略形态：`option_hedge` 是「自动跟随现有期货持仓」还是「手动指定保护的期货腿」？
5. 到期处理：默认「最后交易日收盘前强平」，是否需要保留行权选项？

## 9. 交付物

- 本 Spec：`docs/期权交易/spec.md`
- 任务清单：`docs/期权交易/tasks.md`
- 评估报告：`docs/期权交易/0.可行性评估.md`（已按 6 个问题写回）
- 代码：符号解析、合约元数据、期权算仓/保证金、两个买方策略、到期生命周期、五列表结构与落表已落地；
  主循环、执行抽象、对账、告警未改。
- 发布：`VERSION` 57.3.0，`CHANGELOG.md` 与 `README.md` 已同步。
- 本地仿真冒烟已完成；真实 CTP 仿真需柜台账号，按 README「CTP 上线四步」执行。

