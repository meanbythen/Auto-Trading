"""当前交易会话：账号 → 连接 → 执行通道 / 柜台 / 账本。

以前执行通道、CTP 操作账、券商档案、当前账本分散在四个开关里；本模块把它们
收成一个入口。运行时只认 data/active_account.json 里的账号。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from src.account.registry import AccountRegistry, load_registry
from src.domain import trading_account as ta
from src.domain.accounts import active_file, load_active_id, profile_of
from src.utils.secret_store import default_store


@dataclass(frozen=True)
class ResolvedSession:
    """一句话能说清的当前状态，界面直接展示。"""

    account_id: str
    display_name: str
    channel: str
    environment: str
    status: str
    connection_id: str
    profile_name: str
    book_id: str
    investor_id: str
    connection_label: str = ""

    @property
    def summary(self) -> str:
        env = "LIVE" if self.environment == ta.ENV_LIVE else "SIM"
        pieces = [self.display_name, env, self.channel.upper()]
        connection = self.connection_label or self.connection_id
        if connection:
            pieces.append(f"连接 {connection}")
        if self.book_id:
            pieces.append(f"账本 {self.book_id}")
        return " · ".join(piece for piece in pieces if piece)


def _find_account(registry: AccountRegistry, active: str) -> ta.TradingAccount | None:
    """active_account.json 里可能是 account_id（v2）或旧账本 id（v1）。"""
    account = registry.account(active)
    if account is not None:
        return account
    for item in registry.accounts:
        if item.book_id == active:
            return item
    return None


def active_account_id(root: str | Path) -> str:
    """当前使用账号的 account_id；没有注册表时返回空。"""
    registry = load_registry(Path(root))
    if registry is None:
        return ""
    account = _find_account(registry, load_active_id(root))
    return account.account_id if account else ""


def resolve_session(
    root: str | Path,
    cfg: dict[str, Any],
    secrets: dict[str, Any] | None = None,
    *,
    apply: bool = True,
    account_id: str = "",
) -> ResolvedSession | None:
    """按 active_account.json 解析执行通道、柜台和账本，并写回 cfg。

    注册表不存在、或当前记号对不上任何账号时返回 None，旧配置保持原样。
    account_id 非空时按指定账号预览，不改 active_account.json。
    """
    store = default_store()
    registry = load_registry(Path(root))
    if registry is None:
        return None
    active = str(account_id or "").strip() or load_active_id(root)
    account = _find_account(registry, active)
    if account is None:
        return None
    trade_before = cfg.get("trade") or {}
    ctp_before = trade_before.get("ctp") if isinstance(trade_before, dict) else None
    configured_max = 0
    if isinstance(ctp_before, dict):
        try:
            configured_max = int(float(str(ctp_before.get("max_order_volume") or 0)))
        except (TypeError, ValueError):
            configured_max = 0
    registry.materialize(cfg, secrets, store)
    profile_name = registry.runtime_profile_key(account.account_id)
    connection = registry.profile(account.profile_id) if account.profile_id else None
    session = ResolvedSession(
        account_id=account.account_id,
        display_name=account.display_name,
        channel=account.channel,
        environment=account.environment,
        status=account.status,
        connection_id=account.profile_id,
        profile_name=profile_name,
        book_id=account.book_id or account.account_id,
        investor_id=account.investor_id,
        connection_label=(connection.label if connection is not None else ""),
    )
    if not apply:
        return session
    trade = cfg.setdefault("trade", {})
    if not isinstance(trade, dict):
        return session
    trade["executor"] = account.channel
    if account.channel == ta.CHANNEL_CTP:
        ctp = trade.setdefault("ctp", {})
        if not isinstance(ctp, dict):
            ctp = {}
            trade["ctp"] = ctp
        ctp["account_kind"] = account.environment
        ctp["ctp_profile"] = profile_name
        ctp["account_id"] = account.account_id
        ctp["environment"] = account.environment
        if configured_max > 0:
            # 配置页的「单笔最大手数」是本机硬上限：账号/档案只能再往下压，
            # 不能盖回档案默认的 20，否则交易台 3 手会绕过本机 2 手的限制。
            from src.trader.ctp_limits import effective_max_order_volume

            profiles = ctp.get("ctp_profiles")
            item = profiles.get(profile_name) if isinstance(profiles, dict) else None
            profile_max = item.get("max_order_volume") if isinstance(item, dict) else 0
            ctp["max_order_volume"] = effective_max_order_volume(configured_max, profile_max)
    if account.channel == ta.CHANNEL_GUI:
        block = cfg.setdefault("account", {})
        if isinstance(block, dict):
            # GUI 账号是唯一数据源：资金号、密码、登录行为全部按当前账号覆盖。
            # 没存过密码的账号不能用其他账号留在 secret://gui/password 的旧密钥。
            ref = account.password_ref
            password = store.resolve(ref, secrets) if ref else ""
            block["user_id"] = account.investor_id
            block["resolved_password"] = password
            block["captcha_mode"] = account.captcha_mode
            block["skip_auto_login"] = account.skip_auto_login
            block["_from_active_account"] = True
            if account.broker:
                block["broker"] = account.broker
    return session


def apply_active_session(
    root: str | Path,
    cfg: dict[str, Any],
    secrets: dict[str, Any] | None = None,
) -> ResolvedSession | None:
    """配置加载时调用；只有真的选过账号（文件存在）才覆盖旧开关。"""
    path = Path(root) / "data" / "active_account.json"
    if not path.exists():
        return None
    try:
        return resolve_session(root, cfg, secrets)
    except Exception as exc:  # noqa: BLE001 - 选账号失败不能拖垮启动
        logger.warning("按当前账号解析运行配置失败，继续用旧配置: {}", exc)
        return None


def set_active_account(
    root: str | Path,
    cfg: dict[str, Any],
    account_id: str,
    secrets: dict[str, Any] | None = None,
) -> ResolvedSession:
    """切换当前账号；校验启用状态与连接完整性，然后一次切换所有开关。"""
    registry = load_registry(Path(root))
    if registry is None:
        raise ValueError("账号注册表未启用或读不出来")
    account = registry.account(account_id)
    if account is None:
        raise KeyError(f"账号不存在: {account_id}")
    if account.status != ta.STATUS_ENABLED:
        raise ValueError(f"{account.display_name} 当前是「{account.status}」，请先启用再切换")
    if account.channel == ta.CHANNEL_CTP:
        profile = registry.profile(account.profile_id)
        if profile is None:
            raise ValueError(f"{account.display_name} 没有绑定连接，请先在连接页选择")
        if not account.investor_id:
            raise ValueError(f"{account.display_name} 还没填资金账号")
        if not ta.is_self_test_account(account) and not (
            account.password_ref or profile.password_ref or profile.password_env
        ):
            raise ValueError(f"{account.display_name} 还没保存密码")
    if account.channel == ta.CHANNEL_GUI and not account.investor_id:
        raise ValueError(f"{account.display_name} 还没填资金账号")
    _write_active(root, account)
    session = resolve_session(root, cfg, secrets)
    assert session is not None  # 刚写进去的账号一定能解析
    logger.info("当前交易账号切换为 {}", session.summary)
    return session


def _write_active(root: str | Path, account: ta.TradingAccount) -> None:
    """active_account.json v2：只存 account_id，账本/柜台都由账号推导。"""
    path = active_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "account_id": account.account_id,
        "label": account.display_name,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def current_book_profile(root: str | Path) -> dict[str, str]:
    """当前账号对应的账本文件布局（给账户分析 / 运行页显示）。"""
    return profile_of(None, root=root)
