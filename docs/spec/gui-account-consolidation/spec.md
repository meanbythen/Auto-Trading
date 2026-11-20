# 交易账号页：删除「同花顺登录（GUI 通道）」块

## 1. 背景与结论

交易账号页顶部已有账号卡，但页面下方仍保留一块「同花顺登录（GUI 通道）」。
经代码核对，该块目前仍是 GUI 实盘登录的**真实数据源**：

- [gui_trader.py](file:///d:/Auto-Trading/src/trader/gui_trader.py#L356-L366) 读取 `cfg["account"]["resolved_password"]`、`user_id`、`captcha_mode`。
- [app_login.py](file:///d:/Auto-Trading/src/login/app_login.py#L317-L336) 读取同一组字段。
- [config_loader.py](file:///d:/Auto-Trading/src/utils/config_loader.py#L113-L127) 的 GUI 密码来自 `password_env` 环境变量或 `secrets.local.yaml` 的 `password`，**不读账号的 `password_ref`**。
- [session.py](file:///d:/Auto-Trading/src/account/session.py#L119-L122) 只在 `cfg["account"]["user_id"]` 为空时才写入账号资金号，导致切换 GUI 账号后仍可能用旧号登录。

因此不能直接删块，必须先把「资金账号、密码、GUI 登录行为」收口到账号，再删界面。

本次决策：**完全删除该块**。资金账号与密码跟随当前 GUI 账号；验证码模式、自动登录并入账号详情，由 GUI 账号统一管理。

## 2. 目标与非目标

### 目标

1. 交易账号页不再出现「同花顺登录（GUI 通道）」标题、输入框和开关。
2. GUI 账号在账号注册表中拥有自己的 `captcha_mode`、`skip_auto_login`。
3. 运行时按 `active_account.json` 把当前 GUI 账号的资金号、密码、GUI 参数写进 `cfg["account"]`。
4. 切换 GUI 账号后，绝不会继续使用上一个账号的资金号或密码。
5. 旧 `accounts.yaml`、旧 `config.yaml` 无需手工迁移，启动后行为保持。
6. SQLite 投影（`data/trading.db`）同步新增字段。

### 非目标

- 不新增「新增 GUI 账号」按钮（当前也没有该入口，只管理已迁移/已存在的 GUI 账号）。
- 不改变 CTP 账号、连接、账本、风控的任何逻辑。
- 不删除 `secrets.local.yaml` 只读回退链路。
- 不改 GUI 点击坐标、`trade.gui` 标定逻辑。

## 3. 设计决策

### 3.1 数据模型（YAML 权威）

在 [trading_account.py](file:///d:/Auto-Trading/src/domain/trading_account.py#L195-L258) 的 `TradingAccount` 增加两个字段：

| 字段 | 类型 | 默认值 | 含义 |
|---|---|---|---|
| `captcha_mode` | `str` | `"pause"` | GUI 登录遇到验证码时暂停或跳过，仅接受 `pause` / `skip` |
| `skip_auto_login` | `bool` | `True` | 与现有配置同名同义；`True`=不自动登录，`False`=自动登录 |

落地要求：

- `from_mapping` 读取：`captcha_mode` 非 `pause` / `skip` 时归一为 `"pause"`；`skip_auto_login` 用现有 `_bool(..., True)`。
- `to_mapping` / `asdict` 自动带上这两个字段。
- 不新增「通道名称」「密码环境变量名」字段：全仓库无消费方，属于死配置。
- `broker` 字段保留（账号上已有），GUI 详情页只读展示。

### 3.2 迁移与兼容

修改 [registry.py](file:///d:/Auto-Trading/src/account/registry.py)：

1. `migrate_from_cfg` 生成 GUI 账号时（约 L572-L588）补：
   - `broker=str((account_block or {}).get("broker") or "").strip()`（保持现有）
   - `captcha_mode=str(account_block.get("captcha_mode") or "pause")`
   - `skip_auto_login=bool(account_block.get("skip_auto_login", True))`
   - `password_ref=ref_for_env(password_env)`，仅当 `password_env` 非空。`FUTURES_PASSWORD` 会映射成 `secret://gui/password`，可继续命中环境变量或旧 `secrets.local.yaml` 的 `password`。
2. 删除 `sync_from_cfg` 末尾「把 `cfg.account.user_id` 盖到所有非 CTP 账号」的整段（约 L506-L514）。账号是权威，不能再被旧块反向覆盖。
3. 已存在的 `config/accounts.yaml` 不需要手工改：缺失字段由 `from_mapping` 默认值补齐，下次保存时写出。

### 3.3 运行时（单一入口）

修改 [session.py](file:///d:/Auto-Trading/src/account/session.py#L119-L123) 的 `resolve_session`，GUI 分支改为**每次全量覆盖**：

```python
if account.channel == ta.CHANNEL_GUI:
    block = cfg.setdefault("account", {})
    if isinstance(block, dict):
        ref = account.password_ref or "secret://gui/password"
        block["user_id"] = account.investor_id
        block["resolved_password"] = store.resolve(ref, secrets) if account.password_ref else store.resolve("secret://gui/password", secrets)
        block["captcha_mode"] = account.captcha_mode
        block["skip_auto_login"] = account.skip_auto_login
        block["_from_active_account"] = True
        if account.broker:
            block["broker"] = account.broker
```

说明：

- `resolved_password` 即使为空也要写空，确保不会残留上一个 GUI 账号的密码。
- `secret://gui/password` 是兼容回退：老账号没有 `password_ref` 时，仍能从 `FUTURES_PASSWORD` 环境变量或 `secrets.local.yaml` 的 `password` 取到旧密码。
- `_from_active_account=True` 是运行时标记，不落盘。

配套修改 [config_loader.py](file:///d:/Auto-Trading/src/utils/config_loader.py#L105-L127)：

- `inject_ctp_secrets` 中，`account.get("_from_active_account")` 为真时**跳过** GUI 密码的「空则从环境变量/旧文件补」逻辑，避免旧 `FUTURES_PASSWORD` 污染当前账号。
- 把 `_from_active_account` 加入「写盘前剔除运行时字段」的过滤元组（与 `resolved_password` 同一处，约 L40-L45）。

修改 [session.py](file:///d:/Auto-Trading/src/account/session.py#L142-L169) 的 `set_active_account`：

- GUI 账号校验：`account.investor_id` 为空时抛 `ValueError(f"{display_name} 还没填资金账号")`。
- 不强制要求密码：密码缺失时由登录流程报「密码是空的」，账号页显示「未保存」。

修改 [trading_account.py](file:///d:/Auto-Trading/src/domain/trading_account.py#L315-L396) 的 `validate_registry`：

- 启用中的 `channel == "gui"` 账号必须填 `investor_id`，否则报「启用中的 GUI 账号必须填资金账号」。
- 其余 CTP 校验不变。

### 3.4 界面

修改 [credentials_tab.py](file:///d:/Auto-Trading/src/ui/credentials_tab.py)：

1. 删除 `_pack_gui_login_section`（L719-L761）及 `build_credentials_tab` 中的调用（L848）。
2. `build_credentials_tab` 初始化变量时增加：
   - `app.account_captcha_var = ctk.StringVar(value="pause")`
   - `app.account_auto_login_var = ctk.BooleanVar(value=False)`
   （`account_auto_login_var` 表示「自动登录」是否打开，与账号里的 `skip_auto_login` 相反。）
3. `_render_account_detail`（L204-L299）：
   - 选中的是 CTP 账号：保持现状（显示「连接」下拉）。
   - 选中的是 GUI 账号：
     - 显示「资金账号」输入框；
     - 不显示「连接」下拉；
     - 增加「验证码模式」下拉：`["pause", "skip"]`，绑定 `account_captcha_var`；
     - 增加「自动登录」开关，绑定 `account_auto_login_var`，提示文案「开=启动时自动填密码并点登录；关=请先自己登录」；
     - 头部状态行追加只读 `期货公司 {account.broker}`（`broker` 为空则不显示）；
     - 密码行说明改为「密码只保存在本机加密文件（Windows 凭据加密）」。
4. `_save_account_fields`（L438-L470）：
   - GUI 账号额外写入：
     - `account.captcha_mode = app.account_captcha_var.get()`（只接受 `pause` / `skip`）
     - `account.skip_auto_login = not bool(app.account_auto_login_var.get())`
   - CTP 账号不读这两个值。
5. `_update_password`（L318-L348）：
   - 每账号引用规则扩展：若现有 `account.password_ref` 以 `/{account_id}/password` 结尾则保留；
   - 否则 GUI 账号用 `secret://gui/{account_id}/password`，CTP 账号维持 `secret://ctp/{environment}/{account_id}/password`。
6. `_switch_to_selected`（L633-L634）删除 `app.user_var.set(session.investor_id)`。

### 3.5 旧 GUI 变量清理

修改 [config_vars.py](file:///d:/Auto-Trading/src/ui/config_vars.py)：

- 删除初始化：`broker_var`、`channel_var`、`user_var`、`pwd_env_var`、`password_var`、`captcha_var`、`auto_login_var`（L365-L369、L382-L384）。
- 删除 `reset_from_cfg` 中对应赋值（L1063-L1067、L1073-L1074）。

修改 [config_collect.py](file:///d:/Auto-Trading/src/ui/config_collect.py)：

- 删除 `acc["broker"] / channel / user_id / password_env / captcha_mode / skip_auto_login` 六行写入（L766-L772）。`cfg["account"]` 由 `resolve_session` 维护，保存时原样保留。
- 删除 GUI 密码变量相关逻辑（L797-L820 中的 `pwd`、`pwd_env`、`acc["resolved_password"]` 分支）。
- 保留天勤密码、CTP 仿真密码、CTP 认证码的保存逻辑不变。

修改 [config_support.py](file:///d:/Auto-Trading/src/ui/config_support.py)：

- 删除 `_toggle_password`（L250-L251，`pwd_entry` 不再存在）。
- 删除保存成功日志中对 `self.password_var.get()` 的判断（L359-L365）。

修改 [app.py](file:///d:/Auto-Trading/src/ui/app.py#L155)：

- 删除 `self._show_password = ctk.BooleanVar(value=False)`（已无引用；天勤密码仍用 `_show_tq_password`）。

修改 [credentials_tab.py](file:///d:/Auto-Trading/src/ui/credentials_tab.py) 两处对新账号「期货公司」的取值：

- `_template_from_ui`（L399）与 `_new_account`（L496）不再读 `app.broker_var`，改为
  `str((app.cfg.get("account") or {}).get("broker") or "")`。

### 3.6 SQLite 投影

修改 [schema.sql](file:///d:/Auto-Trading/src/account/schema.sql#L37-L56) 的 `accounts` 表：

```sql
captcha_mode     TEXT    NOT NULL DEFAULT 'pause' CHECK (captcha_mode IN ('pause', 'skip')),
skip_auto_login  INTEGER NOT NULL DEFAULT 1 CHECK (skip_auto_login IN (0, 1)),
```

修改 [sqlite_migrate.py](file:///d:/Auto-Trading/src/account/sqlite_migrate.py)：

- `ACCOUNT_COLUMNS` 增加 `captcha_mode`、`skip_auto_login`。
- `_account_row` 写入 `account.captcha_mode` 和 `1 if account.skip_auto_login else 0`。
- `migrate()` 的 `_ensure_columns(conn, "accounts", {...})` 字典补两列，老库自动加列。

### 3.7 文档

更新 [配置说明书.md](file:///d:/Auto-Trading/docs/配置说明书.md) 中「同花顺登录」相关段落：
GUI 资金账号、密码、验证码模式、自动登录统一在「交易账号 → 账号详情」维护。

## 4. 变更文件清单

| 文件 | 改动 |
|---|---|
| `src/domain/trading_account.py` | 新增 `captcha_mode` / `skip_auto_login`；GUI 校验 |
| `src/account/registry.py` | 迁移补 GUI 参数；删除旧块反向覆盖 |
| `src/account/session.py` | GUI 分支全量写 `cfg["account"]`；GUI 资金号校验 |
| `src/utils/config_loader.py` | 阻止旧密码回填；运行时字段过滤 |
| `src/ui/credentials_tab.py` | 删除 GUI 块；GUI 参数并入详情；密码引用规则 |
| `src/ui/config_vars.py` | 删除旧 GUI 变量 |
| `src/ui/config_collect.py` | 删除旧 GUI 保存逻辑 |
| `src/ui/config_support.py` | 删除 `_toggle_password` 与旧密码日志 |
| `src/ui/app.py` | 删除 `_show_password` |
| `src/account/schema.sql` | accounts 表新增两列 |
| `src/account/sqlite_migrate.py` | 投影两列 |
| `docs/配置说明书.md` | 更新说明 |
| `tests/test_account_registry.py` | 迁移 + 运行时用例 |
| `tests/test_secret_store.py` | 不改 |

## 5. 验证

### 自动化

```powershell
python -m pytest -q
ruff check src tests
mypy src
```

新增用例：

1. `test_migrate_from_cfg_gui_keeps_gui_settings`
   - 旧 `cfg["account"]` 带 `captcha_mode="skip"`、`skip_auto_login=True`、`password_env="FUTURES_PASSWORD"`；
   - 断言 GUI 账号 `captcha_mode == "skip"`、`skip_auto_login is True`、`password_ref == "secret://gui/password"`。
2. `test_resolve_session_hydrates_gui_account_block`
   - 注册表含一条 GUI 账号，`password_ref` 指向测试密钥库；
   - `set_active_account` 后断言 `trade.executor == "gui"`、`account.user_id`、`resolved_password`、`captcha_mode`、`skip_auto_login`、`_from_active_account` 全部来自该账号。
3. `test_switch_gui_account_clears_previous_password`
   - 账号 A 有密码、账号 B 无密码；先切 A 再切 B，断言 B 的 `resolved_password == ""`，不残留 A 的密码。
4. `test_inject_ctp_secrets_skips_registry_managed_password`
   - `account["_from_active_account"]=True` 且 `resolved_password` 为空时，即使 `FUTURES_PASSWORD` 有值也不回填。
5. `test_validate_requires_investor_for_gui`
   - 启用中的 GUI 账号 `investor_id=""` → 返回「必须填资金账号」。

### 手工验收

1. 打开「交易账号」页：下方不再有「同花顺登录（GUI 通道）」块，页面保存按钮仍可用。
2. 选中 GUI 账号：详情出现「验证码模式」「自动登录」，改完保存后重开程序保持。
3. 选中 CTP 账号：详情不出现上述两项，连接下拉正常。
4. 把 GUI 账号设为当前账号，启动 GUI 通道登录：使用账号里的资金号与密码。
5. GUI 账号未保存密码时，登录报密码为空；不会偷偷使用另一个账号的密码。
6. 直接点「启动常驻」：沿用账号里的自动登录/验证码设置。

## 6. 风险与回滚

| 风险 | 处理 |
|---|---|
| 旧用户启动时 GUI 密码取不到 | 保留 `secret://gui/password` 回退（环境变量 + `secrets.local.yaml`） |
| 删除变量后其他页面引用报错 | 变更清单已覆盖全部 `grep` 命中点；`ruff`/启动自检兜底 |
| 旧库缺列 | `_ensure_columns` 自动 `ALTER TABLE` |
| 行为不符 | 回滚本次提交即可；`accounts.yaml` 多出的两个字段不影响旧代码读取 |

## 7. 验收标准

- 交易账号页无「同花顺登录（GUI 通道）」块。
- GUI 账号详情可维护验证码模式、自动登录。
- 切换 GUI 账号后 `cfg["account"]` 的资金号、密码、两项参数全部来自当前账号。
- 全量测试、`ruff`、`mypy` 通过。
- 不修改任何用户密钥明文，不新增明文落盘。

## 8. 审批

请确认以上规格后开始实施。
