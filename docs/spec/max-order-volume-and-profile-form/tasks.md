# 任务清单

## T1 统一手数口径

- [x] `src/trader/ctp_limits.py`（叶子模块）新增 `effective_max_order_volume(configured_max, profile_max, default=20)`，`ctp_profile.py` 顶部 re-export。
- [x] `src/trader/ctp_gateway.py` 第 190-194 行改用该函数。
- [x] `src/account/session.py` 第 129-138 行改为从 profile 键取档案值后调用该函数（保留 `configured_max` 在 materialize 之前读取的顺序）。
- [x] `src/ui/config_vars.py` 的 `_fill_ctp_profile_vars()` 用该函数回填 `ctp_max_volume_var`。

验收：公共 3 / 档案 20 时，界面显示 3、`CtpSettings.max_order_volume == 3`。

## T2 修复保存时的表单重灌

- [x] `src/ui/config_collect.py` 的 `_apply_ui_to_cfg()` CTP 段重排：先读全部表单值与手数，再写公共值与当前档案，最后才按需 `ctp_kind_var.set`。
- [x] `ctp_kind_var.set` 前比较目标标签，相同则跳过。
- [x] 保留 `ctp_profile_var` 切换与 `_fill_ctp_profile_vars()` 的既有行为（仅档案真的换掉时执行）。

验收：首次保存后界面保留刚输入的 3；前置/BrokerID/资金账号/环境变量名的新输入不再被旧值覆盖。

## T3 回归测试

- [x] 新增 `effective_max_order_volume` 的 5 组边界用例。
- [x] 新增“物化后档案 20 + 公共 3 → 回填 3”用例。
- [x] 新增“首次保存后手数与档案值均为 3”用例。
- [x] 运行 `python -m pytest tests/test_ctp_channel.py tests/test_ui_regression.py tests/test_risk.py -q`。
- [x] 运行完整 `python -m pytest -q`。
- [x] 运行 `git diff --check`。

## T4 复核与交付

- [x] 复核 `generic_stop_var` / `kelly_*_var` / `engine_var` 等 trace 不会重写用户输入（只 pack/pack_forget 面板、改标题与状态行）。
- [x] 输出结论：异常根因、修复点、其他设置项排查表、验证结果。
