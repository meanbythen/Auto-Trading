# P1 / P2 实施任务清单

## P1 账号账本一对一

1. `src/domain/accounts.py`
   - [x] 新增 `LEGACY_BOOK_IDS`。
   - [x] 新增 `book_layout(book_id, meta=None)`。
   - [x] 新增 `book_choices(root)`（无注册表回退四本旧账）。
2. `src/account/registry.py`
   - [x] 新增 `one_book_per_account_enabled(cfg)`（默认 True）。
   - [x] 新增 `migrate_legacy_books(root, registry, enabled=True)`：复制账本/状态/意图/账期，写审计，幂等。
   - [x] `ensure_registry()` 在加载/补齐后按开关执行迁移并保存注册表。
   - [x] `AccountRegistry.validate()` 增补派生账本文件名冲突校验。
3. `src/account/workspace.py`
   - [x] `labels()` 改为注册表账号标签。
   - [x] 新增 `resolve_choice(choice, root)`。
4. `src/ui/account_tab.py`
   - [x] 切换账号用 `resolve_choice`。
5. 文档 `docs/系统架构与演进方案.md`
   - [x] P1 勾选与回退说明；“尚未完成与后置项”同步。

## P2 信号 / 命令事件化

6. `src/kernel/commands.py`
   - [x] 新增 `Command` / `CommandBus`（FIFO、超时、满队列返回 False）。
7. `src/scheduler/runner.py`
   - [x] `__init__` 增加 `self.commands` / `self.events`。
   - [x] 新增 `post_command()`、`_handle_command()`。
   - [x] `_journal_signal()` 发布 `SIGNAL` 事件。
   - [x] `run_forever()` 用命令队列 + 超时等待替换每秒轮询，保留扫描/清仓/异常/退出语义。
8. 文档
   - [x] P2 勾选“命令/事件总线接管编排”，补回退说明。

## 测试与验证

9. [x] 新增 `tests/test_command_bus.py`（总线、SIGNAL、命令处理）。
10. [x] 扩充 `tests/test_account_registry.py`（迁移、幂等、开关、文件冲突）。
11. [x] 扩充 `tests/test_account_workspace.py`（labels / resolve_choice）。
12. [x] 运行 `python -m pytest -q --ignore=tests/test_ui_regression.py`。
13. [x] 设 `TCL_LIBRARY/TK_LIBRARY` 后运行 `tests/test_ui_regression.py`。
