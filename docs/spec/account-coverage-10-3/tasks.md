# 任务清单：账号包失败/迁移分支补测

- [x] T1 新建 `tests/test_account_failure_paths.py`，覆盖 registry 的审计轮转/损坏文件/增删改/状态流转、`sync_from_cfg`、迁移与归档、`ensure_registry` 保存失败、`sync_registry_from_cfg`、`check_cfg_with_registry`、`validate_registry_file`。
- [x] T2 同文件覆盖 outbox 的写入/状态失败、只读连接缺失、订单阶段映射、恢复失败、终态 failed 与告警、调度线程异常与幂等启停。
- [x] T3 同文件覆盖 ctp_sync 的 `_should_log_mtm`、`apply_broker_runtime` 空值/坏值分支、`push_gateway_account` 数据补齐与旧快照坏余额、非 CTP 对齐、常驻缺资金账号、`can_pull_from_trader`。
- [x] T4 运行新增用例并修正夹具，确保无网络/无 GUI、全部通过。
- [x] T5 用 `coverage run` + `coverage report` 采集账号包及三模块覆盖率，确认 registry/outbox/ctp_sync 各自 ≥80%、包整体 ≥80%。
- [x] T6 运行 ruff、mypy、lint-imports 与全量 pytest，确认无新增问题。
- [x] T7 更新测试报告 10.3（完成状态、DEF-05、覆盖率表），版本升 53.7.11，同步 CHANGELOG/README/VERSION/TODO/架构芯片/测试方案/测试用例的用例数，跑 `tools/check_doc_versions.py`。
