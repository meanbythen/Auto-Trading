# R10 任务清单

对应版本：49.17.0
规格：[spec.md](spec.md)

## A1 手续费

- [x] `config/品种与合约.xlsx` 补 7 个零费率在跑品种的开仓/平今手续费
- [x] `config/config.yaml` 经 `save_config` 同步手续费
- [x] `src/risk/commission.py` 新增 `zero_fee_products`
- [x] 评审单数据门禁：零费率写明「成本没扣」并把状态降到「注意」
- [x] `tests/test_commission.py` 覆盖零费率清单
- [x] `tests/test_release_sheet.py` 覆盖零费率降级

## B1 成交模型

- [x] `LiquidityModel` 默认开启锁板、部分成交、超时撤单；新增 `entry_timeout_bars`
- [x] `wait_fill_index`：锁板顺延到可成交K线，等满即撤单
- [x] 引擎按真正成交那根的开盘价成交；部分成交写「余量当根撤单」
- [x] 结果快照记录 `liquidity`；`fill_note` 追加成交约束说明
- [x] `config.example.yaml`、`config.yaml` 同步默认值
- [x] `tests/test_liquidity.py` 覆盖默认值与超时撤单

## C1 收盘口径

- [x] 清仓时刻由交易时段推出，删掉硬编码分组
- [x] `flatten_times_for_symbol` 统一回测 / 实盘 / 复盘入口
- [x] 今日一致性标出清仓窗口
- [x] `tests/test_session.py` 覆盖 lh / ag / lu / CFFEX 的清仓时刻

## D1 夜盘表

- [x] `lu` 移出 02:30 组；`bc` 补进 01:00 组；`ec` 补进无夜盘组
- [x] 品种与合约表 81 行时段与清仓时刻回写

## E1 时段统计与止损滑点

- [x] `summarize_by_session` / `quality_text` 按品种表时段归类，支持传入 `cfg`
- [x] 复盘页传入 `self.cfg`
- [x] `tests/test_slippage_log.py` 覆盖「生猪 22:10 归其他」
- [x] 止损滑点核实：`exit_fill_price` 取更差的止损/开盘价后按方向让滑点，无需改代码

## 全量校验

- [x] 全量 `pytest` 0 失败，`ruff`、`mypy` 通过

## 待人工

- [ ] 按期货公司结算单核对手续费（价格型费率会随价格变化）
- [ ] 两周 CTP 仿真 + 评审单签字（R6 / 放行门槛遗留）
