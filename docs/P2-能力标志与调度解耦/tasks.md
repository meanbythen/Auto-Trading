# P2 任务清单

> 依据：[spec.md](spec.md)。一次一项，完成即勾。全部完成后统一跑门禁与全量测试。

- [x] T1 能力定义
  - `src/trader/base.py`：`Capabilities`、`DEFAULT_CAPABILITIES`、`capabilities_for()`、`TraderBase.capabilities`。
  - `sim_trader.py / gui_trader.py / ctp_trader.py`：覆写 `capabilities`。

- [x] T2 风控链
  - `src/kernel/risk_chain.py`：`TradeIntent` 换能力字段；`_stage/_working/_books` 改判能力。
  - `tests/test_kernel.py`：两处意图构造改能力字段。

- [x] T3 订单执行
  - `src/scheduler/order_executor.py`：`trader_capabilities`；`_trade_intent`、`_dispatch`、`_send_and_book` 按能力分支；去厂商文案。

- [x] T4 常驻调度
  - `src/scheduler/runner.py`：价格就绪、成交回调、启动对账、扫描登录、定时登录、多品种硬拦、启动日志。
  - `src/scheduler/login_flow.py`：6 处条件与日志文案。
  - `src/trader/go_live.py`、`src/ui/ctp_probe.py`：两处 `executor == "ctp"` 改能力判断。

- [x] T5 账本显示名
  - `src/domain/accounts.py`：label/short/hint/banner 中性化；`normalize_id` 增新别名、旧别名保留；id 与文件名不动。

- [x] T6 界面文案
  - `src/ui/account_tab.py`、`src/ui/run_tab.py`：厂商名文案与通道名分支改能力；控件与流程不动。
  - `tests/test_account_workspace.py`：两处显示名断言更新。

- [x] T7 假通道验收测试
  - 新增 `tests/test_channel_capabilities.py`：三通道能力断言 + 只下单不查询假通道跑通开/平仓与 `assumed_fill` 记账。

- [x] T8 验收
  - `rg 'executor == "ctp"|executor == "gui"' src` 无输出。
  - `.venv\Scripts\python.exe -m pytest -q` 全绿。
  - 汇报改动文件清单与结果。
