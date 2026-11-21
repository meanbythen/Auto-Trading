# 批次 A 修复规格：实盘上线前的五项硬门槛

- 目标版本：**44.12.0**
- 日期：2026-11-15
- 状态：**A2～A5 已实施**（随 44.12.0）；A1 人工演练待仿真/实盘账号；A5.1 华泰能力确认待回复；A5.5 决策链仿真已通过（[drill/A5.5-guardian-sim.md](drill/A5.5-guardian-sim.md)），真实账号带仓演练待执行
- 实施偏差：A3 按用户决定只做飞书 webhook（未做钉钉/SMTP，`runtime.alerts.external` 仅 `off|webhook`）；A5 按「华泰暂不支持条件单」处理，只交付冷备守护；`run_id` 末尾加 4 位随机，避免同进程同秒撞号
- 来源：`docs/TODO.md` 第一、二节；`project-review-2026.html` 第 10 节「P0 收尾」
- 前置：P0-1～P0-4 已随 44.11.0 交付

---

## 1. 背景与目标

P0 修完之后，程序在「进程正常活着」这一前提下已经能兜住成交回报、关窗、对账和扫描停滞。仍有三类现实风险没有闭合：

1. **日历失效**：交易所节假日表硬编码到 2026 年（[trading_day.py](file:///d:/Auto-Trading/src/utils/trading_day.py#L21-L44)），2027 年起收盘清仓与回测都会把节假日当交易日。
2. **无人知晓**：所有告警只写 `data/alerts.jsonl`（[alerts.py](file:///d:/Auto-Trading/src/ops/alerts.py#L30-L62)），人不在电脑前就看不到熔断、断线、对账差异。
3. **进程消失后的持仓**：标准 CTP 没有原生止损单，[protective.py](file:///d:/Auto-Trading/src/trader/protective.py#L24-L31) 恒返回不支持；进程被结束后，持仓没有任何保护。

批次 A 只解决「上线前必须有」的部分，不改策略、不改默认开平仓规则。

验收总目标：

- 2027 年任何一天的交易日判断正确；日历缺年份时实盘**不能**静默开仓。
- 熔断/断线/对账/心跳停滞能在 1 分钟内到达手机。
- 任何一条日志、告警、状态快照都能定位到唯一一次运行。
- 带仓关窗、幽灵仓补记两项人工演练留下可复核的证据。
- 进程被强制结束后，有可选方案在交易时段内平掉持仓（默认关闭，需你确认后开启）。

---

## 2. 范围

| 编号 | 事项 | 交付物 | 默认行为 |
|---|---|---|---|
| A1 | P0 人工演练 T5.3 / T5.4 | 演练记录 + 证据文件 | 一次性人工 |
| A2 | 节假日日历外置 + 缺年份阻断 | `config/holidays.yaml` + 装载与校验 | 2026 行为完全不变 |
| A3 | 告警外发（飞书/钉钉/邮件） | `src/ops/notify.py` + 配置 | 默认关闭 |
| A4 | `run_id` 贯穿事件、告警、状态 | 生成与透传 | 加字段，不改语义 |
| A5 | 断线/进程消失时的持仓保护 | 能力结论 + 冷备守护（可选） | 默认关闭 |

---

## 3. 非目标

- 不做同花顺界面条件单自动化（无稳定接口，误触风险高于收益；P0 spec 已排除）。
- 不改任何策略的开平仓信号与默认参数。
- 不改 GUI 布局，除 A3/A4 需要的最小提示。
- 不引入第三方依赖：外发用标准库 `urllib` / `smtplib`，进程存活用 `ctypes`。
- 不做多账户、CIS、分布式（属批次 C）。
- 不实现券商侧条件单的“猜测接口”；必须以华泰书面能力为准（见 A5）。

---

## 4. 总体约束

1. **默认路径零变化**：不打开新开关时，回测数字、开平仓、告警内容与 44.11.0 一致。
2. **失败不阻断主流程**：外发、日历读取、守护进程的任何异常都只能降级并记录，不能让扫描线程退出。
3. **实盘 fail-closed**：日历覆盖不足、配置非法时，只允许平仓，不允许开新仓。
4. **可回滚**：每项都能用配置关闭；A5 不参与主程序启动。
5. 提交粒度：A1 单独（文档/证据）、A2、A3、A4、A5 各自独立提交，提交前 `ruff check src tests` + 全量 `pytest`。

---

## 5. A1：P0 人工演练（T5.3 / T5.4）

### 5.1 目的

验证两条最危险的路径在真实账号下确实按设计工作，而不是只在单测里成立：

- T5.3：带仓关闭窗口 → 重启 → 对账与告警正确。
- T5.4：柜台有仓、程序无仓（phantom）→ 补记只发生一次。

### 5.2 环境

- 优先使用华泰仿真账号（`trade.ctp.account_kind=sim`），不足时用 `executor=sim` 做前半段、CTP 仿真做后半段。
- 合约用流动性好的主力（如 `bu2612`），手数 1 手。
- 全程录屏或截图，证据存 `docs/archive/project-review-2026/batch-a/drill/`。

### 5.3 演练步骤 T5.3

1. 启动常驻，手工开 1 手多单（交易台或策略均可），确认程序持仓 = 柜台持仓。
2. 点击窗口关闭：应出现「仍有持仓/挂单」确认框。
3. 选择「继续关闭」：日志出现 `shutdown` 事件；进程在 15 秒内退出。
4. 检查 `data/unprotected_positions.json` 已生成，内容含合约、方向、手数。
5. 重新启动：界面出现「上次带仓退出」提示；日志有 `unprotected_resume`；恢复前不报单。
6. 处理对账横幅：应当显示柜台与程序一致（或按差异提示认领）。
7. 确认止损价被重新评估；点击恢复后能正常扫描。
8. 平掉持仓，确认 `unprotected_positions.json` 被清除。

通过标准：第 3、4、5 步全部符合；无重复下单；无未处理告警。

### 5.4 演练步骤 T5.4

1. 仿真柜台手工开 1 手，程序内存不认领（或删除 `data/bot_state.json` 中的该持仓后重启）。
2. 启动后应出现「未认领」横幅与 `position_unclaimed` 告警。
3. 不做任何操作，等待两个扫描周期：确认**不会**自动补记。
4. 在横幅选择「认领」，确认程序持仓变为 1 手，且只记一次。
5. 反向场景：程序内存有仓、柜台手工平掉；连续两轮后应自动补记平仓，`events.jsonl` 只有一条对应事件。

通过标准：第 3 步不自动改仓；第 4、5 步各只发生一次；账本与柜台一致。

### 5.5 交付

- `drill/T5.3.md`、`drill/T5.4.md`：时间、账号类型、步骤截图、`events.jsonl` 与 `alerts.jsonl` 摘录、结论。
- 若失败：在本规格追加「偏差与修复」小节，修复后再演练，不跳过。

---

## 6. A2：节假日日历外置与缺年份阻断

### 6.1 现状

- `EXCHANGE_HOLIDAYS` 为 2026 年硬编码常量，`is_trading_day()` 只认它 + `schedule.closed_days`。
- 调用点：`src/scheduler/flatten_manager.py`（收盘清仓、交易日跳过）、`src/backtest/engine.py`（回测跳过休市）。
- 缺失年份时没有任何提示，程序会把节假日当交易日。

### 6.2 设计

新增 `config/holidays.yaml`：

```yaml
# 每年国务院公布放假安排后追加；周末不用写。
# 程序只把这里 + config.yaml 的 schedule.closed_days 当休市。
version: 1
years:
  2026:
    - "2026-01-01"
    - "2026-02-16"
    # ... 与当前硬编码保持一致
  2027: []
```

新增 `src/utils/holidays.py`：

```python
@dataclass(frozen=True)
class HolidayCalendar:
    years: dict[int, frozenset[date]]
    source: str

    @classmethod
    def load(cls, path: Path | str | None) -> "HolidayCalendar": ...
    def is_closed(self, day: date) -> bool: ...
    def covered_until(self) -> date: ...      # 已配置年份的最后一天
    def covers(self, day: date) -> bool: ...  # 该日期所在年份是否有配置
```

改造 `trading_day.py`：

- `is_trading_day(day, *, extra_closed=None, calendar=None)`。
- `calendar is None` 时回退到内置 `EXCHANGE_HOLIDAYS`（保证旧测试与未配置环境不变）。
- 新增 `calendar_coverage_ok(calendar, now, min_days=60)`：要求 `now + 60 天` 所在年份已配置。

启动校验（`TradingBot` 初始化或 `_start`）：

```text
if executor == "ctp" and not dry_run:
    if not calendar_coverage_ok(...):
        risk.halt("holiday_calendar_missing")
        emit_alert("holiday_calendar_missing", "节假日表未覆盖到 YYYY-MM-DD，已禁止开仓", level="error")
        orders_paused = True
```

- 只禁止开仓；止损与清仓继续按已有逻辑工作（但清仓依赖日历，缺覆盖时按“每天都是交易日”执行，宁可多平）。
- 回测：读同一份日历；缺年份时回测结果页顶部显示黄色提示「该区间节假日表缺失，结果可能偏差」。

配置新增：

```yaml
schedule:
  holidays_file: "config/holidays.yaml"
  holiday_min_cover_days: 60
```

### 6.3 行为变化

- 2026 年：与现在完全一致（YAML 内容等价）。
- 2027 年及以后：未配置 → 实盘阻断开仓 + 告警；仿真/回测只提示。

### 6.4 测试

- `tests/test_holidays.py`：YAML 解析、坏日期跳过、2026 与旧常量等价、2027 可识别、缺年份 `covers()` 为 False。
- `tests/test_trading_day.py` 增补：传 `calendar` 时优先于内置；缺年份 `calendar_coverage_ok` 为 False。
- `tests/test_flatten_due.py` 回归：2026 节假日仍不清仓。
- 启动阻断：构造 `executor=ctp`、`dry_run=False`、日历缺 2027 → `orders_paused=True` + 告警。

---

## 7. A3：告警外发

### 7.1 现状

`emit_alert()` 只追加写 `data/alerts.jsonl`，相同 `code|message` 10 分钟内不重复。界面可读最近一条。

### 7.2 设计

新增 `src/ops/notify.py`：

- `NotifyConfig.from_cfg(cfg)` 读取并校验配置；非法配置只记日志，等价于关闭。
- `notify(row: dict)`：入队即返回；后台单线程消费。
- 队列 `Queue(maxsize=200)`；满时丢最旧并计数，绝阻塞调用方。
- 通道：
  - 飞书/钉钉：POST JSON（文本消息）。
  - generic：POST 原始 JSON。
  - SMTP：`smtplib.SMTP_SSL`，正文纯文本。
- 超时 5 秒；失败重试 1 次；仍失败写 `logger.warning` 与本地 `data/alerts.jsonl` 的 `notify_failed`（不再外发，防循环）。
- 限流：同一 `code` 每小时最多 `rate_limit_per_hour` 条（默认 10）；超限只写本地。
- 脱敏：日志与消息中不出现 webhook URL、SMTP 密码；密码只从环境变量读。

`emit_alert()` 在写文件成功后调用 `notify(row)`；`row` 增加 `run_id`（A4）。

配置：

```yaml
runtime:
  alerts:
    external: "off"          # off | webhook | smtp | both
    min_level: "error"       # warning | error
    rate_limit_per_hour: 10
    webhook:
      kind: "feishu"         # feishu | dingtalk | generic
      url: ""
      timeout_sec: 5
    smtp:
      host: ""
      port: 465
      ssl: true
      user: ""
      password_env: "SMTP_PASSWORD"
      to: []
```

默认外发事件（`min_level=error` 时）：`halt`、`heartbeat_stale`、`ctp_down`、`ctp_book_mismatch`、`exit_with_positions`、`fill_lock_timeout`、`force_close`、`pending_intent`、`guardian_flatten`。`warning` 级（如 `strategy_health`）不外发，除非 `min_level=warning`。

### 7.3 测试

- `tests/test_notify.py`：配置解析、级别过滤、限流、队列满丢弃、HTTP 失败不影响 `emit_alert` 返回值、文件仍写入、超时端点不阻塞（用本地假服务器或 monkeypatch）。
- 手工：填入真实 webhook，触发 `emit_alert("test", ...)` 应 5 秒内收到；断开网络应只记 `notify_failed`。

---

## 8. A4：`run_id` 贯穿

### 8.1 设计

- `TradingBot.__init__` 生成：
  `run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}"`
- `_record_event()` 每条事件默认带 `run_id`；调用点不需要改。
- `_persist_state()` / `status_snapshot()` 增加 `run_id`、`started_at`。
- `alerts.set_run_id(run_id)`：`emit_alert` 写文件与外发都带 `run_id`。
- 界面：运行状态卡第二行显示短号（`run_id` 后 6 位）；日志面板标题同步。空间不足时只显示短号。
- `tools/` 脚本若自行构造 bot，同样自动获得，不需要额外参数。

### 8.2 兼容

旧 `events.jsonl` / `bot_state.json` 没有 `run_id` 时按空处理；复盘读取不受影响。

### 8.3 测试

- `test_run_events.py`（或现有事件测试）：事件行含 `run_id`；两个 bot 的 `run_id` 不同。
- `test_alerts.py` 增补：告警行含 `run_id`；未设置时字段为空。
- `test_state_store.py`：状态文件含 `run_id` 后可正常回读。

---

## 9. A5：断线/进程消失时的持仓保护

### 9.1 关键事实（必须先说清）

- 标准 CTP 报单只支持「立即成交/限价」，[ctp_td.py](file:///d:/Auto-Trading/src/trader/ctp_td.py#L225) 里 `ContingentCondition` 固定为 `Immediately`。
- 标准 CTP **没有**止损单/条件单接口；`ReqParkedOrderInsert` 是预埋单（到时段再报），不是价格触发的止损单，且是否开放由期货公司决定。
- 因此「柜台原生条件单」不是一个可以直接写的功能，必须先确认华泰开放了什么。

### 9.2 三条路径

| 路径 | 前提 | 优点 | 缺点 | 结论 |
|---|---|---|---|---|
| ① 券商条件单 API | 华泰提供独立接口与权限 | 断网/关机仍有效 | 需券商支持、可能要单独申请 | **先确认**，可用则实现 adapter |
| ② 冷备守护进程 | 本机另一个进程 | 无券商依赖，进程被杀可救 | 整机断电/断网无效；同账号互踢 | **默认方案（开关默认关）** |
| ③ 同花顺界面条件单 | GUI 自动化 | 无 API 依赖 | 坐标/弹窗不稳定，误触风险高 | **不做** |

### 9.3 本批交付

- A5.1 能力确认：向华泰客户经理书面确认是否存在条件单/止损单 API、预埋单权限、是否支持程序化。结论写入本规格附录（见第 16 节；当前按「不支持」实施，客户经理回复后再补 adapter）。
- A5.2 若①可用：实现 `CtpNativeStopAdapter.supported() -> True`，开仓成交后按止损价挂条件单；撤单/平仓联动；重启后先查柜台保护单再补挂。
- A5.3 若①不可用：实现 `tools/guardian.py` 冷备守护（下述），`runtime.guardian.enabled` 默认 `false`。

### 9.4 冷备守护（A5.3）

新增 `tools/guardian.py`，独立进程，不进 GUI：

- 每 10 秒读 `data/bot_state.json`：`run_id`、`pid`、`heartbeat_at`、`positions`、`trading_day`。
- 仅当以下条件**全部**满足才动作：
  1. `positions` 非空；
  2. `heartbeat_at` 超过 `stale_after_sec`（默认 180 秒）；
  3. 主进程 PID **已不存在**（Windows 用 `ctypes.windll.kernel32.OpenProcess` 判断，不引入 psutil）；
  4. 当前在交易时段；
  5. `trade.ctp` 配置完整。
- 动作：独立连 CTP → 确认结算单 → 查持仓 → 对手价平仓（每品种最多 3 次）→ 写 `data/guardian_actions.jsonl` + `emit_alert("guardian_flatten", ...)`。
- 若 PID 还在但心跳停滞：**只外发告警** `guardian_stale_main`，绝不连接（同账号会把主进程踢下线）。
- 启动时若发现 `guardian_actions.jsonl` 新于 `bot_state.json`，强制走一次三方对账后再恢复报单。
- 明确边界：整机断电、系统休眠、网络中断时守护进程同样不在，本方案无效；这类场景只能靠①。

配置：

```yaml
runtime:
  guardian:
    enabled: false
    poll_sec: 10
    stale_after_sec: 180
    max_close_attempts: 3
```

### 9.5 测试与验收

- `tests/test_guardian.py`：心跳新鲜不动、PID 存活不动、仓位为空不动、非交易时段不动、PID 消失触发一次且只一次（用假的 state 文件与注入的平仓函数）。
- 仿真演练：主进程带仓，任务管理器结束进程 → 守护在 60 秒内平仓，`guardian_actions.jsonl` 有一条记录。
- 反向演练：主进程暂停（SIGSTOP 等价：挂起线程/断点）但 PID 存活 → 守护不发单，只告警。
- 已执行部分（2026-11-15）：决策链 8 个场景仿真通过，平仓动作以探针替代、未连柜台，见 [drill/A5.5-guardian-sim.md](drill/A5.5-guardian-sim.md)；真实账号带仓演练仍待执行。

---

## 10. 配置变更汇总

| 键 | 默认 | 说明 |
|---|---|---|
| `schedule.holidays_file` | `config/holidays.yaml` | 节假日日历 |
| `schedule.holiday_min_cover_days` | `60` | 至少覆盖未来多少天 |
| `runtime.alerts.external` | `off` | 外发通道 |
| `runtime.alerts.min_level` | `error` | 外发级别 |
| `runtime.alerts.rate_limit_per_hour` | `10` | 同 code 限流 |
| `runtime.alerts.webhook.{kind,url,timeout_sec}` | 空 / feishu / 5 | webhook |
| `runtime.alerts.smtp.*` | 空 | 邮件 |
| `runtime.guardian.enabled` | `false` | 冷备守护 |
| `runtime.guardian.poll_sec` | `10` | 守护轮询 |
| `runtime.guardian.stale_after_sec` | `180` | 判定停滞 |

`config.example.yaml` 与 `docs/配置说明书.md` 同步；不写环境变量以外任何密码。

---

## 11. 文件改动清单

新增：

- `src/utils/holidays.py`
- `src/ops/notify.py`
- `tools/guardian.py`
- `config/holidays.yaml`
- `tests/test_holidays.py`、`tests/test_notify.py`、`tests/test_run_events.py`、`tests/test_guardian.py`
- `docs/archive/project-review-2026/batch-a/drill/`（A1 证据）

修改：

- `src/utils/trading_day.py`：可注入日历、覆盖检查
- `src/scheduler/flatten_manager.py`：使用日历
- `src/backtest/engine.py`：使用日历 + 缺年份提示
- `src/scheduler/runner.py`：启动校验、`run_id`、事件与状态字段、守护联动
- `src/ops/alerts.py`：`run_id`、外发调用
- `src/ui/dashboard.py`：状态卡第二行显示 `run_id` 短号（日历缺失通过熔断原因和告警呈现，未加独立横幅）
- `config/config.example.yaml`
- `README.md`、`docs/实盘交易简易说明书.md`、`docs/配置说明书.md`、`docs/TODO.md`、`CHANGELOG.md`

明确不修改：`src/strategy/**` 的开平仓逻辑、`src/trader/ctp_td.py` 的报单参数（除非 A5.2 确认可用后单独提交）。

---

## 12. 测试与验收计划

| 项 | 自动 | 手工 |
|---|---|---|
| A1 | — | 两段演练 + 证据 |
| A2 | 单测 + 2026 回归 + 缺年份阻断 | 改系统日期/伪造 `now` 验证横幅 |
| A3 | 单测 + 假服务器 | 真实 webhook/邮件各发一条 |
| A4 | 单测 + 状态回读 | 界面看到短号；重启后新号 |
| A5 | 单测 | 仿真杀进程/挂起两种演练 |

全量门槛：`ruff check src tests`、`mypy src`、`pytest tests/ -q` 全绿；`tools/sim_full_test.py` 通过。

---

## 13. 兼容、灰度与回滚

- 版本号 44.12.0（功能更新，不改开平仓规则）。
- 灰度顺序：先 `sim` 跑一天 → CTP 仿真一天 → CTP 实盘小资金一天。
- 回滚：关闭 `runtime.alerts.external`、`runtime.guardian.enabled`；日历文件删掉即回退内置 2026 表。代码回滚按单次提交 revert。
- 数据兼容：新增字段均为增量，旧 `bot_state.json` / `events.jsonl` 可继续读。

---

## 14. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 日历文件写错，把交易日当休市 | 少开仓/漏清仓 | 解析失败即回退内置表并告警；启动校验要求格式完整 |
| 外发通道阻塞 | 扫描变慢 | 队列异步 + 超时；主流程只入队 |
| webhook 泄露 | 被恶意调用 | URL 只存本地配置，日志脱敏 |
| 守护进程与主进程同时平仓 | 重复平仓/反向开仓 | 只在 PID 死亡后动作；启动先对账 |
| 同账号互踢 | 主进程断线 | 守护绝不在 PID 存活时连接 |
| 日历与外发都依赖本机 | 断电仍无保护 | 明确写入说明书；条件单（A5.1）才是根治 |

---

## 15. 需要你拍板

1. **节假日维护方式**：已定——每年手工追加 `config/holidays.yaml`，实盘缺年份只禁开仓。
2. **外发通道**：已定——先接飞书 webhook；钉钉/SMTP 暂不做。
3. **A5 路径**：已定——华泰暂按不支持条件单处理，先交付冷备守护（默认关，需手动启动）；客户经理回复后再评估 adapter。
4. **`run_id` 展示**：已定——日志、告警、状态文件都带，运行台状态卡显示后 6 位短号。
5. **演练时间**：待定——T5.3/T5.4 与守护演练需要仿真或实盘账号，账号就绪后按第 5 节步骤执行。

---

## 16. 附录：华泰条件单能力确认（待回复）

- 提问时间：2026-11-15
- 对象：华泰期货客户经理
- 问题：
  1. 是否提供条件单/止损单的独立 API（非界面预埋），程序化下单是否允许；
  2. `ReqParkedOrderInsert` 预埋单是否对本账户开放，是否按价格触发；
  3. 若支持，权限申请流程、测试环境与限制（单笔/单账户数量、有效期）。
- 当前结论：**未确认，按「不支持」实施**。在回复前不写任何猜测接口；持仓保护依赖本地止损、看门狗与冷备守护。
- 回复后动作：若①可用，新增 `CtpNativeStopAdapter` 实现并在开仓成交后挂保护单、重启时补挂；本规格第 9.2/9.3 节的设计不变。
