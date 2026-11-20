# P0 验收清单：账号注册表 + 密钥托管

验证时间：2026-11-20　基线：49.18.2 之后

## 功能

- [x] 注册表首次加载自动生成：`config/accounts.yaml` 含 `acc_huatai_sim_01`、`acc_huatai_live_01`、`acc_gui_01`
  - 证据：`python -c "from src.utils.config_loader import load_config; ..."` 输出 3 条账号、2 个模板
- [x] 账号物化进 `ctp_profiles`，原档案名不变（`huatai_sim`/`huatai_live`），现有登录链路零改动
  - 证据：`tests/test_config_registry_dualread.py::test_load_config_migrates_and_materializes`
- [x] 注册表资金号/环境优先于旧档案
  - 证据：`tests/test_config_registry_dualread.py::test_registry_changes_win_over_old_profile`
- [x] 新增/复制/停用/归档/软删除走 `AccountRegistry`，并写 `data/accounts_audit.jsonl`
  - 证据：`tests/test_account_registry.py::test_add_and_duplicate_account`
- [x] 账号页新增「账号注册表」区块，操作后刷新券商档案下拉
  - 证据：`tests/test_ui_regression.py` 全页构建通过

## 校验（保存前硬拦）

- [x] 真实账号使用仿真前置（SimNow / 180.168.146.187 / 59.36.3.115）→ 报错
- [x] 仿真账号填真实资金号 → 报错
- [x] 两个账号共用模板或共用账本 → 报错
- [x] 启用中的 CTP 账号缺资金号/模板 → 报错
  - 证据：`tests/test_account_registry.py` 四个 `test_validate_*`

## 密钥

- [x] DPAPI 密文保存，文件内无明文
- [x] 解析顺序：DPAPI → 环境变量 → `secrets.local.yaml`
- [x] DPAPI 不可用时拒绝写盘，不降级明文
- [x] 旧密钥迁移后原文件不改不删
  - 证据：`tests/test_secret_store.py` 六个测试
- [x] 首次保存配置时自动调用 `migrate_legacy`，把明文搬进 DPAPI（只迁移一次）
- [x] `save_config()` 后 `config.yaml` 无 `resolved_password` / `resolved_auth_code` 明文
  - 证据：`tests/test_config_registry_dualread.py::test_saved_config_has_no_plaintext_secret`

## 回退

- [x] `trade.ctp.account_registry: false` → 不迁移、不物化、旧档案原样
  - 证据：`tests/test_config_registry_dualread.py::test_registry_can_be_disabled`
- [x] 注册表损坏 → 告警并回退旧 `ctp_profiles`
  - 证据：`tests/test_config_registry_dualread.py::test_broken_registry_falls_back_to_legacy`
- [x] 删除 `config/accounts.yaml` + `config/secrets.dpapi.json` 可回到旧路径

## 回归

- [x] `python -m pytest tests -p no:warnings` → `1555 passed`
- [x] 新增测试 24 项：账号注册表 13、密钥 6、双读 5
- [x] 分层约束：domain 纯标准库；account 不依赖 ui/scheduler
