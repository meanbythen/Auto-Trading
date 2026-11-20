# 实施任务：删除 GUI 登录块并收口到账号

## T1 数据模型

- [x] `src/domain/trading_account.py`
  - `TradingAccount` 增加 `captcha_mode: str = "pause"`、`skip_auto_login: bool = True`。
  - `from_mapping` 读取并归一：验证码模式只允许 `pause` / `skip`；`skip_auto_login` 用 `_bool(..., True)`。
  - `validate_registry`：启用中的 GUI 账号必须有 `investor_id`。

## T2 注册表迁移与回写

- [x] `src/account/registry.py`
  - `migrate_from_cfg` 的 GUI 账号补 `broker`、`captcha_mode`、`skip_auto_login`、`password_ref=ref_for_env(password_env)`。
  - 删除 `sync_from_cfg` 末尾用 `cfg.account.user_id` 覆盖非 CTP 账号的整段。

## T3 运行时收口

- [x] `src/account/session.py`
  - `resolve_session` GUI 分支全量写 `user_id`、`resolved_password`（空也写）、`captcha_mode`、`skip_auto_login`、`_from_active_account`、`broker`。
  - 无 `password_ref` 时回退 `secret://gui/password`。
  - `set_active_account` 校验 GUI 账号资金号。
- [x] `src/utils/config_loader.py`
  - `inject_ctp_secrets` 遇到 `_from_active_account` 时跳过 GUI 密码回填。
  - 写盘过滤元组加入 `_from_active_account`。

## T4 界面

- [x] `src/ui/credentials_tab.py`
  - 删除 `_pack_gui_login_section` 及调用。
  - 初始化 `account_captcha_var`、`account_auto_login_var`。
  - GUI 详情显示验证码模式、自动登录、只读期货公司；不显示连接下拉。
  - `_save_account_fields` 写回两项 GUI 参数。
  - `_update_password` 按账号生成引用：GUI 用 `secret://gui/{account_id}/password`，保留已有每账号引用。
  - `_switch_to_selected` 删除 `user_var` 同步。
  - `_template_from_ui`、`_new_account` 的 broker 改读 `app.cfg["account"]["broker"]`。

## T5 旧变量清理

- [x] `src/ui/config_vars.py` 删除 7 个旧 GUI 变量及其 `reset_from_cfg` 赋值。
- [x] `src/ui/config_collect.py` 删除旧 GUI 字段写入和 GUI 密码分支，保留天勤/CTP 密钥保存。
- [x] `src/ui/config_support.py` 删除 `_toggle_password` 和旧密码日志判断。
- [x] `src/ui/app.py` 删除 `_show_password`。

## T6 SQLite 投影

- [x] `src/account/schema.sql` accounts 表新增 `captcha_mode`、`skip_auto_login`。
- [x] `src/account/sqlite_migrate.py` 更新 `ACCOUNT_COLUMNS`、`_account_row`、`_ensure_columns`。

## T7 文档

- [x] `docs/配置说明书.md` 改为：GUI 资金号/密码/验证码/自动登录在账号详情维护。

## T8 测试与验证

- [x] 新增 5 个用例（见 spec.md 第 5 节）。
- [x] 运行 `python -m pytest -q`。
- [x] 运行 `ruff check src tests`。
- [x] 运行 `mypy src`。
- [x] 手工验收 6 项。
