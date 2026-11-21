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
import shutil
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
# 审计文件只保留一代：超过这个大小先改名为 .1 再继续写。
AUDIT_MAX_BYTES = 5 * 1024 * 1024
# 迁移后内容完全相同的旧账本移到这里，宁可多留一份也不删除。
LEGACY_ARCHIVE_REL = Path("data") / "archive" / "legacy_books"
_ARCHIVE_KEYS = ("book_name", "state_name", "intent_name")
# v2：连接与账号分离。连接不含资金号和密码，一条连接可被多个账号引用。
REGISTRY_VERSION = 2

# 迁移时沿用现有两本 CTP 账，保证账本文件不用搬家。
_LEGACY_BOOKS = {ta.ENV_SIM: "ctp_sim", ta.ENV_LIVE: "ctp_live"}
_DEFAULT_PASSWORD_ENV = {ta.ENV_SIM: "CTP_PASSWORD", ta.ENV_LIVE: "CTP_LIVE_PASSWORD"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _rotate_if_needed(path: Path, max_bytes: int) -> None:
    """超过上限就把当前文件改名为 .1（覆盖旧 .1），保持只留一代。"""
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return
    except OSError:
        return
    backup = path.with_name(path.name + ".1")
    try:
        backup.unlink(missing_ok=True)
        path.replace(backup)
    except OSError as exc:
        logger.warning("轮转日志 {} 失败，继续追加: {}", path, exc)


def _audit(root: str | Path, action: str, **fields: Any) -> None:
    """账号相关操作留痕。只记引用和字段名，不记密钥值。"""
    row = {"ts": _now(), "action": action, **fields}
    path = Path(root) / AUDIT_REL
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path, AUDIT_MAX_BYTES)
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
        raw_connections = box.get("connections")
        if raw_connections is None:
            raw_connections = box.get("broker_profiles")
        profiles = [
            BrokerProfileTemplate.from_mapping(item)
            for item in (raw_connections or [])
            if isinstance(item, dict)
        ]
        return cls(
            accounts=accounts,
            profiles=profiles,
            version=int(box.get("version") or REGISTRY_VERSION),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "version": REGISTRY_VERSION,
            "connections": [item.to_mapping() for item in self.profiles],
            "accounts": [item.to_mapping() for item in self.accounts],
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
        from src.domain.accounts import invalidate_registry_cache

        invalidate_registry_cache(root)
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

    def accounts_for_connection(self, connection_id: str) -> list[TradingAccount]:
        key = str(connection_id or "").strip().lower()
        return [item for item in self.accounts if item.profile_id == key]

    def runtime_profile_key(self, account_id: str) -> str:
        """账号在运行时 ctp_profiles 里的键；与 materialize 的分配规则一致。"""
        account = self.account(account_id)
        if account is None or not account.profile_id:
            return ""
        siblings = self.accounts_for_connection(account.profile_id)
        shared = bool(siblings) and siblings[0].account_id != account.account_id
        return ta.runtime_profile_key(account, shared=shared)

    def validate(self) -> list[str]:
        problems = ta.validate_registry(self.accounts, self.profiles)
        # 光看 book_id 不够：book_a 与 acc_a 会派生同一个账本文件，也必须拦住。
        from src.domain.accounts import book_layout

        seen: dict[str, tuple[str, str]] = {}
        for account in self.accounts:
            if not account.book_id:
                continue
            label = account.display_name or account.account_id
            name = book_layout(account.book_id)["book_name"]
            previous = seen.get(name)
            if previous is not None and previous[0] != account.book_id:
                problems.append(
                    f"账本文件 {name} 被 {previous[1]} 与 {label} 共用（一个账号一本账）"
                )
            seen[name] = (account.book_id, label)
        return problems

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
        """新建一个账号。

        传入 template 表示复用已有连接（一条连接可被多个账号引用），不再复制连接；
        不传时才按账号新建一条连接。账本默认等于 account_id。
        """
        env = ta.normalize_environment(environment)
        channel = ta.normalize_channel(channel)
        base = f"{broker or 'ctp'}_{env}"
        account_id = ta.new_account_id(base, [item.account_id for item in self.accounts])
        status = status or (
            ta.STATUS_ENABLED if investor_id or channel != ta.CHANNEL_CTP else ta.STATUS_DISABLED
        )
        profile_id = ""
        password_ref = ""
        auth_code_ref = ""
        if channel == ta.CHANNEL_CTP:
            reusable = self.profile(template.profile_id) if template is not None else None
            if reusable is not None:
                # 复用已有连接：一条连接可被多个账号引用。
                profile_id = reusable.profile_id
            else:
                source = template or BrokerProfileTemplate(
                    profile_id=account_id, label=display_name or account_id
                )
                profile = BrokerProfileTemplate.from_mapping(
                    {**source.to_mapping(), "connection_id": account_id}
                )
                profile.environment = env
                profile.label = profile.label or display_name or account_id
                profile.password_env = profile.password_env or _DEFAULT_PASSWORD_ENV[env]
                profile.password_ref = profile.password_ref or ref_for_env(profile.password_env)
                if profile.auth_code_env:
                    profile.auth_code_ref = profile.auth_code_ref or ref_for_env(
                        profile.auth_code_env
                    )
                self.profiles.append(profile)
                profile_id = account_id
            connection = self.profile(profile_id)
            # 账号自己的密钥引用：同一连接下不同资金号各有各的密码。
            password_ref = f"secret://ctp/{env}/{account_id}/password"
            auth_code_ref = (
                f"secret://ctp/{env}/{account_id}/auth_code"
                if connection is not None and connection.auth_code_env
                else ""
            )
        if not book_id:
            book_id = account_id
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
            password_ref=password_ref,
            auth_code_ref=auth_code_ref,
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
        if ta.is_self_test_account(source):
            raise ValueError("内置程序自测账号是只读账号，不能复制")
        env = ta.normalize_environment(environment or source.environment)
        new_investor = str(investor_id or source.investor_id).strip()
        if env == ta.ENV_LIVE and source.environment == ta.ENV_SIM and new_investor == source.investor_id:
            raise ValueError("仿真账号复制到真实环境时，必须填写新的真实资金账号")
        template = self.profile(source.profile_id) if source.profile_id else None
        if template is not None and env != source.environment:
            # 换环境要单独一条连接，不能把仿真前置复用给真实账号。
            template = BrokerProfileTemplate.from_mapping(
                {**template.to_mapping(), "connection_id": ""}
            )
        account = self.add_account(
            display_name or f"{source.display_name} 副本",
            env,
            new_investor,
            channel=source.channel,
            broker=source.broker,
            template=template,
            max_order_volume=source.max_order_volume,
            notes=source.notes,
        )
        account.password_ref = source.password_ref or account.password_ref
        account.auth_code_ref = source.auth_code_ref or account.auth_code_ref
        account.captcha_mode = source.captcha_mode
        account.skip_auto_login = source.skip_auto_login
        return account

    def update_account(self, account_id: str, **changes: Any) -> TradingAccount:
        """改显示名/资金号/上限/备注；环境变更请显式传 environment。"""
        account = self.account(account_id)
        if account is None:
            raise KeyError(f"账号不存在: {account_id}")
        if ta.is_self_test_account(account):
            raise ValueError("内置程序自测账号是只读账号，不能修改")
        for key in (
            "display_name",
            "investor_id",
            "notes",
            "book_id",
            "risk_policy_id",
            "password_ref",
            "auth_code_ref",
            "connection_id",
        ):
            if key in changes:
                setattr(account, key, str(changes[key] or "").strip())
        if "max_order_volume" in changes:
            account.max_order_volume = int(changes["max_order_volume"] or 0)
        if "environment" in changes and changes["environment"]:
            env = ta.normalize_environment(changes["environment"])
            account.environment = env
            profile = self.profile(account.profile_id)
            if profile is not None and not self._profile_shared(account):
                profile.environment = env
        account.touch()
        return account

    def _profile_shared(self, account: TradingAccount) -> bool:
        """这条连接是否已被别的账号引用。共用连接不能跟着单个账号改环境。"""
        return any(
            item.account_id != account.account_id and item.profile_id == account.profile_id
            for item in self.accounts
        )

    def set_status(self, account_id: str, status: str) -> TradingAccount:
        account = self.account(account_id)
        if account is None:
            raise KeyError(f"账号不存在: {account_id}")
        if ta.is_self_test_account(account):
            raise ValueError("内置程序自测账号是只读账号，不能停用或归档")
        account.status = ta.normalize_status(status)
        account.touch()
        return account

    def remove_account(self, account_id: str, *, hard: bool = False) -> bool:
        """默认软删除（归档 + 备注）；硬删除只给测试/手工清理用。"""
        account = self.account(account_id)
        if account is None:
            return False
        if ta.is_self_test_account(account):
            raise ValueError("内置程序自测账号是只读账号，不能删除")
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

        第一条使用某条连接的账号沿用连接 id（旧档案、旧代码继续可用）；
        共用同一连接的后续账号写成「连接__账号」，资金号互不覆盖。
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
        first_for_connection: dict[str, str] = {}
        for account in self.ctp_accounts():
            profile = self.profile(account.profile_id)
            if profile is None:
                notes.append(f"{account.account_id} 找不到连接 {account.profile_id}，按旧配置处理")
                continue
            key = first_for_connection.setdefault(account.profile_id, account.account_id)
            shared = key != account.account_id
            runtime_key = ta.runtime_profile_key(account, shared=shared)
            current = profiles.get(runtime_key)
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
                    "password_ref": account.password_ref or profile.password_ref,
                    "auth_code_ref": account.auth_code_ref or profile.auth_code_ref,
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
                    "connection_id": profile.profile_id,
                    "environment": account.environment,
                    "status": account.status,
                }
            )
            item.pop("td_fronts", None)
            item.pop("md_fronts", None)
            password = ""
            for ref in (account.password_ref, profile.password_ref):
                if ref:
                    password = store.resolve(ref, legacy_secrets)
                    if password:
                        break
            if password:
                item["resolved_password"] = password
            auth_code = ""
            for ref in (account.auth_code_ref, profile.auth_code_ref):
                if ref:
                    auth_code = store.resolve(ref, legacy_secrets)
                    if auth_code:
                        break
            if auth_code:
                item["resolved_auth_code"] = auth_code
            profiles[runtime_key] = item
            after = (account.investor_id, account.environment)
            if before != after and (before[0] or before[1] in ta.ENVIRONMENTS):
                notes.append(
                    f"注册表覆盖旧档案 {runtime_key}: 资金号 {before[0] or '空'}→{after[0] or '空'}，"
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
            if ta.is_self_test_account(account):
                continue
            profile = self.profile(account.profile_id)
            base_item = profiles.get(account.profile_id)
            runtime_key = self.runtime_profile_key(account.account_id)
            item = profiles.get(runtime_key)
            if not isinstance(item, dict) or profile is None:
                item = base_item
            if not isinstance(item, dict) or profile is None:
                continue
            investor = str(item.get("user_id") or "").strip()
            if investor and investor != account.investor_id:
                notes.append(f"{account.account_id}: 资金号 {account.investor_id or '空'}→{investor}")
                account.investor_id = investor
            kind = ta.normalize_environment(item.get("kind") or account.environment)
            if kind != account.environment and not self._profile_shared(account):
                notes.append(f"{account.account_id}: 环境 {account.environment}→{kind}")
                account.environment = kind
                profile.environment = kind
            # 连接级字段只从基础键回写；共用连接时不要被第二个账号的表单覆盖。
            source = base_item if isinstance(base_item, dict) else item
            broker_id = str(source.get("broker_id") or "").strip()
            if broker_id:
                profile.broker_id = broker_id
            td = _split_fronts(source.get("td_front") or source.get("td_fronts"))
            md = _split_fronts(source.get("md_front") or source.get("md_fronts"))
            if td:
                profile.td_fronts = td
            if md:
                profile.md_fronts = md
            env_name = str(source.get("password_env") or profile.password_env).strip()
            if env_name and env_name != profile.password_env:
                profile.password_env = env_name
                profile.password_ref = ref_for_env(env_name)
            auth_env = str(source.get("auth_code_env") or "").strip()
            if auth_env != profile.auth_code_env:
                profile.auth_code_env = auth_env
                profile.auth_code_ref = ref_for_env(auth_env) if auth_env else ""
            if "max_order_volume" in item:
                try:
                    raw = item.get("max_order_volume")
                    account.max_order_volume = (
                        max(0, int(float(str(raw)))) if raw not in (None, "") else 0
                    )
                except (TypeError, ValueError):
                    pass
            account.touch()
        return notes


def one_book_per_account_enabled(cfg: dict[str, Any]) -> bool:
    """是否允许把旧四本共用账一次性迁成账号独立账。默认开启。"""
    trade = cfg.get("trade") or {}
    ctp = (trade.get("ctp") or {}) if isinstance(trade, dict) else {}
    value = (ctp or {}).get("one_book_per_account")
    if value is None:
        return True
    return str(value).strip().lower() not in {"false", "off", "0", "no"}


def _copy_ledger_path(source: Path, target: Path) -> bool:
    """源存在且目标不存在时复制（文件或目录）；不复用、不覆盖。"""
    if not source.exists() or target.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target)
    return True


def _repoint_active_book(
    root: Path, renamed: dict[str, str], registry: AccountRegistry
) -> None:
    """迁移后把 active_account.json 里的旧账本记号换成账号，避免启动仍落在旧文件。"""
    path = root / "data" / "active_account.json"
    if not path.exists() or not renamed:
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(data, dict):
        if str(data.get("account_id") or "").strip():
            return
        legacy = str(data.get("id") or "").strip().lower()
    else:
        legacy = str(data or "").strip().lower()
    target = renamed.get(legacy)
    if not target:
        return
    account = registry.account(target)
    payload = {
        "version": 2,
        "account_id": target,
        "label": (account.display_name if account is not None else target),
        "updated_at": _now(),
    }
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("更新当前账本指向失败（旧账本文件仍可读）: {}", exc)
        return
    _audit(root, "active_book_repoint", old_book=legacy, new_book=target)


def archive_duplicate_legacy_books(
    root: str | Path,
    registry: AccountRegistry,
) -> list[str]:
    """把与新账号账本内容完全一致的旧账本移入归档目录，不删除。

    只处理还在使用旧账本 id（ctp_sim / ctp_live / live / paper）的条目；
    内容有任何差异就保留原位，避免误伤仍在使用的历史账。
    """
    from src.domain.accounts import LEGACY_BOOK_IDS, book_layout

    base = Path(root)
    data_dir = base / "data"
    archive_dir = base / LEGACY_ARCHIVE_REL
    used = {str(item.book_id or "").strip().lower() for item in registry.accounts}
    targets: list[tuple[str, dict[str, str]]] = []
    for account in registry.accounts:
        book = str(account.book_id or "").strip().lower()
        if not book or book in LEGACY_BOOK_IDS or ta.is_self_test_account(account):
            continue
        targets.append((account.account_id, book_layout(book)))
    archived: list[str] = []
    for legacy in sorted(LEGACY_BOOK_IDS):
        if legacy in used:
            continue
        source_layout = book_layout(legacy)
        for key in _ARCHIVE_KEYS:
            src = data_dir / source_layout[key]
            if not src.exists():
                continue
            owner = ""
            for account_id, layout in targets:
                dst = data_dir / layout[key]
                if not dst.exists():
                    continue
                try:
                    if src.read_bytes() == dst.read_bytes():
                        owner = account_id
                        break
                except OSError:
                    continue
            if not owner:
                continue
            dest = archive_dir / src.name
            try:
                archive_dir.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    dest = archive_dir / f"{src.stem}.{datetime.now():%Y%m%d%H%M%S}{src.suffix}"
                shutil.move(str(src), str(dest))
            except OSError as exc:
                logger.warning("归档旧账本 {} 失败: {}", src, exc)
                continue
            _audit(
                base,
                "legacy_book_archive",
                account_id=owner,
                legacy_book=legacy,
                file=src.name,
                archive=dest.relative_to(base).as_posix(),
            )
            archived.append(f"{src.name}→{dest.name}")
    if archived:
        logger.info("旧账本归档（内容与账号账本一致）: {}", "；".join(archived))
    return archived


def migrate_legacy_books(
    root: str | Path,
    registry: AccountRegistry,
    *,
    enabled: bool = True,
) -> list[str]:
    """把共用旧账本一次性迁成账号独立账本。

    只复制、不删除旧文件，因此随时可以改回去；重复执行不会重复复制。
    返回中文说明列表；空列表表示没有账号需要迁移。
    """
    if not enabled:
        return []
    from src.domain.accounts import LEGACY_BOOK_IDS, book_layout

    base = Path(root)
    data_dir = base / "data"
    layout_of = {}
    owners: dict[str, list[str]] = {}
    for account in registry.accounts:
        if ta.is_self_test_account(account):
            continue
        layout = book_layout(account.book_id or account.account_id)
        layout_of[account.account_id] = layout
        owners.setdefault(layout["book_name"], []).append(account.account_id)

    def _needs_migration(account: TradingAccount) -> bool:
        if ta.is_self_test_account(account):
            return False
        book = str(account.book_id or "").strip().lower()
        if not book or book in LEGACY_BOOK_IDS:
            return True
        return len(owners.get(layout_of[account.account_id]["book_name"], [])) > 1

    notes: list[str] = []
    renamed: dict[str, str] = {}
    for account in registry.accounts:
        if not _needs_migration(account):
            continue
        old_book = str(account.book_id or "").strip().lower()
        new_book = account.account_id
        source = layout_of.get(account.account_id) or (
            book_layout(old_book) if old_book else None
        )
        target = book_layout(new_book)
        copied: list[str] = []
        skipped: list[str] = []
        if source is not None:
            for key in ("book_name", "state_name", "intent_name", "periods_dir"):
                src = data_dir / source[key]
                dst = data_dir / target[key]
                try:
                    if _copy_ledger_path(src, dst):
                        copied.append(target[key])
                    elif src.exists():
                        skipped.append(target[key])
                except OSError as exc:
                    logger.warning("迁移账本 {} → {} 失败: {}", src, dst, exc)
                    skipped.append(target[key])
            old_intents = data_dir / "intents" / old_book
            new_intents = data_dir / "intents" / new_book
            try:
                if _copy_ledger_path(old_intents, new_intents):
                    copied.append(f"intents/{new_book}")
                elif old_intents.exists():
                    skipped.append(f"intents/{new_book}")
            except OSError as exc:
                logger.warning("迁移委托意图 {} → {} 失败: {}", old_intents, new_intents, exc)
                skipped.append(f"intents/{new_book}")
        account.book_id = new_book
        account.touch()
        if old_book:
            renamed[old_book] = new_book
        _audit(
            base,
            "book_migrate",
            account_id=account.account_id,
            old_book=old_book,
            new_book=new_book,
            copied=copied,
            skipped=skipped,
        )
        notes.append(
            f"{account.account_id}: 账本 {old_book or '空'}→{new_book}，复制 {len(copied)} 项"
            + (f"，已存在跳过 {len(skipped)} 项" if skipped else "")
        )
    if notes:
        _repoint_active_book(base, renamed, registry)
        logger.info("账号账本一对一迁移: {}", "；".join(notes))
    # 迁移后旧文件若与新账本内容完全相同，移入归档目录；不同则保留，随时可回退。
    archive_duplicate_legacy_books(base, registry)
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
                password_ref=template.password_ref,
                auth_code_ref=template.auth_code_ref,
                notes="" if investor else "迁移时没有资金账号，先停用，填好后启用",
            )
        )

    gui_user = str((account_block or {}).get("user_id") or "").strip()
    if gui_user:
        account_id = ta.new_account_id("gui", [item.account_id for item in registry.accounts])
        book_id = "live" if "live" not in used_books else f"book_{account_id}"
        used_books.add(book_id)
        gui_password_env = str((account_block or {}).get("password_env") or "").strip()
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
                password_ref=ref_for_env(gui_password_env) if gui_password_env else "",
                captcha_mode=ta.normalize_captcha_mode((account_block or {}).get("captcha_mode")),
                skip_auto_login=bool((account_block or {}).get("skip_auto_login", True)),
            )
        )
    for account in registry.accounts:
        account.touch()
    return registry


def _backfill_gui_settings(path: Path, registry: AccountRegistry, cfg: dict[str, Any]) -> bool:
    """老 accounts.yaml 没有 GUI 登录参数时，从旧 account 块补一次。"""
    block = cfg.get("account") or {}
    if not isinstance(block, dict) or not block:
        return False
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return False
    boxes = raw.get("accounts") if isinstance(raw, dict) else None
    if not isinstance(boxes, list):
        return False
    raw_by_id = {
        str(box.get("account_id") or ""): box
        for box in boxes
        if isinstance(box, dict)
    }
    changed = False
    for account in registry.accounts:
        if account.channel != ta.CHANNEL_GUI:
            continue
        box = raw_by_id.get(account.account_id) or {}
        if "captcha_mode" not in box:
            account.captcha_mode = ta.normalize_captcha_mode(block.get("captcha_mode"))
            changed = True
        if "skip_auto_login" not in box:
            account.skip_auto_login = bool(block.get("skip_auto_login", True))
            changed = True
        if "password_ref" not in box and not account.password_ref:
            env_name = str(block.get("password_env") or "").strip()
            if env_name:
                account.password_ref = ref_for_env(env_name)
                changed = True
    return changed


def _ensure_self_test_account(registry: AccountRegistry) -> bool:
    """幂等补齐内置自测账号；不改已有账号，也不改变当前账号选择。"""
    changed = False
    if registry.profile(ta.SELF_TEST_CONNECTION_ID) is None:
        registry.profiles.append(
            BrokerProfileTemplate(
                profile_id=ta.SELF_TEST_CONNECTION_ID,
                label=ta.SELF_TEST_LABEL,
                broker="本机仿真柜台",
                environment=ta.ENV_SIM,
                broker_id="9999",
                td_fronts=[ta.SELF_TEST_FRONT],
                md_fronts=[ta.SELF_TEST_FRONT],
                app_id="client_AutoTrading_SelfTest",
                flow_dir="data/ctp_flow_self_test",
                password_env="",
                auth_code_env="",
            )
        )
        changed = True
    if registry.account(ta.SELF_TEST_ACCOUNT_ID) is None:
        account = TradingAccount(
            account_id=ta.SELF_TEST_ACCOUNT_ID,
            display_name=ta.SELF_TEST_LABEL,
            broker="本机仿真柜台",
            channel=ta.CHANNEL_CTP,
            environment=ta.ENV_SIM,
            status=ta.STATUS_ENABLED,
            profile_id=ta.SELF_TEST_CONNECTION_ID,
            investor_id=ta.SELF_TEST_INVESTOR_ID,
            book_id=ta.SELF_TEST_BOOK_ID,
            notes="内置只读账号；先在运行页测试工具启动仿真柜台，无需真实密码",
        )
        account.touch()
        registry.accounts.append(account)
        changed = True
    return changed


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
        registry = AccountRegistry.load(root)
        if registry is None:
            return None
        changed = _backfill_gui_settings(path, registry, cfg)
        changed = _ensure_self_test_account(registry) or changed
        if migrate_legacy_books(root, registry, enabled=one_book_per_account_enabled(cfg)):
            changed = True
        if changed:
            try:
                registry.save(root)
            except OSError as exc:
                logger.warning("账号注册表自动补齐失败，继续用文件里的值: {}", exc)
        return registry
    registry = migrate_from_cfg(cfg)
    if not registry.accounts and not registry.profiles:
        return None
    _ensure_self_test_account(registry)
    migrate_legacy_books(root, registry, enabled=one_book_per_account_enabled(cfg))
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
