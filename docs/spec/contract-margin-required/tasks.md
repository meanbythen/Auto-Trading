# 保证金比例缺失即拒绝开仓 Tasks（55.0.0）

- [x] T1 `src/risk/sizing.py`：删除 `bind_run_margin` 与 `self.margin_per_lot`；新增 `disable_margin_cap`；`margin_for` 末尾返回 0；`calc_open` 缺数据分支拒绝并给出提示；`_lots_by_margin_cap` 去 `None` 早退；模块 docstring 改口径。
- [x] T2 `src/screen/picker.py`：改用 `disable_margin_cap`；更新 docstring。
- [x] T3 `src/backtest/limits.py`：`skip_reason_key` 增加「保证金比例缺失，拒绝开仓」。
- [x] T4 界面删除：`config_tab_strategy.py`、`config_vars.py`、`config_collect.py`、`backtest_tab.py`、`backtest_config.py`、`config_support.py`、`backtest_actions.py`、`optimize_tab.py`、`account_tab.py`、`backtest/engine.py`、`config/config.example.yaml`。
- [x] T5 测试：改写 `test_sizing_margin.py` 并补 4 例（缺数据拒绝 / 只有金额放行 / ignore 开关 / 比例随价格）；同步 `test_symbol_picker.py`、`test_ui_regression.py`、`test_backtest_fill.py`、`test_backtest_risk.py`、`test_kelly.py`、`test_ctp_channel.py`。
- [x] T6 定向回归：`pytest tests/test_sizing_margin.py tests/test_symbol_picker.py tests/test_ui_regression.py tests/test_backtest_fill.py tests/test_backtest_risk.py tests/test_kelly.py tests/test_portfolio_run.py tests/test_portfolio.py tests/test_risk.py -q` 通过。
- [x] T7 全量门禁：`python tools/testkit.py baseline`（2515 项）+ `python tools/testkit.py gate --coverage` 6 步全 PASS。
- [x] T8 文档：README（L3/L13/L21/L865/L866）、`docs/配置说明书.md`（CF-34 删除、BT-20/AC-13 去键）、`docs/TODO.md`、两份架构 HTML（版本/行数/测试文件数）。
- [x] T9 升版与校验：`VERSION`=55.0.0，`CHANGELOG.md`、测试方案/用例/报告同步，`python tools/check_doc_versions.py` 全部 OK。
- [x] T10 汇报：删了什么、哪些情况会被拒开、需重启生效。
