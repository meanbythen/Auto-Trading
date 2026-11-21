# P1 账号账本一对一 + P2 信号/命令事件化 规格

基线版本：49.19.0（2026-11-21）
范围来源：`docs/系统架构与演进方案.md` §3 P1（L557-565）、P2（L567-577）、“尚未完成与后置项”（L607-614）。
原则：绞杀者模式逐块替换；每一步可单独上线/回退；JSON 仍是权威账本；四本 legacy 账继续可读。

---

## 1. 目标

### P1：一账号一本账

- 新账号 `book_id` 默认等于 `account_id`（已有行为，补齐边界）。
- 旧账号做**一次性、显式、可回退**的账本迁移：把 `live / paper / ctp_sim / ctp_live` 落到账号独立账本。
- 迁移只**复制**、不删除旧文件；每一步写 `data/accounts_audit.jsonl` 审计。
- 校验“一账号一本账、账本不可共享”，包括派生文件名冲突。
- 界面账本下拉与切换只认账号（account_id），无注册表时回退旧四本账。

### P2：命令/事件总线接管实盘编排

- 新增进程内命令总线，`run_forever()` 由命令队列 + 带超时等待驱动，替换 `time.sleep(1)` 轮询。
- 新增 `SIGNAL` 类型化事件：`scan_once` 产生非 HOLD 决策时，在原有 `journal.record_signal` 之后同步发布，供旁路订阅。
- 保留确定性顺序：风控、报单仍在调度线程串行完成；命令 FIFO。
- 保留旧入口：`scan_once()`、`run_forever()`、`stop()`、`--once` 行为不变；发件箱逻辑不动。

---

## 2. P1 详细设计

### 2.1 `src/domain/accounts.py`

新增：

```python
LEGACY_BOOK_IDS = frozenset(ORDER)   # {"live", "paper", "ctp_sim", "ctp_live"}

def book_layout(book_id: str, meta: dict[str, str] | None = None) -> dict[str, str]:
    """book_id → 账本/账期/状态/意图四类文件名；legacy 走固定表，其余按账号派生。"""
```

- `book_layout` 复用 `PROFILES` 与 `_derived_profile`，供迁移与冲突校验使用。
- `profile_of` 维持现状：legacy 优先固定表，其次注册表 `book_id`，再按 `account_id` 反查。
- `normalize_id` 维持现状（legacy 名称仍可识别，便于无注册表回退）。

新增：

```python
def book_choices(root) -> list[tuple[str, str]]:
    """账本下拉选项：[(account_id, label), ...]；无注册表时回退 ORDER。"""
```

- 读 `config/accounts.yaml`；列出全部账号（含停用/归档，便于查看历史账），标签用 `_registry_account_label` 同款“显示名 · 仿真/真实”。
- 标签重复时追加 `account_id` 保证唯一。
- 无注册表返回 `[(book_id, PROFILES[book_id]["label"]) for book_id in ORDER]`。

### 2.2 `src/account/registry.py`

新增 `one_book_per_account_enabled(cfg)`：

- 读 `trade.ctp.one_book_per_account`，默认 `True`；`false/off/0/no` 关闭。
- `true` 表示允许在注册表加载时执行一次性账本迁移。

新增：

```python
def migrate_legacy_books(root, registry, *, enabled=True) -> list[str]:
    """把共用 legacy 账本迁成账号独立账本：复制不删除，写审计，幂等。"""
```

规则：

1. 遍历 `registry.accounts`；跳过内置自测账号（`book_id == self_test` 已独立）。
2. 需要迁移的判定：`book_id` 为空、在 `LEGACY_BOOK_IDS`、或与其他账号的派生文件名冲突。
3. 目标 `book_id = account.account_id`（保证稳定、唯一）。
4. 用 `book_layout(old)` → `book_layout(new)`：
   - `book_name` / `state_name` / `intent_name`：源存在且目标不存在时 `shutil.copy2`。
   - `periods_dir`：源目录存在且目标不存在时 `shutil.copytree`。
   - `data/intents/<old>`：源目录存在且目标不存在时 `shutil.copytree`。
5. 逐账号写审计 `book_migrate`，字段：`account_id / old_book / new_book / copied / skipped`。
6. 更新 `account.book_id` 并 `touch()`；返回中文说明列表，供日志。
7. 全程异常只记 warning，不阻断启动；已复制的目标文件不删。

`ensure_registry()` 变更：

- 在建/读注册表、补齐自测账号之后，若 `one_book_per_account_enabled(cfg)`，调用 `migrate_legacy_books()`；有变更则 `registry.save(root)` 并审计。
- `trade.ctp.one_book_per_account: false` 时不迁移，保持旧账本可读。

`AccountRegistry.validate()` 增补：

- 先跑 `ta.validate_registry(...)`（已含 `book_id` 重复）。
- 再按 `book_layout(book_id)` 统计 `book_name`，同一文件名被两个账号使用时报：
  `账本文件 X 被 A 与 B 共用（一个账号一本账）`。

### 2.3 `src/account/workspace.py`

- `labels(root)`：有注册表时返回 `[label for _, label in book_choices(root)]`，否则回退 `ORDER`。
- 新增 `resolve_choice(choice, root)`：先按 `book_choices` 的标签精确匹配得到 `account_id`，匹配不到再 `normalize_id(choice)`（兼容旧标签与直接传 id）。
- `set_active_id` / `display_label` / `resolve_ctp_book` 不改。

### 2.4 `src/ui/account_tab.py`

- 下拉选项继续用 `labels(self.root_dir)`（现在来自账号）。
- `_on_account_switch` 把 `wanted = id_from_label(choice)` 改为 `wanted = resolve_choice(choice, self.root_dir)`。
- 其余提示/确认逻辑不变。

### 2.5 文档

- `docs/系统架构与演进方案.md`：
  - P1 勾选“账号与账本完全一对一”，补充迁移/回退说明。
  - P1 回退补充：`trade.ctp.one_book_per_account: false` 或手工把 `book_id` 改回 legacy。
  - “尚未完成与后置项”删去 P1 后置中的“一对一未做”，保留“legacy 账继续可读、未全量迁 SQLite”。

---

## 3. P2 详细设计

### 3.1 新增 `src/kernel/commands.py`

```python
SCAN = "scan"
FLATTEN = "flatten"
STOP = "stop"

@dataclass(frozen=True)
class Command:
    name: str
    data: Mapping[str, Any] = field(default_factory=dict)

class CommandBus:
    def __init__(self, maxsize: int = 256) -> None
    def post(self, name: str, data: Mapping[str, Any] | None = None) -> bool
    def get(self, timeout: float | None = None) -> Command | None
    def drain(self) -> list[Command]
    def clear(self) -> None
```

- 基于 `queue.Queue`，FIFO；满队列时 `post` 返回 `False` 并记 warning，不阻塞界面。
- `get` 超时返回 `None`。

### 3.2 `src/kernel/events.py`

- 复用现有 `SIGNAL` 常量，不改协议。

### 3.3 `src/scheduler/runner.py`

`TradingBot.__init__`：

```python
from src.kernel.commands import CommandBus
from src.kernel.events import EventBus
self.commands = CommandBus()
self.events = EventBus()
```

新增：

```python
def post_command(self, name: str, **data) -> bool:
    """界面/脚本下发命令；主循环按 FIFO 处理。"""
```

`_journal_signal()` 末尾（现有 journal+audit 之后）发布：

```python
self.events.publish(SIGNAL, {
    "trace_id", "account_id", "strategy_id", "symbol", "bar_time",
    "action", "price", "score", "reason", "stop_price",
})
```

新增 `_handle_command(cmd)`：

- `SCAN`：不做额外动作，主循环下一拍立即扫描（设置 `next_scan_at`）。
- `FLATTEN`：`_flatten_pending = True; flatten_if_due(force=True)`。
- `STOP`：`self.stop()`。

`run_forever()` 主循环替换为：

- 进入循环前 `next_scan_at = time.monotonic()`（第一拍立即扫描）。
- 每拍：
  1. `schedule.run_pending()`
  2. 计算等待时长 `min(1.0, max(0.0, next_scan_at - now))`，`cmd = self.commands.get(timeout=wait)`。
  3. 有命令则 `_handle_command(cmd)`；`SCAN` 令 `next_scan_at = 0` 立即扫描。
  4. 到达清仓钟点：沿用现有 `flatten_at` / `_flatten_pending` / `_flatten_retry_sec` 逻辑。
  5. `time.monotonic() >= next_scan_at` 时：`scan_once()`、`_maybe_shutdown_after_close()`、`next_scan_at = now + max(scan_interval, 1)`。
- 异常分支仍记 `loop_error` 并 `time.sleep(5)`。
- `finally` 清理逻辑不变。

约束：

- 命令处理与扫描在同一线程，顺序确定。
- `stop()` 仍直接置 `_running=False`；队列等待最长 1 秒即可退出。

### 3.4 测试

新增 `tests/test_command_bus.py`：

- `CommandBus` FIFO、超时返回 `None`、满队列 `post` 返回 `False`、`drain` 保序。
- `TradingBot._journal_signal` 发布 `SIGNAL`（monkeypatch `journal.record_signal/record_audit` 为 no-op；用 `object.__new__` 构造最小实例）。
- `_handle_command(STOP/FLATTEN)` 行为（最小实例）。

既有测试补充（`tests/test_account_registry.py`）：

- `migrate_legacy_books`：legacy `ctp_sim` → `acc_*`，复制账本/状态/意图，源文件保留，审计有记录；再跑一次幂等。
- `one_book_per_account_enabled` 默认 True、显式 false 关闭。
- `validate()` 能报派生文件名冲突。

`tests/test_account_workspace.py`：

- `labels()` 有注册表时返回账号标签；`resolve_choice` 能把标签解析回 account_id。

---

## 4. 验收

1. 新安装/旧安装启动后 `config/accounts.yaml` 中每个账号 `book_id == account_id`（自测账号固定 `self_test`），旧 `signal_account*.json` 原样存在。
2. 迁移后当前账号账本内容与迁移前一致（成交、持仓、权益、意图）。
3. 账户分析下拉只显示账号；切换写入 `data/active_account.json` 的 `account_id`。
4. `trade.ctp.one_book_per_account: false` 时不再迁移。
5. 常驻启动后，`post_command("scan")` 1 秒内触发扫描；`post_command("stop")` 正常退出；清仓钟点行为与改造前一致。
6. 非 HOLD 决策既有 `signals` 落库不变，同时 `EventBus` 能收到一条 `SIGNAL`。

## 5. 回退

- P1：`trade.ctp.one_book_per_account: false`；或把 `accounts.yaml` 的 `book_id` 手工改回 legacy（旧文件仍在）。
- P2：命令总线是新增对象，`run_forever` 保留原扫描/清仓/退出语义；如异常可把主循环等待改回 `time.sleep(1)`，`scan_once` 与 `_journal_signal` 无签名变化。
