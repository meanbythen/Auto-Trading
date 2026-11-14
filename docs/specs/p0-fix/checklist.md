# P0 修复验收清单

- [x] P0-1 缺 `dry_run` 键时 CTP 不连柜台、不发单；`load_config` 返回必含该键
- [x] P0-2 同一进程重复启动被单实例锁拦截；`run_once` 运行中第二次返回 False，结束后释放
- [x] P0-3 日亏熔断可跨夜自动解除；崩溃/遗留委托/未知原因跨夜保留；日切复查意图文件
- [x] P0-4 账本拒记时策略/风控仓位不变；平仓无价回退开仓价，仍无价则拒记
- [x] P0-5 手工单价格无效/账本失败时账本与内存都不变；手工单与扫描共用交易锁
- [x] P0-6 账本读取异常时开仓手数为 0 且写 error 日志；新账不误判满回撤
- [x] P0-7 意图写盘失败/已有未清理意图时不下单并熔断
- [x] P0-8 扫描清单包含当前品种与全部非空持仓；启动、每轮、手工开仓后刷新

## 验证记录

- `python -m ruff check .` → All checks passed
- `python -m mypy src` → Success: no issues found in 172 source files
- `python -m pytest -q` → 全量通过
- 版本更新：`VERSION` / `README.md` / `CHANGELOG.md` → 44.6.4
