# 审查报告 §7.4 第四批（规范与文档）规格

- 基线版本：49.21.0（2026-11-21）
- 范围来源：`docs/项目全面审查报告-2026-11-21.md` §7.4、§2「下一步（文档侧）」、§5.2、C1/C3
- 目标：补齐命名/快照/复盘交易日三个可修项，给审计与告警加轮转，为历史实验补索引，并把架构文档里已过期的状态改成实际情况。
- 原则：不引入循环依赖；不改 JSON 账本 / SQLite 投影权威；不动开平仓规则；不修改用户未跟踪文档 `docs/说明文本改进.md`；不 commit / 不 push。

---

## 1. 决策摘要

| ID | 决策 | 是否改变交易/回测结果 |
|----|------|------------------------|
| D7 | `normalize_id` 显式识别自测账 `self_test`，不再回落成 GUI 实盘 | 否（只修正账本路由） |
| C1 | `runner.status_snapshot()` 返回 `halt_kind`，界面“禁开新仓/已熔断”按真实原因显示 | 否（仅展示） |
| C3 | 复盘 `previous_trading_day` 跳过周末 + 内置节假日表 | 否（仅复盘窗口） |
| D6 | 旧四本账在内容与新账本完全一致时才**归档**（移入 `data/archive/legacy_books/`），不删除；影子盘按注册表解析当前 CTP 仿真/真实账本 | 否（只动历史文件位置与展示来源） |
| D9 | `accounts_audit.jsonl`、`alerts.jsonl` 加按大小的轮转；新增实验索引重建工具，只为历史实验补 `index.json`，不搬文件 | 否 |
| DOC | 更新架构文档中 ORDER_REQUEST/RISK_BLOCK、result_summary、SQLite 权威、回测差异的过期表述 | 否 |

版本号：`49.22.0`（新增轮转/索引工具与界面字段，属功能更新）。

---

## 2. D7 自测账命名豁免

### 修改 `src/domain/accounts.py`

- 顶部导入：`from src.domain.trading_account import SELF_TEST_BOOK_ID`（`trading_account` 不反向依赖 `accounts`，不构成循环）。
- `normalize_id()` 在旧四本账判断之前加入：

```python
if text == SELF_TEST_BOOK_ID:
    return SELF_TEST_BOOK_ID
```

- `profile_of()` 已能把注册表里的 `self_test` 派生为 `signal_account_self_test.json`，无需再改。

### 验收

- `normalize_id("self_test") == "self_test"`。
- `normalize_id("SELF_TEST") == "self_test"`。
- 其它未知文本仍回落 `LIVE`；`acc_xxx` / `book_xxx` 行为不变。

---

## 3. C1 快照补 halt_kind

### 修改 `src/scheduler/runner.py::status_snapshot`

在 `"halt_reason"` 后增加：

```python
"halt_kind": str(getattr(self.risk, "halt_kind", "") or ""),
```

### 验收

- 构造 `halted=True, halt_kind="holiday_calendar_missing"` 的 runner，`status_snapshot()["halt_kind"]` 原样返回。
- 未熔断时为空字符串。
- `ui/dashboard.py` 无需改动。

---

## 4. C3 复盘交易日跳过节假日

### 修改 `src/review/session.py`

```python
from src.utils.trading_day import is_trading_day

def previous_trading_day(day: date) -> date:
    """前一个交易日：跳过周末、内置节假日与临时休市（默认配置时用内置表）。"""
    prev = day - timedelta(days=1)
    while not is_trading_day(prev):
        prev -= timedelta(days=1)
    return prev
```

- 保持函数签名不变，`windows_for` 不动。
- 依赖方向 `review -> utils` 与现有 `review/consistency.py` 一致。

### 验收

- `previous_trading_day(date(2026, 10, 9))`（国庆后首日）返回 `2026-09-30`，不再返回 `10-08`。
- 普通周一仍返回上周五。
- `windows_for` 的夜盘起点随之使用节假日口径。

---

## 5. D6 旧账本归档 + 影子盘解析

### 5.1 新增 `archive_duplicate_legacy_books(root, registry)`（`src/account/registry.py`）

- 仅处理 `book_name` / `state_name` / `intent_name` 三类 JSON 账本文件，不含目录。
- 对注册表中 `book_id` 已不在 `LEGACY_BOOK_IDS` 的账号：
  - 旧文件名取 `book_layout(旧 book_id)`；目标文件名取 `book_layout(账号当前 book_id)`；
  - 仅当**源文件存在、目标文件存在、两者字节内容完全一致**时，把源文件 `move` 到 `data/archive/legacy_books/<旧名>`；
  - 内容不一致或目标缺失时**不动**，只记 `logger.info` 跳过原因。
- 每次归档写 `_audit(root, "legacy_book_archive", ...)`。
- 归档目录已存在同名文件时加时间戳后缀，不覆盖。
- 在 `migrate_legacy_books` 完成迁移（`notes` 非空）后调用一次；异常只记日志，不影响启动。

### 5.2 影子盘按注册表解析

### 修改 `src/domain/accounts.py` 新增：

```python
def ctp_book_ids(root: str | Path | None = None) -> dict[str, str]:
    """注册表里 CTP 渠道的 sim/live 账本 id；没有注册表时返回空。"""
```

- 读 `_registry_book_meta`，按 `channel == "ctp"` 且 `environment in {"sim","live"}` 取**第一个**账本 id（注册表顺序稳定）。

### 修改 `src/review/shadow.py::shadow_report`

```python
ids = ctp_book_ids(base)
sim = _side(base, ids.get("sim") or CTP_SIM, symbol=symbol)
live = _side(base, ids.get("live") or CTP_LIVE, symbol=symbol)
```

- 没有注册表或没有对应环境账号时，保持旧行为。

### 验收

- 临时根目录构造旧 `signal_account_ctp_sim.json` 与账号账本内容相同 → 调用后旧文件消失、出现在 `data/archive/legacy_books/`，账号账本不变。
- 内容不同 → 旧文件仍在。
- 注册表含 `acc_demo_sim`（`book_id=book_acc_demo_sim`，`environment=sim`）时，`shadow_report` 读取该书而不是 `ctp_sim`。

---

## 6. D9 日志轮转与实验索引

### 6.1 `accounts_audit.jsonl`（`src/account/registry.py::_audit`）

- 复用常量 `AUDIT_MAX_BYTES = 5 * 1024 * 1024`。
- 追加前若文件大小达到上限：`unlink .1` → `replace .1`（保留一代）。
- 写失败行为不变（只告警，不阻断保存）。

### 6.2 `alerts.jsonl`（`src/ops/alerts.py::emit_alert`）

- 新增 `ALERTS_MAX_BYTES = 5 * 1024 * 1024`，追加前同样滚动到 `alerts.jsonl.1`。
- 冷却去重逻辑不变。

### 6.3 `market_health.jsonl`

- 已有 20MB → `.1` 的轮转，保留现状；在文档中注明“已有轮转”，不重复改代码。

### 6.4 实验索引重建

### 新增 `src/backtest/experiment.py::rebuild_index(root: Path) -> int`

- 扫描 `root/experiments/*/manifest.json`，复用 `_update_index` 的行字段，重建 `root/experiments/index.json` 并返回条目数；
- 目录不存在返回 0；单个 manifest 损坏跳过并计数日志。
- 新增 `tools/rebuild_experiment_index.py`：默认项目根，`python tools/rebuild_experiment_index.py` 打印中文结果；**只读历史文件、只写 index.json，不搬动/删除实验目录**。

### 验收

- 空目录返回 0，不创建文件。
- 两个 manifest → `index.json` 含 2 行，字段与 `archive_report` 写入的一致。
- 运行工具后原实验目录文件数量不变。

---

## 7. 文档过期表述

### `docs/系统架构与演进方案.md`

- L577 附近 ORDER_REQUEST / RISK_BLOCK：改为实际状态——事件枚举已定义，生产链路未发布/未订阅，UI 进度不依赖它们。
- L616 附近 `result_summary`：改为“选股链路已统一；回测/优化/复盘仍读 performance，统一口径未完成”。
- §2.5：明确 SQLite `accounts` 仅作可重建投影，YAML + JSON 账本为权威（与 49.20.4 实现一致）。
- §4 回测差异表：以 §4.2 的 49.21.0 状态为准，补一句“已修/已标注”指向报告。

### `docs/项目全面审查报告-2026-11-21.md`

- §0 增加“0.4 第四批修复复核（49.22.0）”，逐条写 D6/D7/D9/C1/C3 与未验证项结论。
- §5.2、§7.4 标注完成状态。

---

## 8. 未验证项调查

按“能修则修，不能修在报告标注”处理：

1. **手工开平仓是否过风控闸门**：静态追踪交易台手工下单入口到 `run_early_gates` / `run_send_gates`；若绕过则补闸门并留审计，否则在报告写明依据。
2. **组合风控上限为 0**：检查 `src/risk/portfolio.py` 的 `observe` 分支，若 0 表示“未配置”且完全静默，补一条启动提示或文档说明，不改变拦截语义。
3. **`catch_up_on_start` 回测对应逻辑**：确认回测是否等价实现；无对应逻辑则在回测口径说明/报告中标注。

---

## 9. 验证与交付

```powershell
python -m pytest -q
lint-imports
python -m ruff check src tests
python -m mypy src
python -c "import src.ui.app; print('ok')"
```

新增测试：`tests/test_review_7_4_regression.py`（D7、C1、C3、D6 归档、D9 轮转/索引）。

文档与版本：

1. `VERSION` → `49.22.0`（单行）。
2. `CHANGELOG.md` 顶部新增 `## 49.22.0 — 2026-11-21`，中文逐条说明。
3. `README.md` 第 3 行与版本说明改为 `49.22.0`。
4. 报告 §0.4 / §5.2 / §7.4 与架构文档过期段落同步。

不 commit、不 push。

---

## 10. 需要确认

1. 是否接受版本号 `49.22.0`。
2. 是否接受 D6 的“内容一致才归档、否则不动”策略（避免误删历史账本）。
3. 是否接受 D9 只保留一代轮转文件（`.1`），不引入压缩/多代。
