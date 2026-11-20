"""
账号注册表：config/accounts.yaml 的读写、迁移和"物化"。

功能：
  1. 账号身份（account_id）与连接模板（profile）落成一份可读 YAML；
  2. 首次加载把旧配置的 ctp_profiles 与 account 块迁移成账号；
  3. 每次 load_config 时把账号物化进 trade.ctp.ctp_profiles，现有 CTP 链路不用改；
  4. 配置页保存时把界面改过的档案字段同步回注册表。

原理：注册表只是叠加层。文件不存在就不物化，旧路径完全照旧；删掉 accounts.yaml 即回退。
      密码只存引用，真实值交给 src.utils.secret_store 解析，不写回 config.yaml。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import yaml
from loguru import logger

from src.domain import trading_account as ta
from src.domain.trading_account import BrokerProfileTemplate, TradingAccount
from src.utils.secret_store import default_store, ref_for_env

REGISTRY_REL = Path("config") / "accounts.yaml"
AUDIT_REL = Path("data") / "accounts_audit.jsonl"
REGISTRY_VERSION = 1

# 迁移时沿用现有两本 CTP 账，保证账本文件不用搬家。
_LEGACY_BOOKS = {ta.ENV_SIM: "ctp_sim", ta.ENV_LIVE: "ctp_live"}
_DEFAULT_PASSWORD_ENV = {ta.ENV_SIM: "CTP_PASSWORD", ta.ENV_LIVE: "CTP_LIVE_PASSWORD"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _audit(root: str | Path, action: str, **fields: Any) -> None:
    """账号相关操作留痕。只记引用和字段名，不记密钥值。"""
    row = {"ts": _now(), "action": action, **fields}
    path = Path(root) / AUDIT_REL
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:  # 审计失败不能拦住账号保存
        logger.warning("账号审计写不进去: {}", exc)


def record_audit(root: str | Path, action: str, **fields: Any) -> None:
    """对外暴露的审计入口，界面按钮也能留痕。"""
    _audit(root, action, **fields)


def _front_value(fronts: Iterable[str]) -> str | list[str]:
    """单个前置写成字符串，多台写成列表，与现有 ctp_profiles 写法一致。"""
    items = [str(item).strip() for item in fronts if str(item).strip()]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return items


def _split_fronts(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value)
    for sep in (";", "\n", "；"):
        text = text.replace(sep, ",")
    return [item.strip() for item in text.split(",") if item.strip()]


@dataclass
class AccountRegistry:
    """accounts.yaml 的内存形态。"""

    accounts: list[TradingAccount] = field(default_factory=list)
    profiles: list[BrokerProfileTemplate] = field(default_factory=list)
    version: int = REGISTRY_VERSION

    # —— 读写 ——

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "AccountRegistry":
        box = data if isinstance(data, dict) else {}
        accounts = [
            TradingAccount.from_mapping(item)
            for item in (box.get("accounts") or [])
            if isinstance(item, dict)
        ]
        profiles = [
            BrokerProfileTemplate.from_mapping(item)
            for item in (box.get("broker_profiles") or [])
            if isinstance(item, dict)
        ]
        return cls(
            accounts=accounts,
            profiles=profiles,
            version=int(box.get("version") or REGISTRY_VERSION),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "accounts": [item.to_mapping() for item in self.accounts],
            "broker_profiles": [item.to_mapping() for item in self.profiles],
        }

    @classmethod
    def load(cls, root: str | Path) -> "AccountRegistry | None":
        """文件不存在返回 None；文件坏了也返回 None（调用方回退旧配置）。"""
        path = Path(root) / REGISTRY_REL
        if not path.exists():
            return None
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("账号注册表读不出来，回退旧 ctp_profiles: {}", exc)
            return None
        if not isinstance(data, dict):
            logger.warning("账号注册表格式不对（顶层不是字典），回退旧 ctp_profiles: {}", path)
            return None
        return cls.from_mapping(data)

    def save(self, root: str | Path) -> Path:
        """原子写：先写临时文件再替换，写一半不会毁掉旧注册表。"""
        path = Path(root) / REGISTRY_REL
        text = yaml.safe_dump(self.to_mapping(), allow_unicode=True, sort_keys=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        return path

    # —— 查询 ——

    def account(self, account_id: str) -> TradingAccount | None:
        key = str(account_id or "").strip().lower()
        return next((item for item in self.accounts if item.account_id == key), None)

    def profile(self, profile_id: str) -> BrokerProfileTemplate | None:
        key = str(profile_id or "").strip().lower()
        return next((item for item in self.profiles if item.profile_id == key), None)

    def ctp_accounts(self) -> list[TradingAccount]:
        return [item for item in self.accounts if item.channel == ta.CHANNEL_CTP]

    def validate(self) -> list[str]:
        return ta.validate_registry(self.accounts, self.profiles)

    # —— 增删改 ——

    def add_account(
        self,
        display_name: str,
        environment: str,
        investor_id: str = "",
        *,
        channel: str = ta.CHANNEL_CTP,
        broker: str = "",
        template: BrokerProfileTemplate | None = None,
        book_id: str = "",
        max_order_volume: int = 0,
        notes: str = "",
        status: str | None = None,
    ) -> TradingAccount:
        """新建一个账号；CTP 账号会复制一份连接模板，模板 id 跟着 account_id。"""
        env = ta.normalize_environment(environment)
        channel = ta.normalize_channel(channel)
        base = f"{broker or 'ctp'}_{env}"
        account_id = ta.new_account_id(base, [item.account_id for item in self.accounts])
        status = status or (ta.STATUS_ENABLED if investor_id or channel != ta.CHANNEL_CTP else ta.STATUS_DISABLED)
        profile_id = ""
        if channel == ta.CHANNEL_CTP:
            source = template or BrokerProfileTemplate(profile_id=account_id, label=display_name or account_id)
            profile = BrokerProfileTemplate.from_mapping({**source.to_mapping(), "profile_id": account_id})
            profile.environment = env
            profile.label = profile.label or display_name or account_id
            profile.password_env = profile.password_env or _DEFAULT_PASSWORD_ENV[env]
            profile.password_ref = profile.password_ref or ref_for_env(profile.password_env)
            if profile.auth_code_env:
                profile.auth_code_ref = profile.auth_code_ref or ref_for_env(profile.auth_code_env)
            self.profiles.append(profile)
            profile_id = account_id
        used_books = {item.book_id for item in self.accounts if item.book_id}
        if not book_id:
            book_id = _LEGACY_BOOKS.get(env, "") if channel == ta.CHANNEL_CTP else ""
            if not book_id or book_id in used_books:
                book_id = f"book_{account_id}"
        account = TradingAccount(
            account_id=account_id,
            display_name=str(display_name or "").strip() or account_id,
            broker=str(broker or "").strip(),
            channel=channel,
            environment=env,
            status=ta.normalize_status(status),
            profile_id=profile_id,
            investor_id=str(investor_id or "").strip(),
            book_id=book_id,
            max_order_volume=int(max_order_volume or 0),
            notes=str(notes or ""),
        )
        account.touch()
        self.accounts.append(account)
        return account

    def duplicate_account(
        self,
        account_id: str,
        display_name: str = "",
        investor_id: str = "",
        environment: str = "",
    ) -> TradingAccount:
        """以现有账号为模板复制一份；复制到真实环境必须换资金号。"""
        source = self.account(account_id)
        if source is None:
            raise KeyError(f"账号不存在: {account_id}")
        env = ta.normalize_environment(environment or source.environment)
        new_investor = str(investor_id or source.investor_id).strip()
        if env == ta.ENV_LIVE and source.environment == ta.ENV_SIM and new_investor == source.investor_id:
            raise ValueError("仿真账号复制到真实环境时，必须填写新的真实资金账号")
        template = self.profile(source.profile_id)
        return self.add_account(
            display_name or f"{source.display_name} 副本",
            env,
            new_investor,
            channel=source.channel,
            broker=source.broker,
            template=template,
            max_order_volume=source.max_order_volume,
            notes=source.notes,
        )

    def update_account(self, account_id: str, **changes: Any) -> TradingAccount:
        """改显示名/资金号/上限/备注；环境变更请显式传 environment。"""
        account = self.account(account_id)
        if account is None:
            raise KeyError(f"账号不存在: {account_id}")
        for key in ("display_name", "investor_id", "notes", "book_id", "risk_policy_id"):
            if key in changes:
                setattr(account, key, str(changes[key] or "").strip())
        if "max_order_volume" in changes:
            account.max_order_volume = int(changes["max_order_volume"] or 0)
        if "environment" in changes and changes["environment"]:
            env = ta.normalize_environment(changes["environment"])
            account.environment = env
            profile = self.profile(account.profile_id)
            if profile is not None:
                profile.environment = env
        account.touch()
        return account

    def set_status(self, account_id: str, status: str) -> TradingAccount:
        account = self.account(account_id)
        if account is None:
            raise KeyError(f"账号不存在: {account_id}")
        account.status = ta.normalize_status(status)
        account.touch()
        return account

    def remove_account(self, account_id: str, *, hard: bool = False) -> bool:
        """默认软删除（归档 + 备注）；硬删除只给测试/手工清理用。"""
        account = self.account(account_id)
        if account is None:
            return False
        if hard:
            self.accounts = [item for item in self.accounts if item.account_id != account.account_id]
            self.profiles = [item for item in self.profiles if item.profile_id != account.profile_id]
            return True
        account.status = ta.STATUS_ARCHIVED
        note = "已删除（保留账本）"
        if note not in account.notes:
            account.notes = f"{account.notes}；{note}" if account.notes else note
        account.touch()
        return True

    # —— 物化 / 回写 ——

    def materialize(
        self,
        cfg: dict[str, Any],
        legacy_secrets: dict[str, Any] | None = None,
        store: Any = None,
    ) -> list[str]:
        """
        把账号写进 cfg["trade"]["ctp"]["ctp_profiles"]。

        名字沿用 profile_id（迁移时就是旧档案名），所以档案下拉、登录、下单都不用改。
        返回给日志用的差异说明。
        """
        store = store or default_store()
        trade = cfg.setdefault("trade", {})
        if not isinstance(trade, dict):
            return []
        ctp = trade.setdefault("ctp", {})
        if not isinstance(ctp, dict):
            ctp = {}
            trade["ctp"] = ctp
        profiles = ctp.get("ctp_profiles")
        if not isinstance(profiles, dict):
            profiles = {}
            ctp["ctp_profiles"] = profiles
        notes: list[str] = []
        for account in self.ctp_accounts():
            profile = self.profile(account.profile_id)
            if profile is None:
                notes.append(f"{account.account_id} 找不到连接模板 {account.profile_id}，按旧配置处理")
                continue
            current = profiles.get(profile.profile_id)
            item = dict(current) if isinstance(current, dict) else {}
            before = (str(item.get("user_id") or ""), ta.normalize_environment(item.get("kind")))
            item.update(
                {
                    "label": profile.label or profile.profile_id,
                    "kind": account.environment,
                    "broker_id": profile.broker_id,
                    "td_front": _front_value(profile.td_fronts),
                    "md_front": _front_value(profile.md_fronts),
                    "app_id": profile.app_id or str(ctp.get("app_id") or ""),
                    "flow_dir": profile.flow_dir or str(ctp.get("flow_dir") or "data/ctp_flow"),
                    "password_env": profile.password_env
                    or _DEFAULT_PASSWORD_ENV[account.environment],
                    "auth_code_env": profile.auth_code_env,
                    "password_ref": profile.password_ref,
                    "auth_code_ref": profile.auth_code_ref,
                    "query_interval_sec": profile.query_interval_sec,
                    "login_timeout_sec": profile.login_timeout_sec,
                    "order_wait_sec": profile.order_wait_sec,
                    "cancel_wait_sec": profile.cancel_wait_sec,
                    "order_count_warn": profile.order_count_warn,
                    "cancel_count_warn": profile.cancel_count_warn,
                    "close_split_products": list(profile.close_split_products),
                    "price_tick": profile.price_tick,
                    "max_order_volume": account.max_order_volume or profile.max_order_volume,
                    "auto_cancel_on_timeout": profile.auto_cancel_on_timeout,
                    "user_id": account.investor_id,
                    "account_id": account.account_id,
                    "environment": account.environment,
                    "status": account.status,
                }
            )
            item.pop("td_fronts", None)
            item.pop("md_fronts", None)
            password = store.resolve(profile.password_ref, legacy_secrets) if profile.password_ref else ""
            if password:
                item["resolved_password"] = password
            auth_code = store.resolve(profile.auth_code_ref, legacy_secrets) if profile.auth_code_ref else ""
            if auth_code:
                item["resolved_auth_code"] = auth_code
            profiles[profile.profile_id] = item
            after = (account.investor_id, account.environment)
            if before != after and (before[0] or before[1] in ta.ENVIRONMENTS):
                notes.append(
                    f"注册表覆盖旧档案 {profile.profile_id}: 资金号 {before[0] or '空'}→{after[0] or '空'}，"
                    f"环境 {before[1]}→{after[1]}"
                )
        if notes:
            logger.info("账号注册表物化: {}", "；".join(notes))
        return notes

    def sync_from_cfg(self, cfg: dict[str, Any]) -> list[str]:
        """配置页保存后，把界面上的档案字段回写注册表，避免下次物化又盖回去。"""
        trade = cfg.get("trade") or {}
        ctp = (trade.get("ctp") or {}) if isinstance(trade, dict) else {}
        profiles = (ctp.get("ctp_profiles") or {}) if isinstance(ctp, dict) else {}
        notes: list[str] = []
        for account in self.ctp_accounts():
            item = profiles.get(account.profile_id)
            profile = self.profile(account.profile_id)
            if not isinstance(item, dict) or profile is None:
                continue
            investor = str(item.get("user_id") or "").strip()
            if investor and investor != account.investor_id:
                notes.append(f"{account.account_id}: 资金号 {account.investor_id or '空'}→{investor}")
                account.investor_id = investor
            kind = ta.normalize_environment(item.get("kind") or account.environment)
            if kind != account.environment:
                notes.append(f"{account.account_id}: 环境 {account.environment}→{kind}")
                account.environment = kind
                profile.environment = kind
            broker_id = str(item.get("broker_id") or "").strip()
            if broker_id:
                profile.broker_id = broker_id
            td = _split_fronts(item.get("td_front") or item.get("td_fronts"))
            md = _split_fronts(item.get("md_front") or item.get("md_fronts"))
            if td:
                profile.td_fronts = td
            if md:
                profile.md_fronts = md
            env_name = str(item.get("password_env") or profile.password_env).strip()
            if env_name and env_name != profile.password_env:
                profile.password_env = env_name
                profile.password_ref = ref_for_env(env_name)
            auth_env = str(item.get("auth_code_env") or "").strip()
            if auth_env != profile.auth_code_env:
                profile.auth_code_env = auth_env
                profile.auth_code_ref = ref_for_env(auth_env) if auth_env else ""
            try:
                limit = int(float(item.get("max_order_volume") or profile.max_order_volume))
            except (TypeError, ValueError):
                limit = profile.max_order_volume
            profile.max_order_volume = max(1, limit)
            account.touch()
        # GUI/模拟账号只同步资金号（同花顺登录号和真实资金号是同一个）
        account_block = cfg.get("account") or {}
        gui_user = str((account_block or {}).get("user_id") or "").strip()
        if gui_user:
            for account in self.accounts:
                if account.channel != ta.CHANNEL_CTP and gui_user != account.investor_id:
                    notes.append(f"{account.account_id}: 资金号 {account.investor_id or '空'}→{gui_user}")
                    account.investor_id = gui_user
                    account.touch()
        return notes


def migrate_from_cfg(cfg: dict[str, Any]) -> AccountRegistry:
    """把旧 ctp_profiles + account 块迁移成注册表（只在 accounts.yaml 缺失时调用）。"""
    registry = AccountRegistry()
    trade = cfg.get("trade") or {}
    ctp = (trade.get("ctp") or {}) if isinstance(trade, dict) else {}
    old_profiles = ctp.get("ctp_profiles") if isinstance(ctp, dict) else {}
    account_block = cfg.get("account") or {}
    broker = str((account_block or {}).get("broker") or "").strip()
    used_books: set[str] = set()

    for name, raw in (old_profiles or {}).items():
        if not isinstance(raw, dict):
            continue
        profile_id = str(name).strip().lower()
        if not profile_id:
            continue
        template = BrokerProfileTemplate.from_mapping({**raw, "profile_id": profile_id})
        template.broker = template.broker or broker
        template.app_id = template.app_id or str(ctp.get("app_id") or "")
        template.flow_dir = template.flow_dir or str(ctp.get("flow_dir") or "")
        # 档案没写 auth_code_env 时沿用公共值；都没有就按代码默认，不能悄悄变成"不需要认证码"。
        if "auth_code_env" not in raw:
            template.auth_code_env = str(ctp.get("auth_code_env") or "CTP_AUTH_CODE")
        template.password_env = template.password_env or _DEFAULT_PASSWORD_ENV[template.environment]
        template.password_ref = template.password_ref or ref_for_env(template.password_env)
        if template.auth_code_env:
            template.auth_code_ref = template.auth_code_ref or ref_for_env(template.auth_code_env)
        registry.profiles.append(template)

        investor = str(raw.get("user_id") or "").strip()
        account_id = ta.new_account_id(profile_id, [item.account_id for item in registry.accounts])
        book_id = _LEGACY_BOOKS.get(template.environment, f"book_{account_id}")
        if book_id in used_books:
            book_id = f"book_{account_id}"
        used_books.add(book_id)
        registry.accounts.append(
            TradingAccount(
                account_id=account_id,
                display_name=(
                    f"{template.label or profile_id}-{investor}" if investor else (template.label or profile_id)
                ),
                broker=broker,
                channel=ta.CHANNEL_CTP,
                environment=template.environment,
                status=ta.STATUS_ENABLED if investor else ta.STATUS_DISABLED,
                profile_id=profile_id,
                investor_id=investor,
                book_id=book_id,
                notes="" if investor else "迁移时没有资金账号，先停用，填好后启用",
            )
        )

    gui_user = str((account_block or {}).get("user_id") or "").strip()
    if gui_user:
        account_id = ta.new_account_id("gui", [item.account_id for item in registry.accounts])
        book_id = "live" if "live" not in used_books else f"book_{account_id}"
        used_books.add(book_id)
        registry.accounts.append(
            TradingAccount(
                account_id=account_id,
                display_name=f"GUI实盘-{gui_user}",
                broker=broker,
                channel=ta.CHANNEL_GUI,
                environment=ta.ENV_LIVE,
                status=ta.STATUS_ENABLED,
                investor_id=gui_user,
                book_id=book_id,
            )
        )
    for account in registry.accounts:
        account.touch()
    return registry


def ensure_registry(
    root: str | Path,
    cfg: dict[str, Any],
    legacy_secrets: dict[str, Any] | None = None,
) -> AccountRegistry | None:
    """
    读注册表；文件缺失时自动从旧配置迁移一次。

    迁移只在文件确实不存在时发生；文件坏了不覆盖（避免吞掉用户手改的内容）。
    """
    path = Path(root) / REGISTRY_REL
    if path.exists():
        return AccountRegistry.load(root)
    registry = migrate_from_cfg(cfg)
    if not registry.accounts and not registry.profiles:
        return None
    try:
        registry.save(root)
    except OSError as exc:
        logger.warning("账号注册表首次生成失败，继续用旧 ctp_profiles: {}", exc)
        return None
    _audit(
        root,
        "migrate",
        detail=f"从旧配置迁移 {len(registry.accounts)} 个账号 / {len(registry.profiles)} 个模板",
    )
    logger.info(
        "已生成账号注册表 {}：{} 个账号，{} 个连接模板",
        path,
        len(registry.accounts),
        len(registry.profiles),
    )
    return registry


def registry_disabled(cfg: dict[str, Any]) -> bool:
    """回退开关：trade.ctp.account_registry: false 时完全走旧 ctp_profiles。"""
    trade = cfg.get("trade") or {}
    ctp = (trade.get("ctp") or {}) if isinstance(trade, dict) else {}
    value = (ctp or {}).get("account_registry")
    if value is None:
        return False
    return str(value).strip().lower() in {"false", "off", "0", "no"}


def apply_account_registry(
    root: str | Path,
    cfg: dict[str, Any],
    legacy_secrets: dict[str, Any] | None = None,
) -> list[str]:
    """load_config 的接入点：注册表优先，任何异常都回退旧 ctp_profiles。"""
    if registry_disabled(cfg):
        return []
    try:
        registry = ensure_registry(root, cfg, legacy_secrets)
        if registry is None:
            return []
        return registry.materialize(cfg, legacy_secrets)
    except Exception as exc:  # noqa: BLE001 - 注册表坏不能拦住启动
        logger.warning("账号注册表物化失败，回退旧 ctp_profiles: {}", exc)
        return []


def sync_registry_from_cfg(root: str | Path, cfg: dict[str, Any]) -> list[str]:
    """配置页保存时调用：把档案字段回写注册表并落盘。"""
    registry = AccountRegistry.load(root)
    if registry is None:
        return []
    try:
        notes = registry.sync_from_cfg(cfg)
        if notes:
            registry.save(root)
            _audit(root, "sync", detail="；".join(notes))
            logger.info("账号注册表已随配置更新: {}", "；".join(notes))
        return notes
    except Exception as exc:  # noqa: BLE001
        logger.warning("账号注册表回写失败（配置本身已保存）: {}", exc)
        return []


def check_cfg_with_registry(root: str | Path, cfg: dict[str, Any]) -> list[str]:
    """保存前预检：把存档配置套进注册表后校验，不写盘。返回错误列表。"""
    registry = AccountRegistry.load(root)
    if registry is None:
        return []
    try:
        registry.sync_from_cfg(cfg)
        return registry.validate()
    except Exception as exc:  # noqa: BLE001
        logger.warning("账号注册表预检失败，跳过校验: {}", exc)
        return []


def validate_registry_file(root: str | Path) -> list[str]:
    """只校验磁盘上的注册表，给界面展示用。"""
    registry = AccountRegistry.load(root)
    return registry.validate() if registry is not None else []



def load_registry(root: str | Path) -> AccountRegistry | None:
    """界面读取注册表用；文件不存在返回 None。"""
    return AccountRegistry.load(root)
