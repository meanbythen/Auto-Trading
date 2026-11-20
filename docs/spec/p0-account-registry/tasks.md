# P0 任务分解：账号注册表 + 密钥托管

对应规格：[spec.md](spec.md)

每个任务都能单独提交、单独回退。验证记录见 [checklist.md](checklist.md)。

## 任务 1：领域模型与校验

- [x] 新增 `src/domain/trading_account.py`
  - `BrokerProfileTemplate`、`TradingAccount`（`from_mapping`/`to_mapping`）
  - `validate_registry(accounts, profiles) -> list[str]`
  - `new_account_id()`
- 验证：`python -c "from src.domain.trading_account import validate_registry"` 可导入；无 `src.*` 其它导入。

## 任务 2：注册表存储与迁移

- [x] 新增 `src/account/registry.py`
  - `AccountRegistry.load/save/reload`，原子写 `config/accounts.yaml`
  - `add_account/duplicate_account/set_status/update_account/remove_account`
  - `migrate_from_cfg(cfg)`（`huatai_sim`/`huatai_live` + GUI 号 → 三条账号）
  - `ensure_registry(root, cfg)`：缺失自动迁移；失败只告警
  - `_audit(root, action, **fields)` 写 `data/accounts_audit.jsonl`
- 验证：`python -m pytest tests/test_account_registry.py -q`

## 任务 3：密钥托管

- [x] 新增 `src/utils/secret_store.py`
  - `SecretStore`（DPAPI + `config/secrets.dpapi.json`，Base64）
  - `resolve(ref)`：DPAPI → 环境变量 → `secrets.local.yaml`
  - `set/delete/list_refs/migrate_legacy`
  - `ref_for_env/env_for_ref/SecretStoreUnavailable`
- 验证：`python -m pytest tests/test_secret_store.py -q`（假 cipher + 无 DPAPI 回退）

## 任务 4：配置双读接入

- [x] 修改 `src/utils/config_loader.py`
  - `load_config()`：`inject_ctp_secrets()` 之前调用 `apply_account_registry()`
  - `_read_ctp_secret()`：增加 DPAPI 查询（`ref_for_env`，不借用旧文件别名）
- [x] 修改 `src/account/registry.py`：`apply_account_registry()` 物化 `ctp_profiles`，异常回退
- 验证：`python -m pytest tests/test_config_registry_dualread.py -q`；关闭开关后不生成注册表、旧档案保持原样。

## 任务 5：保存同步与保存前校验

- [x] 修改 `src/ui/config_support.py` `_on_save()`
  - `check_cfg_with_registry()` 预检；有错误 `messagebox.showerror` 并 return（不写盘）
  - `sync_registry_from_cfg(self.root_dir, cfg)` 回写档案字段
- 验证：预检覆盖真实账号 + 仿真前置；单元测试覆盖校验规则；`real_app` 全页构建通过。

## 任务 6：账号页 CRUD

- [x] 修改 `src/ui/credentials_tab.py`
  - 「账号注册表」区块：下拉、显示名/资金账号、新增仿真/新增真实/复制/停用启用/归档/删除/保存
  - 真实账号确认输入资金号后四位；操作写审计
  - 操作后物化并 `self._fill_ctp_profile_vars()`
- 验证：`test_ui_regression.py` 全部通过（账号页参与构建）；注册表增删改有单元测试。

## 任务 7：回归与文档

- [x] `python -m pytest tests -p no:warnings` 全绿：`1555 passed`
- [x] import-linter 三条约束人工确认：`src/domain/trading_account.py` 只 import 标准库；`src/account/registry.py` 不 import `src.ui`/`src/scheduler`
- [x] 在 `docs/系统架构与演进方案.md` P0 段勾选已完成项，记录回退方法
- [x] 汇报改动文件、验证命令、遗留项（P1 账本/风控/停用闸门）
