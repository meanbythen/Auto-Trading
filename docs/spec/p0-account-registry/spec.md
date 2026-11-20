# P0 规格：账号注册表 + 密钥托管

基线：49.18.2　目标来源：[系统架构与演进方案.md](../../系统架构与演进方案.md) §2.5、§3 P0
原则：绞杀者模式，旧配置始终可启动；删掉新文件即可回退。

---

## 1. 目标与验收

把"账号只是配置里的一个槽"改成"注册表 + 连接模板 + 密钥引用 + 账本绑定"，并保持现有 CTP 链路不改调用方式。

验收（对应方案 §3 P0）：

1. 在账号页新增一个仿真账号，不改代码、不手改 YAML，重启后即可在券商档案下拉中选中并登录。
2. 真实账号误填仿真前置（SimNow / 华泰仿真地址）时保存被拦住，提示具体原因。
3. `config.yaml` 中不出现明文密码/认证码；凭据库可解析，凭据库不可用时回退环境变量与 `secrets.local.yaml`。
4. 删除 `config/accounts.yaml` 与 `config/secrets.dpapi.json` 后，旧 `ctp_profiles` 路径完全照旧可用。
5. `python -m pytest tests -q` 全绿；import-linter 三条约束不破。

---

## 2. 现状事实（已核对）

| 位置 | 事实 |
|---|---|
| `config/config.yaml` | `account` 单块（券商/通道/资金号/密码变量）；`trade.ctp.ctp_profiles` 两份：`huatai_sim`、`huatai_live`；`trade.ctp.ctp_profile=huatai_live`；`account_kind=live` |
| `src/utils/config_loader.py` | `load_config()` 读 YAML → `inject_ctp_secrets()` 注入 `resolved_*` → 品种表；`save_config()` 经 `_strip_ctp_runtime()` 去掉运行时密钥 |
| `src/trader/ctp_profile.py` | 所有消费方通过 `resolve_ctp_profile()` / `selected_profile_name()` / `profile_name_for_kind()` 读 `ctp_profiles`；无档案时留空并明确报错 |
| `src/ui/config_vars.py` | 档案下拉 = `list_profiles()`；输入框绑定 `ctp_profile_*_var` |
| `src/ui/config_collect.py` | `_apply_ui_to_cfg()` 把当前档案字段写回 `ctp_profiles[profile_name]` |
| `src/ui/config_support.py` | `_on_save()` 调 `save_config(cfg)`，随后对齐账本、推送运行时配置 |
| `src/ui/credentials_tab.py` | 账号页只有输入框，无 CRUD |
| 密钥 | `secrets.local.yaml` 明文 + 环境变量；`keyring` 未安装，`win32crypt`（DPAPI）可用 |
| 账本 | `src/domain/accounts.py` 四本固定账；`data/active_account.json` 只记当前选中 |

结论：新注册表只要在 `load_config()` 里"物化"成 `ctp_profiles`，现有登录、下单、界面都不用改。

---

## 3. 设计决策

### 3.1 存储：`config/accounts.yaml`

- 方案建议 SQLite，但 P0 阶段 YAML 更简单、可读、可手工修复，且"删文件即回退"。
- 结构：`version`、`accounts[]`、`broker_profiles[]`，字段见 §3.2。
- 原子写：先写 `.tmp` 再 `os.replace`。
- 迁移：首次 `load_config()` 时若文件不存在且旧配置有档案，自动生成并写审计；生成失败只告警，继续走旧路径。
- P1 再迁 `data/trading.db` 的 `accounts`/`broker_profiles` 表，字段不变。

### 3.2 数据模型（`src/domain/trading_account.py`，纯领域）

`BrokerProfileTemplate`（连接模板）：
`profile_id`、`label`、`broker`、`environment(sim/live)`、`broker_id`、`td_fronts[]`、`md_fronts[]`、`app_id`、`flow_dir`、`password_env`、`auth_code_env`、`password_ref`、`auth_code_ref`、`query_interval_sec`、`login_timeout_sec`、`order_wait_sec`、`cancel_wait_sec`、`order_count_warn`、`cancel_count_warn`、`close_split_products[]`、`price_tick`、`max_order_volume`、`auto_cancel_on_timeout`。

`TradingAccount`（账号）：
`account_id`（稳定，`acc_[a-z0-9_]+`）、`display_name`、`broker`、`channel(ctp/gui/sim)`、`environment(sim/live)`、`status(enabled/disabled/archived)`、`profile_id`、`investor_id`、`book_id`、`risk_policy_id`（预留，默认 `default`）、`max_order_volume`（0=沿用模板）、`notes`、`created_at`、`updated_at`。

约束：

- 一个账号对应一个 `profile_id`（现有 `ctp_profiles` 把 `user_id` 写在档案里，共用模板会互相覆盖资金号）。
- 一个账号一个 `book_id`，`book_id` 不得重复。
- 迁移时：`huatai_sim` → `acc_huatai_sim_01` / book `ctp_sim`；`huatai_live` → `acc_huatai_live_01` / book `ctp_live`；GUI 登录号 → `acc_gui_01` / book `live`。多余同类档案按顺序追加，book 用 `book_<account_id>`。

### 3.3 校验（保存前硬拦）

`validate_registry(accounts, profiles) -> list[str]`：

1. `account_id` / `profile_id` 格式与唯一性；`channel`、`environment`、`status` 取值合法。
2. `channel=ctp` 且 `status=enabled`：必须有模板、必须有资金号。
3. `account.environment` 必须等于 `profile.environment`。
4. `environment=live` 的模板前置命中仿真标记（`simnow`、`180.168.146.187`、`59.36.3.115`）→ 错误。
5. `environment=sim` 的资金号与任一 live 账号资金号相同 → 错误（仿真不得填真实资金号）。
6. 两个账号共用同一 `profile_id` → 错误；两个账号共用同一 `book_id` → 错误。

界面保存时若有错误：`messagebox.showerror` 列出前若干条并**中断保存**（不写 config.yaml）。

### 3.4 物化与双读（`src/account/registry.py`）

`apply_account_registry(root, cfg, legacy_secrets) -> list[str]`：

- 读 `accounts.yaml`；不存在则迁移旧配置并落盘；读失败 → 返回空列表，继续旧路径。
- 对每个 `channel=ctp` 的账号，把模板 + 账号字段合并写进 `cfg["trade"]["ctp"]["ctp_profiles"][profile_id]`：
  `label`、`kind=environment`、`broker_id`、`td_front/md_front`（多前置写成列表）、`app_id`、`flow_dir`、`password_env`、`auth_code_env`、`password_ref`、`auth_code_ref`、时限/告警阈值、`close_split_products`、`price_tick`、`max_order_volume`、`user_id=investor_id`、`account_id`。
- 密钥：`SecretStore.resolve(password_ref)` 命中则写 `resolved_password`；未命中交给随后执行的 `inject_ctp_secrets()` 按环境变量/旧文件补。
- 未纳入注册表的旧档案原样保留（保证并存）。
- 注册表与旧字段不一致时以注册表为准，`logger.info` 记录差异（双读比对），不覆盖值以外的行为。

`sync_registry_from_cfg(root, cfg) -> list[str]`：配置页保存时把 `ctp_profiles` 里的连接字段与资金号回写到对应账号/模板，避免界面修改被下次物化覆盖；环境变化写审计。

### 3.5 密钥托管（`src/utils/secret_store.py`）

- 后端：Windows DPAPI（`win32crypt.CryptProtectData`），密文存 `config/secrets.dpapi.json`（Base64，无明文）。
- ref 规范：`secret://ctp/sim/password`、`secret://ctp/live/password`、`secret://ctp/auth_code`、`secret://gui/password`、`secret://tq/password`；也接受 `env://NAME`。
- 解析顺序：DPAPI → 由 ref 推导的环境变量 → `secrets.local.yaml` 旧键（只读备份）。
- `set()` 在 DPAPI 不可用时抛 `SecretStoreUnavailable`，**不降级明文写盘**。
- `migrate_legacy(legacy_dict)`：把旧键值写入 DPAPI，返回已迁移 ref 列表；`secrets.local.yaml` 不改不删。
- `config_loader._read_ctp_secret()` 增加 DPAPI 查询（由环境变量名反推 ref），其余顺序不变。

### 3.6 账号页 CRUD（`src/ui/credentials_tab.py`）

新增「账号注册表」区块：

- 下拉：`显示名 · 券商 · 仿真/真实 · 状态`；选中后显示/可改 `显示名`、`资金账号`。
- 按钮：`新增仿真账号`、`新增真实账号`、`复制当前`、`停用/启用`、`归档`、`删除（软）`、`保存账号`。
- 新建：以当前档案输入框（交易/行情前置、BrokerID、密码变量、认证码变量）为模板复制连接参数；资金号必填（仿真可暂空但账号为停用）。
- 真实账号新建/切换到 live：要求输入资金账号后四位确认，写审计；取消则不落盘。
- 删除：默认软删除（状态改 `archived` 并加备注）；有持仓/未完成委托的检查留给 P1 账本服务，本阶段只做状态约束提示。
- 每次操作：`registry.save()` + `data/accounts_audit.jsonl` 追加 `{ts, action, account_id, fields}`（不记密钥值）。
- 操作完成调用 `self._reload_values_only()`，让档案下拉立即出现新账号。

### 3.7 审计

`data/accounts_audit.jsonl`，字段：`ts`、`action`（migrate/add/duplicate/update/status/delete/sync/env_change）、`account_id`、`detail`。密钥只记 ref。

### 3.8 回退

- 删除 `config/accounts.yaml` → 下次加载按当前配置重新迁移（行为与旧配置一致）。
- 删除 `config/secrets.dpapi.json` → 密钥回退环境变量/`secrets.local.yaml`。
- 代码级开关：`trade.ctp.account_registry: false` 时不迁移、不物化、不写任何旧字段，账号页也按关闭处理。

---

## 4. 变更清单

| 文件 | 动作 | 要点 |
|---|---|---|
| `src/domain/trading_account.py` | 新增 | 两个 dataclass、`validate_registry()`、`new_account_id()`；不 import `src.*` 其它模块 |
| `src/account/registry.py` | 新增 | `AccountRegistry`（load/save/CRUD）、`migrate_from_cfg()`、`ensure_registry()`、`apply_account_registry()`、`sync_registry_from_cfg()`、`_audit()` |
| `src/utils/secret_store.py` | 新增 | `SecretStore`、`ref_for_env()`、`env_for_ref()`、`migrate_legacy()`、`SecretStoreUnavailable` |
| `src/utils/config_loader.py` | 修改 | `load_config()` 在 `inject_ctp_secrets()` 前调 `apply_account_registry()`；`_read_ctp_secret()` 增加 DPAPI 源 |
| `src/ui/config_support.py` | 修改 | `_on_save()`：先 `sync_registry_from_cfg()`，再 `validate_registry()`，有错误 `showerror` 并 return |
| `src/ui/credentials_tab.py` | 修改 | 新增账号注册表区块与回调 |
| `tests/test_account_registry.py` | 新增 | 迁移、物化、双读回退、校验（仿真前置/资金号/模板共用） |
| `tests/test_secret_store.py` | 新增 | DPAPI 不可用注入假 cipher、ref 解析顺序、旧密钥迁移不改原文件 |
| `tests/test_config_registry_dualread.py` | 新增 | 有/无 `accounts.yaml` 时 `load_config` 行为、`config.yaml` 不含明文 |

不改：`src/trader/ctp_profile.py`、`ctp_gateway`、`ctp_trader`、`domain/accounts.py`（消费方保持原接口）。

---

## 5. 验证步骤

1. 单元测试：`python -m pytest tests/test_account_registry.py tests/test_secret_store.py tests/test_config_registry_dualread.py -q`
2. 全量：`python -m pytest tests -q`（基线 1090 项）
3. 分层约束：`python -m pytest --import-mode=importlib` 不受影响；如装了 import-linter 跑 `lint-imports`，否则人工确认 `src/domain/trading_account.py` 无 `src.*` 导入、`src/account/registry.py` 无 `src.ui`/`src.scheduler` 导入。
4. 手工回退：临时重命名 `config/accounts.yaml` 启动，确认档案下拉仍是 `huatai_sim`/`huatai_live`。
5. 手工新增：账号页新建仿真账号 → 重载 → 下拉出现 → 选中保存 → `config.yaml` 对应档案含 `account_id`、无 `resolved_*`。

---

## 6. 明确不做（留到后续阶段）

- SQLite `trading.db` 账号表、账本一账号一本的运行时改造（P1）。
- 停用账号阻止开仓、归档只读的运行时闸门（P1 风控服务）。
- 有持仓/未完成委托时拒绝删除的运行时检查（P1）。
- 凭据读取的完整审计流水（P4 可观测性）。
