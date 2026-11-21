# 审查报告 §7.4 第四批（规范与文档）任务清单

- 规格：[spec.md](./spec.md)
- 基线：49.21.0；目标版本：49.22.0（已完成）
- 约束：不 commit / 不 push；不改用户未跟踪文档 `docs/说明文本改进.md`。

## 任务

1. **D7 自测账豁免**（`src/domain/accounts.py`）
   - [x] 导入 `SELF_TEST_BOOK_ID`，`normalize_id` 显式返回 `self_test`。
   - [x] 回归：大小写两种输入；未知文本仍回落 `LIVE`。

2. **C1 快照补 halt_kind**（`src/scheduler/runner.py`）
   - [x] `status_snapshot()` 增加 `halt_kind`。
   - [x] 回归：熔断/未熔断两种取值。

3. **C3 复盘交易日**（`src/review/session.py`）
   - [x] `previous_trading_day` 改用 `is_trading_day`。
   - [x] 回归：国庆后首日回落到 9/30；普通周一到周五。

4. **D6 旧账本归档 + 影子盘**（`src/account/registry.py`、`src/domain/accounts.py`、`src/review/shadow.py`）
   - [x] `archive_duplicate_legacy_books()`：内容一致才移入 `data/archive/legacy_books/`，并审计。
   - [x] `migrate_legacy_books` 启动时调用（幂等）。
   - [x] `ctp_book_ids()` + `shadow_report` 按注册表解析 sim/live。
   - [x] 回归：内容一致归档、内容不同保留、仍被引用保留、影子盘读账号账本。

5. **D9 轮转与实验索引**（`src/account/registry.py`、`src/ops/alerts.py`、`src/backtest/experiment.py`、`tools/`）
   - [x] `accounts_audit.jsonl` 5MB → `.1`。
   - [x] `alerts.jsonl` 5MB → `.1`。
   - [x] `rebuild_index()` + `tools/rebuild_experiment_index.py`，只建索引不搬文件；已为 99 个历史实验重建。
   - [x] 回归：轮转后新文件可继续追加；空目录返回 0；两条 manifest 生成两行索引。

6. **文档**
   - [x] `docs/系统架构与演进方案.md`：doc:577、doc:616、§1.5、§1.8、§2.5、§4。
   - [x] 报告 §0.4 / §5.2 / §7.4。

7. **未验证项**
   - [x] 手工下单闸门调查：手工单只改程序账本、不发柜台；CTP 交易台为人工测试入口，已在报告写明依据。
   - [x] 组合风控上限 0：补启动 warning“等同于不拦截”，不改变语义。
   - [x] `catch_up_on_start` 回测对应逻辑：无离线缺口，已写入 `live_gap_note`。

8. **验证**
   - [x] `python -m pytest -q`（退出码 0）
   - [x] `lint-imports`（3/3 KEPT）
   - [x] `python -m ruff check src tests`（通过）
   - [x] `python -m mypy src`（通过）
   - [x] `python -c "import src.ui.app; print('ok')"`（ok）

9. **版本与交付**
   - [x] `VERSION` = 49.22.0
   - [x] `CHANGELOG.md` 顶部新增 49.22.0
   - [x] `README.md` 第 3 行与版本说明
