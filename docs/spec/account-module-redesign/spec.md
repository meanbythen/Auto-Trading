# 交易账号模块重构方案（账号 / 连接分离）

> 状态：方案待评审，本次不改代码  
> 范围：账号页信息架构、账号与连接的数据模型、账本归属、行情数据源归属  
> 非目标：事件总线改造、策略与回测重构、多账号并发交易

## 0. 三个问题的结论

| 问题 | 结论 |
| --- | --- |
| 换期货公司很难处理 | 根因是"适配器 / 连接 / 账号 / 凭据"压成了一个全局槽。把**连接**提升为一等对象后，换公司 = 新增一条连接 + 账号改绑，不改代码、不影响其他账号 |
| 数据源账号是否挪到数据管理 | 应该。天勤属于行情源，不是交易账号；交易页不再出现行情账号 |
| 通道是否与账号解耦 | 应该，但要拆两层：**适配器类型**（sim / gui / ctp，代码能力）和**柜台连接**（期货公司 / 前置 / BrokerID）。账号只引用连接。页面改名"交易账号" |

## 1. 现状诊断

### 1.1 注册表只是"物化层"，不是运行入口

`config/accounts.yaml` 在加载配置时被物化回 `trade.ctp.ctp_profiles` 与 `account_kind`（[config_loader.py](file:///d:/Auto-Trading/src/utils/config_loader.py#L430-L437)）。运行时真正认的是：

- `trade.executor`（全局执行通道）
- `trade.ctp.account_kind`（仿真 / 真实）
- `trade.ctp.ctp_profile`（券商档案）
- `data/active_account.json`（当前账本）

账号下拉只是编辑选择，不会切换运行账号。结果是同一件事有四个开关，用户无法从页面判断"现在到底用哪个账号、哪本账"。

### 1.2 连接模板被账号身份污染

`BrokerProfileTemplate`（[trading_account.py](file:///d:/Auto-Trading/src/domain/trading_account.py#L106-L175)）本应只描述"怎么连柜台"，但物化时会把账号资金号写进模板的 `user_id`（[registry.py](file:///d:/Auto-Trading/src/account/registry.py#L286-L351)）。

因此校验只能规定"一个账号一份模板"（[trading_account.py](file:///d:/Auto-Trading/src/domain/trading_account.py#L304-L315)），否则资金号互相覆盖。新增账号被迫复制连接，换期货公司又要在多处改前置和号码。

### 1.3 账本写死四本，与新账号冲突

[accounts.py](file:///d:/Auto-Trading/src/domain/accounts.py#L18-L80) 固定 `live / paper / ctp_sim / ctp_live` 四本账，`profile_of()` 对不认识的书 id 一律回退到 `live`（[accounts.py](file:///d:/Auto-Trading/src/domain/accounts.py#L125-L131)）。

注册表虽然会给后续账号生成 `book_acc_xxx`（[registry.py](file:///d:/Auto-Trading/src/account/registry.py#L192-L196)），但 `book_file()` 解析不了这个 id，实际仍写进 `live` 账。这是当前"新增账号不报错、账却混在一起"的隐患。

### 1.4 凭据三套硬编码

[credentials_tab.py](file:///d:/Auto-Trading/src/ui/credentials_tab.py#L499-L576) 里"同花顺 / 真实柜台"、"华泰仿真"、"天勤行情"是三段独立表单，字段各自直写 `config.yaml` 和 `secrets.local.yaml`，绕过注册表的 `password_ref`。换一家期货公司要同时改这三处相关字段。

### 1.5 页面职责混杂

- 执行通道在账号页和运行页各选一次，账号自身 `channel` 又表达一次。
- "CTP操作账"与"券商档案"是两个独立下拉，组合出来的状态无法用一句话描述。
- 天勤行情账号放在交易账号页。
- 注册表区域是"下拉 + 表单 + 7 个按钮"，不是列表，看不出有几个账号、各自状态。

## 2. 设计原则（对标成熟软件）

行业中 IBKR 多账户、CTP 生态（快期、交易开拓者）、vn.py 的 gateway/account、券商 OMS 的分账户模型基本一致：

1. **适配器是代码能力**：`sim / gui / ctp`，很少变化，不进入账号身份。
2. **连接是可复用资源**：一家期货公司的一条接入点（前置、BrokerID、AppID、流目录、超时）。连接**不含资金账号和密码**。
3. **账号是身份**：投资者账号、环境、状态、凭据引用、所属连接。
4. **会话/组合是绑定**：当前使用哪个账号、用哪本账、用哪套风控策略。
5. **凭据只存引用**：密码、认证码进 DPAPI（现有 `secret_store`），配置里只留 `secret://...`。
6. **数据源独立**：行情账号、默认源、拉取测试归数据管理。
7. **界面用主从列表**：左侧账号列表带状态徽标，右侧详情；行内动作；状态机"启用 / 停用 / 归档"。

## 3. 目标模型

```
适配器（sim / gui / ctp）
    └── 连接（期货公司接入点，可被多个账号复用）
            └── 交易账号（身份 + 环境 + 凭据引用 + 状态）
                    ├── 账本（一账号一本）
                    ├── 风控策略
                    └── 运行会话（当前使用账号 + 已解析通道）
行情数据源（provider + 凭据 + 默认源）──→ 运行会话
```

### 3.1 实体定义

#### Connection（柜台连接）

| 字段 | 说明 |
| --- | --- |
| `connection_id` | 稳定 id，如 `conn_huatai_live` |
| `label` | 显示名，如"华泰期货 主席" |
| `adapter` | `ctp / gui / sim` |
| `broker` | 期货公司名称 |
| `broker_id` | 柜台 BrokerID |
| `td_fronts` / `md_fronts` | 交易 / 行情前置，可多台 |
| `app_id` / `flow_dir` | CTP 应用标识与流文件目录 |
| `timeouts` | 登录、报单、撤单、查询间隔等 |
| `max_order_volume` | 本连接单笔上限 |
| `enabled` | 是否允许新账号绑定 |

**不包含**：资金账号、密码、认证码。

#### TradingAccount（交易账号）

| 字段 | 说明 |
| --- | --- |
| `account_id` | 稳定身份，`acc_*` |
| `display_name` | 显示名，可改 |
| `connection_id` | 引用连接 |
| `investor_id` | 资金账号（唯一身份） |
| `environment` | `sim / live`，硬约束 |
| `status` | `enabled / disabled / archived` |
| `password_ref` / `auth_code_ref` | 指向 `secret_store` 的引用 |
| `book_id` | 默认等于 `account_id` |
| `risk_policy_id` | 风控策略 |
| `max_order_volume` | 账号级上限，覆盖连接默认 |
| `notes` | 备注 |

`channel` 由 `connection.adapter` 推导，保留为只读字段用于查询兼容。

#### Book（账本）

- `book_id = account_id`（迁移后的旧账号沿用旧账本 id）。
- 文件名派生：账本 `signal_account_<slug>.json`、账期目录 `signal_periods_<slug>`、持仓状态 `bot_state_<slug>.json`、意图目录 `data/intents/<slug>`。
- `slug` 由 `account_id` 去掉 `acc_` 前缀，非法字符转下划线。

#### MarketDataSource（行情数据源）

| 字段 | 说明 |
| --- | --- |
| `source_id` | `tq / akshare / ths` 等 |
| `provider` | 数据商 |
| `enabled` | 是否启用 |
| `credential_ref` | 账号 / 密码引用 |
| `options` | 超时、重试、默认合约后缀 |

#### ActiveSelection（当前使用）

`data/active_account.json` 升级为：

```json
{
  "version": 2,
  "account_id": "acc_huatai_live_01",
  "updated_at": "2026-11-21T10:00:00"
}
```

账本、持仓、意图、风控状态都由 `account_id` 推导，不再单独存 book id。

### 3.2 关键约束

1. 一条连接可被多个账号引用；资金号只在账号上。
2. 账号与连接的环境必须一致（仿真账号不能绑真实连接）。
3. 真实账号不得使用仿真专用前置（沿用现有 `SIM_ONLY_MARKERS` 校验）。
4. 一个账号一本账；账本 id 全局唯一。
5. 启用中的 CTP 账号必须有连接、资金号和密码引用。
6. 归档账号只读；删除默认软删除（归档 + 保留账本）。
7. 所有改号、换环境、复制账号操作写审计 `data/accounts_audit.jsonl`。

## 4. 配置与存储

### 4.1 accounts.yaml v2

```yaml
version: 2
connections:
  - connection_id: conn_huatai_live
    label: 华泰期货 主席
    adapter: ctp
    broker: 华泰期货
    broker_id: "8080"
    td_fronts: ["tcp://...", "tcp://..."]
    md_fronts: ["tcp://..."]
    app_id: client_AutoTrading_1.0
    flow_dir: data/ctp_flow
    max_order_volume: 2
    enabled: true

accounts:
  - account_id: acc_huatai_live_01
    display_name: 华泰真实主号
    connection_id: conn_huatai_live
    investor_id: "12345678"
    environment: live
    status: enabled
    password_ref: "secret://ctp/live/12345678/password"
    auth_code_ref: "secret://ctp/live/12345678/auth_code"
    book_id: acc_huatai_live_01
    risk_policy_id: default
    max_order_volume: 2
```

迁移规则（v1 → v2）：

- `broker_profiles` → `connections`，删除其中的资金号与密码字段（旧值转入对应账号）。
- `accounts[*].profile_id` → `connection_id`。
- 旧账号 `book_id` 保留（`ctp_sim / ctp_live / live / paper`）；新账号 `book_id = account_id`。
- 已存在 `book_acc_xxx` 但无对应文件的账号，迁移为 `book_id = account_id`。
- 迁移前做一次冲突检查：若多个账号落到同一旧账本，拒绝自动迁移并提示先复制账号。
- 保留 v1 读取能力一个版本；写回统一写 v2。

### 4.2 SQLite 投影

`src/account/schema.sql` 同步调整（仍由 YAML 权威、SQLite 可重建）：

- `broker_profiles` 保留为连接表（可改名 `connections` 或保留旧名做映射）。
- `accounts` 增加 `password_ref`、`auth_code_ref`。
- 删除 `ux_accounts_profile` 唯一约束，允许多账号共用一条连接。
- `book_id` 默认写 `account_id`，保留唯一索引。
- 迁移脚本 `tools/migrate_accounts_to_sqlite.py` 增加 v1 → v2 分支。

### 4.3 账本解析修正

新增账本注册表（内存对象，来源 accounts.yaml）：

```text
resolve_book(book_id) -> {
  slug, book_file, periods_dir, state_file, intent_dir, funding, ctp_kind
}
```

解析顺序：

1. 已知旧账本 id（`live / paper / ctp_sim / ctp_live`）走现有 `PROFILES`；
2. 账本注册表命中 → 按 `account_id` 派生文件；
3. 未知 id → 报错并回退 `live`，同时写警告日志（不再静默合并）。

这条修正必须与账号迁移同批上线，否则新账号仍会写进 `live` 账。

## 5. 界面信息架构

### 5.1 交易账号页（原"账号"页改名）

```
┌ 交易账号 ─────────────────────────────────────────────┐
│ 当前使用                                                │
│  华泰真实主号 · LIVE · CTP · 连接 华泰期货主席 · 账本 01 │
│  连通状态：未测试        [切换账号] [测试连接]           │
├ [账号] [连接] ────────────────────────────────────────┤
│ 账号列表                      │ 账号详情                │
│ ● 华泰真实主号  LIVE 启用      │ 显示名   [          ]   │
│ ○ 华泰仿真01    SIM  停用      │ 连接     [华泰仿真 ▼]   │
│ ○ 同花顺实盘    LIVE 启用      │ 资金账号 [          ]   │
│                               │ 环境     LIVE（只读）   │
│ [+ 新增] [复制] [停用] [归档]  │ 密码     已保存 [更新]  │
│                               │ 账本     acc_...（只读）│
│                               │ 风控策略 [default ▼]    │
└────────────────────────────────────────────────────────┘
```

- 顶部"当前使用"卡片是唯一运行入口；切换账号时校验启用状态与连接完整。
- 账号列表列：显示名、环境、连接、资金账号后四位、状态、账本。
- 行内动作：测试连接、停用/启用、归档、复制。
- 凭据只显示"已保存 / 未保存"，不显示明文。
- 删除 = 软删除；归档账号不可切换为当前使用。

### 5.2 连接子区

```
连接列表                        连接详情
● 华泰期货主席  CTP  3 个账号    名称 / 适配器 / 期货公司
○ SimNow        CTP  1 个账号    BrokerID / 交易前置 / 行情前置
○ 同花顺本机     GUI  1 个账号    AppID / 流目录 / 超时
                                [测试连通] [停用连接]
```

- 连接详情显示"被 N 个账号引用"，停用前提示仍绑定的账号。
- 适配器类型是连接属性，不再有全局"执行通道"下拉。
- 新增期货公司：新增连接 → 账号改绑，其余账号不受影响。

### 5.3 数据管理 → 行情源

新增小区块：

```
行情数据源
  天勤      [已启用]  账号 [        ]  密码 已保存  [测试]
  新浪      [备用]                        [测试]
  默认源    [天勤 ▼]   失败回退 [新浪 ▼]
```

- 复用现有 `cfg.data.tq.user` 与 `secrets.tq_password`，只是搬家。
- 交易账号页删除"天勤行情"整段。
- 行情源与交易账号完全解耦，切换数据源不影响下单。

### 5.4 运行页

运行页不再提供执行通道选择，只显示解析结果："当前账号 / 连接 / 适配器"，并提供跳转到交易账号页的入口。避免两处选择互相覆盖。

## 6. 运行时解析

新增单一入口（建议放在 `src/account/session.py`）：

```python
def resolve_session(root, cfg, secrets) -> ResolvedSession:
    """按 active_account.json 解析出执行通道、CTP 配置、账本与风控状态。"""
```

解析顺序：

1. 读 `accounts.yaml`（缺失则回退旧 `ctp_profiles`，与现在一致）；
2. 读 `data/active_account.json` 的 `account_id`；
3. 账号 → 连接 → 适配器与前置；
4. 合并 `trade.ctp` 公共默认值，生成运行时 CTP 配置（替换现有 `materialize` 的输出）；
5. 解析 `password_ref / auth_code_ref`（DPAPI → 环境变量 → 旧 secrets，保持现有优先级）；
6. 解析账本与 `bot_state` 文件；
7. 交给现有 executor / trader / scheduler。

兼容策略：

- `materialize` 保留为"导出兼容层"，仍写 `trade.ctp.ctp_profiles`，供尚未迁移的调用方读取；
- 配置页保存时不再用界面字段反向覆盖其他账号的连接；
- `trade.executor` 继续写入当前会话解析结果，作为旧代码兜底。

## 7. 分步实施

### 第 1 步：信息架构与命名（不改数据模型）

- 账号页改名"交易账号"。
- 天勤行情迁到数据管理。
- 删除账号页的执行通道与 CTP 操作账下拉，改为"当前使用账号"只读卡片（数据仍取现有配置）。
- 账号注册表改为列表 + 详情形态。
- 验收：现有行为不变，全量测试通过；页面能一眼说清"当前用哪个账号、哪条连接、哪本账"。

### 第 2 步：连接与账号解耦

- `accounts.yaml` v2：连接不含身份与凭据；账号持有 `password_ref`。
- 允许一条连接被多个账号引用；校验改为"环境一致 + 真实号不得用仿真前置"。
- 账本 `book_id = account_id`，修正 `book_file()` 解析。
- 验收：同一连接下两个账号可独立记账、互不覆盖；重启后当前账号与账本保持。

### 第 3 步：运行时收口

- `active_account_id` 成为唯一运行入口；`resolve_session` 替换分散的物化逻辑。
- 运行页只读展示解析结果。
- 注册表投影到 SQLite，YAML 仍是权威。
- 验收：切换账号后执行通道、柜台、账本、风控状态一起切换，无残留旧值。

### 第 4 步：清理与固化

- 删除同花顺 / 华泰仿真硬编码块与旧下拉变量。
- 补齐迁移脚本、审计、UI 回归。
- 更新 `docs/配置说明书.md`、`docs/系统架构与演进方案.md`。

## 8. 测试计划

单元测试：

- 账号 / 连接校验：共用连接、环境不一致、真实号用仿真前置、重复账本。
- v1 → v2 迁移：字段搬移、冲突拒绝、幂等。
- 账本解析：旧四本、`account_id` 新账本、未知 id 报错。

集成测试：

- 两个真实账号共用一条连接，成交分别落在各自账本。
- 新增账号 → 重启 → 当前账号、账本、日开仓数、日亏损均恢复。
- 切换账号后 CTP 配置与执行通道同步变化。
- SQLite 投影失败不影响 YAML 与交易。

UI 回归：

- 账号列表状态徽标、凭据"已保存"、连接引用数、停用连接拦截。
- 运行页只读展示与账号页一致。

## 9. 风险与兼容

| 风险 | 处理 |
| --- | --- |
| 旧 4 本账与新账号账本混写 | 先修 `book_file()` 解析，再放开新增账号；迁移前做账本冲突检查 |
| `bot_state` 文件名变化导致持仓丢失 | 旧账号继续用旧文件名；只有新账本用新命名 |
| CTP 柜台权益口径变化 | 本次不动 `ctp_history_equity` 与 `apply_ctp_equity` |
| 用户手工改过 `config.yaml` | 读取仍兼容 `ctp_profiles`；保存时提示由注册表接管 |
| SQLite 已导入旧结构 | 迁移脚本带版本判断，可重复执行 |
| 界面大改影响日常操作 | 分四步上线，每步可单独回退 |

## 10. 暂不做

- 多账号同时在线交易（当前仍是单活动账号）。
- 策略与账号的多对多分配。
- 行情源的统一插件化改造（先搬家，复用现有天勤 / 新浪实现）。
- 事件总线与账本结构改造。

## 11. 验收标准

1. 新增一家期货公司只需"新增连接 + 改绑账号"，不改代码、不改其他账号。
2. 页面只有一个"当前使用账号"入口，运行页与账号页显示一致。
3. 天勤账号只出现在数据管理，交易账号页不再出现行情字段。
4. 两个同环境账号各自拥有独立账本，重启后账本与风控状态不串。
5. 凭据只以引用形式存在，界面不显示明文。
6. 关闭 SQLite 时 YAML/JSON 仍是唯一权威，行为与现在一致。
