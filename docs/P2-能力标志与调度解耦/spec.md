# P2 能力标志与调度解耦 — 实施规格

状态：待批准（批准后才改代码）
上位文档：[交易通道通用化升级方案.md](../交易通道通用化升级方案.md) 第六节 P2
范围：只做 P2。不改 CTP 券商档案（P1 已完成），不动 GUI 供应商包（P3），不动 `factory.py` 未知通道回退（P0 遗留）。

---

## 1. 目标与验收

目标：调度层、风控链、界面不再按「通道名」分支，改看 `TraderBase.capabilities`。新增通道只需声明能力，不改风控与调度。

验收（来自上位文档）：

1. `rg 'executor == "ctp"|executor == "gui"' src` 无结果（日志文案也不保留命中）。
2. 新增一个「只能下单、不能查询」的假通道，不改 `risk_chain.py / order_executor.py / runner.py` 即可跑通开仓、平仓与 `assumed_fill` 记账。
3. 旧配置不改，sim/gui/ctp 三通道行为与升级前等价。
4. 账本 id（`live/paper/ctp_sim/ctp_live`）、`data/` 文件名、`normalize_id` 全部旧别名不变。
5. 全量 `pytest -q` 通过。

---

## 2. 能力定义

新增文件位置：`src/trader/base.py`（不新建模块，避免循环导入）。

```python
@dataclass(frozen=True)
class Capabilities:
    can_query_positions: bool = False
    can_query_orders: bool = False
    confirms_fill: bool = False
    supports_symbol_switch: bool = False
    supports_price_mode: bool = False
    supports_cancel: bool = False
    window_driven: bool = False   # 靠桌面窗口点击下单（需要开软件、登录、标定坐标）

    @property
    def orders_via_broker(self) -> bool:
        """委托直接进柜台报单表，且能查柜台委托（CTP 这一类）。"""
        return self.confirms_fill and self.can_query_orders
```

说明：`window_driven` 是上位文档六个「建议字段」之外新增的一项。原因：runner / run_tab / prepare 中有多处「GUI 专属」行为（对手价准备、多品种硬拦、点击意图文件），这些不是成交确认能力，不能靠 `confirms_fill` 反推，也无法用 `can_query_*` 表达。

同时新增：

```python
DEFAULT_CAPABILITIES: dict[str, Capabilities] = {...}
def capabilities_for(executor: Any) -> Capabilities: ...  # 未知通道返回 Capabilities()
```

`TraderBase` 增加只读属性 `capabilities`，默认返回 `Capabilities()`（= 只能下单、不能查询、不确认成交、非窗口驱动）。

### 2.1 三个通道的声明

| 字段 | SimTrader | GuiTrader | CtpTrader |
|---|---|---|---|
| can_query_positions | False | False | True |
| can_query_orders | False | False | True |
| confirms_fill | True | False | True |
| supports_symbol_switch | True | True | True |
| supports_price_mode | True | True | True |
| supports_cancel | False | False | True |
| window_driven | False | True | False |
| orders_via_broker（派生） | False | False | True |

在各自类里覆写 `capabilities` 属性，返回 `DEFAULT_CAPABILITIES["sim"|"gui"|"ctp"]`。

### 2.2 行为等价矩阵（必须逐项保持）

| 现有分支 | 原条件 | 新条件 |
|---|---|---|
| 风控：上线阶段只拦开仓 | `executor == "ctp"` | `intent.can_query_orders` |
| 风控：未完成委托拦新单 | `executor == "ctp"` | `intent.can_query_orders` |
| 风控：柜台对账拦开仓 | `executor == "ctp"` | `intent.can_query_positions` |
| 定价：CTP 限价 + position_side | `executor == "ctp"` | `caps.orders_via_broker` |
| 定价：模拟盘用策略价 | `else`（sim） | `caps.confirms_fill`（非 broker） |
| 定价：点击通道不写价格 | `executor == "gui"` 或平仓 | `not caps.confirms_fill` 或平仓 |
| 点击意图文件 | `executor == "gui" and not dry_run` | `caps.window_driven and not dry_run` |
| 发单：CTP 报单/收尾 | `executor == "ctp"` | `caps.orders_via_broker` |
| 记账：assume_fill=false 只记发单 | `executor == "gui"` | `not caps.confirms_fill` |
| runner：GUI 价格就绪初值 | `executor_name == "gui"` | `caps.window_driven` |
| runner：挂成交回调 | `executor == "ctp"` | `caps.orders_via_broker` |
| runner/login：启动对账 | `executor != "ctp"` | `not caps.can_query_positions` |
| runner：扫描前登录方式 | `executor == "ctp"` | `caps.orders_via_broker` |
| runner：跳过定时 GUI 登录 | `executor == "ctp"` | `caps.orders_via_broker` |
| runner：多品种硬拦 | `executor == "gui"` | `caps.window_driven` |
| login_flow：对手价准备/窗口检查/GUI 提醒 | `executor != "gui"` | `not caps.window_driven` |
| login_flow：柜台对账 | `executor != "ctp"` | `not caps.can_query_positions` |
| login_flow：柜台资金 | `executor == "ctp"` | `caps.can_query_positions` |
| login_flow：CTP 登录 | `executor != "ctp"` | `not caps.orders_via_broker` |
| go_live：阶段推断 | `executor == "ctp"` | `capabilities_for(executor).orders_via_broker` |
| ctp_probe：常驻占用提醒 | `executor == "ctp"` | `capabilities_for(executor).orders_via_broker` |

---

## 3. 精确改动清单

### 3.1 `src/trader/base.py`

- 新增 `Capabilities`、`DEFAULT_CAPABILITIES`、`capabilities_for()`。
- `TraderBase` 增加 `capabilities` 属性（默认 `Capabilities()`）。

### 3.2 `src/trader/sim_trader.py` / `gui_trader.py` / `ctp_trader.py`

- 各类覆写 `capabilities`，返回值见 2.1。
- 不改构造、下单、点击、柜台逻辑。

### 3.3 `src/kernel/risk_chain.py`

- `TradeIntent` 删除 `executor: str = "sim"`，新增：
  `can_query_positions: bool = False`、`can_query_orders: bool = False`、`confirms_fill: bool = False`。
- `_stage` / `_working` 改判 `intent.can_query_orders`；`_books` 改判 `intent.can_query_positions`。
- 规则顺序、返回码、文案全部不动。

### 3.4 `src/scheduler/order_executor.py`

- 新增属性（供本 mixin 与 LoginFlowMixin 共用）：
  `trader_capabilities` → `getattr(self.trader, "capabilities", None) or Capabilities()`。
- `_trade_intent`：按能力填充 `can_query_positions / can_query_orders / confirms_fill`；`stage_block` 仅在 `can_query_orders` 时计算；`has_working_order` 仅在 `can_query_orders` 时查询。
- `_dispatch`：按 2.2 重写定价分支；意图文件条件改 `caps.window_driven and not dry_run`。
- `_send_and_book`：去掉 `executor` 形参；CTP 分支改 `caps.orders_via_broker`；assume_fill 记账分支改 `not caps.confirms_fill`。
- `_write_order_intent` 的 payload 字段保持 `"executor": "gui"` 不变（数据兼容，不是分支）。
- 日志文案去厂商名：`GUI 假定成交记账（请尽快核对同花顺真实持仓）` → `假定成交记账（请尽快核对交易软件真实持仓）`；其余「CTP」保留（通道类型，非券商名）。

### 3.5 `src/scheduler/runner.py`

- `_gui_price_ready` 初值改 `not (caps.window_driven and not dry_run)`。
- 成交回调：`caps.orders_via_broker and hasattr(self.trader, "set_fill_handler")`。
- `_reconcile_broker_at_startup`：`not caps.can_query_positions or dry_run` 直接返回。
- `scan_once`：`caps.orders_via_broker` 走 `_arm_ctp()`，否则 `do_login()`。
- `setup_schedule`：`caps.orders_via_broker` 跳过定时 GUI 登录，日志改「执行通道直接连柜台，跳过 GUI 定时登录」。
- `run_forever`：多品种硬拦改 `caps.window_driven`；启动登录分支按 2.2；启动日志删除「华泰期货_CTP主席」，改为打印通道类名与能力摘要。
- 注释中的「同花顺」改为「GUI / 交易软件」。

### 3.6 `src/scheduler/login_flow.py`

- 按 2.2 替换 6 处条件；日志文案去厂商名（「同花顺」→「GUI」/「交易软件」）。

### 3.7 `src/trader/go_live.py`、`src/ui/ctp_probe.py`

- 仅把 `executor == "ctp"` 两处改为 `capabilities_for(executor).orders_via_broker`（导入 `src.trader.base`）。
- 阶段行为、界面流程、字段全部不动。

### 3.8 `src/domain/accounts.py`（只改显示名）

| id | label | short | banner |
|---|---|---|---|
| live | GUI实盘账 | GUI实盘 | 记账：GUI实盘 |
| paper | GUI模拟账 | GUI模拟 | 记账：GUI模拟（请打开交易软件的模拟交易） |
| ctp_sim | CTP仿真 | CTP仿真 | 记账：CTP仿真（柜台资金） |
| ctp_live | CTP真实 | CTP真实 | 记账：CTP真实（柜台资金） |

- hint 改为中性表述（去掉同花顺/华泰，保留操作指引）。
- `normalize_id` 只增不改：新增 `"CTP仿真"`、`"CTP真实"`、`"GUI模拟"` 识别；`huatai_sim / huatai_live / 华泰仿真 / 华泰真实 / 同花顺模拟账 / ths_sim` 全部继续识别。
- `book_name / periods_dir / state_name / intent_name / funding / ctp_kind` **不动**。
- 模块 docstring 与注释去厂商名。

### 3.9 `src/ui/account_tab.py`

- 只改文案：同花顺 → GUI / 交易软件；华泰仿真、华泰真实账户 → CTP仿真、CTP真实。
- 涉及行：72、122、159、261、317、410–414、419–422、427、437–439、542、561、595、620、979。
- 不改控件结构、不改 `funding == "ctp"` 判断（账本资金类型，不是通道）。

### 3.10 `src/ui/run_tab.py`

- 把 `self.exec_var.get() == "ctp"` 改为 `capabilities_for(self.exec_var.get()).orders_via_broker`；
  `== "gui"` 改为 `.window_driven`。
- 文案：同花顺 → 交易软件 / GUI；提示语句语义不变。
- 不改上线阶段判断、账本守卫、启动流程。

### 3.11 测试

- `tests/test_kernel.py`：两处 `TradeIntent(executor="ctp", ...)` 改为
  `can_query_positions=True, can_query_orders=True, confirms_fill=True`。
- `tests/test_account_workspace.py`：两处显示名断言改为 `CTP真实` / `CTP仿真`（id 与 normalize 断言不动）。
- 新增 `tests/test_channel_capabilities.py`：
  1. 三通道能力默认值断言。
  2. `_BlindTrader(TraderBase)`：只实现 `place_order`，返回 `OrderResult(ok=True, assumed_fill=True)`，不实现查询；`capabilities` 用默认值。
  3. 用 `TradingBot.__new__` + 现有 `test_runner_guard.py` 的 `_bot()` 脚手架（补 `risk`、`cfg`、`root`、`kline`、`_record_signal_account`、`_contract_multiplier` 桩），注入 `_BlindTrader`，调用 `bot._execute(BUY_OPEN)` 与 `bot._execute(CLOSE)`；断言：两次均返回 True、 `place_order` 被调用、策略持仓由多仓回到空仓、事件 `order` 的 `assumed_fill` 为 True。
  4. 断言该假通道 `can_query_orders/can_query_positions` 为 False 时，风控链与调度代码不需要任何额外分支。

---

## 4. 验证步骤

1. 门禁：`rg 'executor == "ctp"|executor == "gui"' src` → 无输出。
2. 全量：`.venv\Scripts\python.exe -m pytest -q` → 全绿。
3. 等价抽查（单测）：
   - sim：开仓价仍取 `decision.entry_price`；平仓 `price=None`。
   - gui：真下单仍写意图文件；`assume_fill=false` 时仍只记发单。
   - ctp：仍走限价 + `position_side` + `_finish_ctp_order`。
4. 旧配置不动，启动日志只改文案，不新增必填配置。

## 5. 风险与回滚

- 风险：能力映射错位导致 CTP 走成点击记账路径。缓解：2.2 矩阵逐条对照 + ctp/gui 现有单测全跑。
- 风险：`TradeIntent` 字段变更影响测试与外部脚本。缓解：仓库内构造点已全部排查（仅 `order_executor.py` 与 `test_kernel.py`）。
- 回滚：本阶段不落盘改配置、不改数据文件；回滚只需回退代码。

## 6. 待批准

请确认本规格后开工。若有异议，重点在两项自行决定：
- 新增 `window_driven` 能力字段（上位文档未列，但 GUI 专属行为需要它）。
- 账本显示名按 3.8 表格替换（id / 文件名 / 别名不变）。
