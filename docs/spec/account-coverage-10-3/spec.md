# 账号包失败/迁移分支补测（测试报告 10.3）

## 1. 目标与验收

依据 `docs/测试报告/软件测试报告-2026-11-24.md` 第 10.3 节，为账号模块的失败分支与迁移分支补直接用例：

- `src/account` 包覆盖率 ≥ 80%（当前 79.4%，需至少再覆盖 18 条语句）。
- 三个目标模块各自尽量 ≥ 80%（当前 registry 69%、outbox 71%、ctp_sync 71%）。
- 不修改业务逻辑，只新增测试；不触碰只读附件目录。
- 全量用例保持通过，且 ruff / mypy / lint-imports 无新增问题。

## 2. 现状与关键事实

- 当前版本 53.7.10；测试环境 `.venv\Scripts\python.exe`（Python 3.12.10，pytest 9.1.1）。
- 全量默认 `-q -m 'not perf and not soak'`，基线 2239 项通过、覆盖率语句 70.20% / 分支 67.42%。
- 现有相关用例：`tests/test_account_registry.py`、`test_account_sqlite_migration.py`、
  `test_account_workspace.py`、`test_outbox.py`、`test_ctp_fill_accounting.py`、`test_ctp_sim_account.py`。
- 环境限制：`pytest --cov` 在本机（pytest-cov 7.1.0 + coverage 7.17.5 + numpy 2.5.4）会在收集阶段报
  `numpy: cannot load module more than once per process`；`coverage run -m pytest` + `coverage report`
  可正常采集。本次覆盖率一律用后者，并在报告中注明该环境差异。

## 3. 落点（精确到文件/函数/分支）

新增单一测试文件 `tests/test_account_failure_paths.py`，不扩写旧文件，避免与既有夹具耦合。

### 3.1 `src/account/registry.py`

| 函数 | 待覆盖行为 |
| --- | --- |
| `_rotate_if_needed` | `stat()` / `replace()` 抛 `OSError` 时跳过轮转（54-55、60-61） |
| `_audit` / `record_audit` | 审计目录不可写时只告警（73-74）；正常轮转生成 `.1` |
| `_split_fronts` | `None`、序列、`;`/`；`/换行分隔（93-100） |
| `AccountRegistry.load` | YAML 损坏、顶层非字典均返回 `None`（150-155） |
| `AccountRegistry.validate` | `book_id` 为空跳过（205） |
| `add_account` | 新建连接、复用模板建连接、`auth_code_env` 生成 `auth_code_ref`、非 CTP 渠道（247-268、277） |
| `duplicate_account` | 不存在抛 `KeyError`、自测账号抛 `ValueError`、跨环境复制连接（308-320） |
| `update_account` | 字段回写、环境跟随、共用连接不改环境、未知账号/自测账号报错（339-365、369） |
| `set_status` | 正常改状态、未知账号、自测账号拒绝（375-382） |
| `remove_account` | 未知返回 `False`、软删除幂等、硬删除连带连接、自测拒绝（386-400） |
| `materialize` | `trade`/`ctp`/`ctp_profiles` 类型异常、连接缺失记 note（420-435） |
| `sync_from_cfg` | 资金号/环境/前置/密码环境/认证码/手数回写，异常值忽略，自测跳过（504-556） |
| `_repoint_active_book` | 文件缺失、坏 JSON、已含 `account_id`、v1 字典/字符串、写入失败（587-614） |
| `archive_duplicate_legacy_books` | 内容一致归档、重名加时间戳、读失败、移动失败（640-678） |
| `migrate_legacy_books` | 复制失败计入 skipped，仍改 book_id（720-775） |
| `migrate_from_cfg` | 非字典/空名连接跳过、同环境第二本账换名（788-812） |
| `_backfill_gui_settings` | 空 account 块、坏 YAML、accounts 非列表、已有密码引用不覆盖（858-891） |
| `ensure_registry` | 文件坏返回 `None`、自动补齐保存失败继续、首次生成保存失败返回 `None`（943-967） |
| `apply_account_registry` | 意外异常回退旧档案（1000-1007） |
| `sync_registry_from_cfg` | 无文件、正常落盘+审计、异常吞掉（1010-1024） |
| `check_cfg_with_registry` | 预检冲突、无文件、异常吞掉（1027-1037） |
| `validate_registry_file` | 无文件/有文件（1040-1043） |

### 3.2 `src/account/outbox.py`

| 函数 | 待覆盖行为 |
| --- | --- |
| `enqueue` / `mark` | 非法事件类型、发件箱关闭、连接异常被吞（57-58、83-85、96-97、118-119） |
| `_rows` | 只读连接为 `None`、查询异常返回空（131-146） |
| `enqueue_order_stage` | 无 `trace_id`、未知阶段、非终态回报忽略、发出/拒单/撤单映射（250-282） |
| `recover_pending` | 处理器抛错保留 pending 并累加 attempts（297-298） |
| `process_ready` / `_fail` | 达到最大重试转 failed 并告警；告警失败不影响流转（190-202） |
| `report_failed` | 存量 failed 汇总告警、告警异常吞掉（205-220） |
| `OutboxDispatcher` | 重复 start、wake、stop、消费异常不退出（338-365） |
| `start_dispatcher` / `stop_dispatcher` / `wake_dispatcher` | 幂等复用、二次停止、唤醒已注册实例（372-404） |

### 3.3 `src/account/ctp_sync.py`

| 函数 | 待覆盖行为 |
| --- | --- |
| `_should_log_mtm` | 非数字返回 `True`，小幅跳动 `False`（43-45） |
| `apply_broker_runtime` | 无 gateway、柜台空权益沿用上一笔、坏余额/可用/权益、`risk=None`（61-118） |
| `push_gateway_account` | 无连接、无资金数据、补齐 available/balance、旧快照余额非法（127-171） |
| `align_active_book_to_ctp` | 非 CTP 通道直接返回（193-195） |
| `resident_book_guard` | live/sim 缺资金账号提示（242-246） |
| `can_pull_from_trader` | 无 trader、非 CTP 账、无 settings/gateway、会话未就绪、kind 匹配/不匹配（261-273） |

## 4. 验证步骤

1. 新增用例：`.venv\Scripts\python.exe -m pytest tests/test_account_failure_paths.py -q`。
2. 定向覆盖率：
   `coverage run --branch --data-file=build/.coverage-account --source=src/account -m pytest <账号相关用例> -q`
   然后 `coverage report --data-file=build/.coverage-account -m --include="src/account/*"`，
   确认 `registry.py`、`outbox.py`、`ctp_sync.py` 各自 ≥ 80%，包整体 ≥ 80%。
3. 全量：`.venv\Scripts\python.exe -m pytest -q`（默认排除 perf/soak）。
4. 静态检查：`ruff check src tests`、`mypy`、`lint-imports`（按仓库现有命令）。
5. 文档：更新测试报告 10.3 完成状态、DEF-05 与覆盖率表；版本升 53.7.11，
   同步 `CHANGELOG.md`、`README.md`、`VERSION`、TODO、架构 HTML 芯片、`docs/软件测试方案.md`、
   `docs/软件测试用例.md` 的用例数；最后 `python tools/check_doc_versions.py` 必须通过。

## 5. 约束

- 不修改 `src/` 业务代码；若发现真实缺陷，先记录再决定是否单独修复。
- 测试全部使用 `tmp_path`，无网络、无 GUI、无真实柜台登录。
- 中间产物放 `build/`，不进入交付目录。
