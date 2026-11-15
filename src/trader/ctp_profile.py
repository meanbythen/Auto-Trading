"""
CTP 券商档案。

功能：
  把「用哪家期货公司、连哪个前置、用哪个账号」收进 trade.ctp_profiles。
  trade.ctp 里仍可写公共默认值，档案里的同名字段覆盖它。
  旧配置（trade.ctp.td_front / sim / live / account_kind）不改也能跑：
  加载时按旧键生成一份 default 档案，字段与升级前逐项一致。

原理：这里只合并配置，不连柜台、不读密码文件。密码和认证码由 config_loader 按档案的
password_env / auth_code_env 解析后写回 resolved_*，本模块只负责按档案选值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CTP_KIND_SIM = "sim"
CTP_KIND_LIVE = "live"

DEFAULT_PROFILE_NAME = "default"
DEFAULT_SIM_PASSWORD_ENV = "CTP_PASSWORD"
DEFAULT_LIVE_PASSWORD_ENV = "CTP_LIVE_PASSWORD"
DEFAULT_AUTH_CODE_ENV = "CTP_AUTH_CODE"

# 这些是档案小节，不参与「公共默认」的合并。
_PROFILE_SECTIONS = {"sim", "live", "ctp_profiles", "ctp_profile", "profiles"}
# 运行时字段。档案里只认 resolved_password / resolved_auth_code，旧的 sim/live 密码不沿用。
_RUNTIME_KEYS = {
    "resolved_password",
    "resolved_sim_password",
    "resolved_live_password",
    "resolved_auth_code",
}


def normalize_ctp_kind(value: Any) -> str:
    """把界面上的「仿真账号 / 真实账号」收成 sim 或 live。"""
    text = str(value or "").strip().lower()
    if text in {"live", "real", "prod", "实盘", "真实", "真实账号"}:
        return CTP_KIND_LIVE
    return CTP_KIND_SIM


def default_password_env(kind: Any) -> str:
    """档案没写 password_env 时按账号种类取默认环境变量名。"""
    if normalize_ctp_kind(kind) == CTP_KIND_LIVE:
        return DEFAULT_LIVE_PASSWORD_ENV
    return DEFAULT_SIM_PASSWORD_ENV


def _bool(value: Any, default: bool = True) -> bool:
    """配置里可能写 true，也可能写 "true"。认不出来就用默认值。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass
class BrokerProfile:
    """一份可切换的券商档案。字段与 trade.ctp 同名同义。"""

    name: str
    label: str = ""
    kind: str = CTP_KIND_SIM
    broker_id: str = ""
    td_front: str = ""
    md_front: str = ""
    user_id: str = ""
    password_env: str = ""
    app_id: str = ""
    auth_code_env: str = ""
    flow_dir: str = ""
    close_split_products: list[str] = field(default_factory=lambda: ["bu", "c"])
    price_tick: float = 1.0
    max_order_volume: int = 20
    order_wait_sec: float = 8.0
    query_interval_sec: float = 30.0
    login_timeout_sec: float = 20.0
    order_count_warn: int = 8
    cancel_count_warn: int = 8
    auto_cancel_on_timeout: bool = True
    cancel_wait_sec: float = 3.0

    @classmethod
    def from_mapping(
        cls,
        name: str,
        data: dict[str, Any] | None,
        base: dict[str, Any] | None = None,
    ) -> "BrokerProfile":
        """档案字段覆盖公共默认值。只取认识的字段，多余键原样忽略。"""
        box = dict(base or {})
        box.update(data or {})
        kind = normalize_ctp_kind(box.get("kind") or box.get("account_kind"))
        products = box.get("close_split_products") or ["bu", "c"]
        return cls(
            name=name,
            label=str((data or {}).get("label") or name),
            kind=kind,
            broker_id=str(box.get("broker_id") or "").strip(),
            td_front=str(box.get("td_front") or "").strip(),
            md_front=str(box.get("md_front") or "").strip(),
            user_id=str(box.get("user_id") or "").strip(),
            password_env=str(box.get("password_env") or default_password_env(kind)).strip(),
            app_id=str(box.get("app_id") or "").strip(),
            auth_code_env=(
                DEFAULT_AUTH_CODE_ENV
                if box.get("auth_code_env") is None
                else str(box.get("auth_code_env") or "").strip()
            ),
            flow_dir=str(box.get("flow_dir") or "").strip(),
            close_split_products=[str(item).lower() for item in products],
            price_tick=float(box.get("price_tick") or 1),
            max_order_volume=int(box.get("max_order_volume") or 20),
            order_wait_sec=float(box.get("order_wait_sec") or 8),
            query_interval_sec=float(box.get("query_interval_sec") or 30),
            login_timeout_sec=float(box.get("login_timeout_sec") or 20),
            order_count_warn=int(box.get("order_count_warn") or 8),
            cancel_count_warn=int(box.get("cancel_count_warn") or 8),
            auto_cancel_on_timeout=_bool(box.get("auto_cancel_on_timeout"), True),
            cancel_wait_sec=float(box.get("cancel_wait_sec") or 3),
        )


def _profiles_box(ctp: dict[str, Any] | None) -> dict[str, Any]:
    box = (ctp or {}).get("ctp_profiles")
    return box if isinstance(box, dict) else {}


def list_profiles(ctp: dict[str, Any] | None) -> list[str]:
    """配置页下拉用。没有新档案时给一份 default，避免下拉是空的。"""
    names = [str(name) for name in _profiles_box(ctp)]
    return names or [DEFAULT_PROFILE_NAME]


def selected_profile_name(ctp: dict[str, Any] | None) -> str:
    """
    当前用哪份档案。

    优先 trade.ctp_profile；没写时选与 account_kind 同类的第一份。
    都没有就交给旧配置生成 default。
    """
    raw = ctp or {}
    profiles = _profiles_box(raw)
    if not profiles:
        return DEFAULT_PROFILE_NAME
    wanted = str(raw.get("ctp_profile") or "").strip()
    if wanted in profiles:
        return wanted
    kind = normalize_ctp_kind(raw.get("account_kind"))
    for name, item in profiles.items():
        if isinstance(item, dict) and normalize_ctp_kind(item.get("kind")) == kind:
            return str(name)
    return str(next(iter(profiles)))


def _join_public(ctp: dict[str, Any]) -> dict[str, Any]:
    """trade.ctp 里的公共默认值，去掉档案小节和运行时密码。"""
    return {
        key: value
        for key, value in ctp.items()
        if key not in _PROFILE_SECTIONS and key not in _RUNTIME_KEYS
    }


def _apply_account(merged: dict[str, Any], kind: str, account: dict[str, Any]) -> None:
    """真实档案 user_id 留空时沿用配置页资金账号；密码同理。"""
    user_id = str(merged.get("user_id") or "").strip()
    if not user_id and kind == CTP_KIND_LIVE:
        user_id = str(account.get("user_id") or "").strip()
    merged["user_id"] = user_id
    if kind == CTP_KIND_LIVE and not str(merged.get("resolved_password") or "").strip():
        merged["resolved_password"] = str(account.get("resolved_password") or "").strip()


def _resolve_named(
    ctp: dict[str, Any],
    name: str,
    data: dict[str, Any],
    account: dict[str, Any],
) -> dict[str, Any]:
    base = _join_public(ctp)
    profile = BrokerProfile.from_mapping(name, data, base)
    merged = dict(base)
    merged.update({key: value for key, value in data.items() if key != "label"})
    merged.update(
        {
            "kind": profile.kind,
            "account_kind": profile.kind,
            "profile": profile.name,
            "profile_label": profile.label,
            "broker_id": profile.broker_id,
            "td_front": profile.td_front,
            "md_front": profile.md_front,
            "user_id": profile.user_id,
            "password_env": profile.password_env,
            "app_id": profile.app_id or str(base.get("app_id") or "").strip(),
            "auth_code_env": profile.auth_code_env,
            "flow_dir": profile.flow_dir or str(base.get("flow_dir") or "data/ctp_flow"),
            "close_split_products": profile.close_split_products,
            "price_tick": profile.price_tick,
            "max_order_volume": profile.max_order_volume,
            "order_wait_sec": profile.order_wait_sec,
            "query_interval_sec": profile.query_interval_sec,
            "login_timeout_sec": profile.login_timeout_sec,
            "order_count_warn": profile.order_count_warn,
            "cancel_count_warn": profile.cancel_count_warn,
            "auto_cancel_on_timeout": profile.auto_cancel_on_timeout,
            "cancel_wait_sec": profile.cancel_wait_sec,
            "resolved_password": str(data.get("resolved_password") or "").strip(),
            "resolved_auth_code": str(
                data.get("resolved_auth_code")
                if data.get("resolved_auth_code") is not None
                else base.get("resolved_auth_code")
                or ""
            ).strip(),
        }
    )
    _apply_account(merged, profile.kind, account)
    return merged


def _resolve_legacy(
    ctp: dict[str, Any],
    account: dict[str, Any],
    kind_override: Any = None,
) -> dict[str, Any]:
    """
    旧配置生成 default 档案。

    仿真：顶层参数 + sim 小节的非空字段，资金账号不参与。
    真实：前置只认 live 小节，资金账号取配置页那一号。
    """
    kind = normalize_ctp_kind(
        kind_override if kind_override is not None else ctp.get("account_kind")
    )
    nested = ctp.get(kind)
    nested = dict(nested) if isinstance(nested, dict) else {}
    merged = _join_public(ctp)
    merged.pop("kind", None)
    if kind == CTP_KIND_LIVE:
        merged["td_front"] = str(nested.get("td_front") or "").strip()
        merged["md_front"] = str(nested.get("md_front") or "").strip()
        merged["broker_id"] = str(
            nested.get("broker_id") or ctp.get("broker_id") or ""
        ).strip()
        merged["user_id"] = str(account.get("user_id") or "").strip() or str(
            nested.get("user_id") or ""
        ).strip()
        merged["resolved_password"] = str(
            ctp.get("resolved_live_password")
            or nested.get("resolved_password")
            or account.get("resolved_password")
            or ""
        ).strip()
    else:
        for key in ("td_front", "md_front", "user_id", "broker_id"):
            overlay = str(nested.get(key) or "").strip()
            if overlay:
                merged[key] = overlay
        merged["resolved_password"] = str(
            ctp.get("resolved_sim_password")
            or ctp.get("resolved_password")
            or nested.get("resolved_password")
            or ""
        ).strip()
    merged["resolved_auth_code"] = str(
        ctp.get("resolved_auth_code")
        or nested.get("resolved_auth_code")
        or ""
    ).strip()
    merged["account_kind"] = kind
    merged["profile"] = DEFAULT_PROFILE_NAME
    merged["profile_label"] = "默认档案"
    merged.setdefault("auth_code_env", DEFAULT_AUTH_CODE_ENV)
    merged.setdefault("password_env", default_password_env(kind))
    return merged


def resolve_ctp_profile(
    ctp: dict[str, Any] | None,
    account: dict[str, Any] | None = None,
    kind: Any = None,
    name: str | None = None,
) -> dict[str, Any]:
    """
    得到柜台连接实际使用的一份字段。

    ctp_profiles 存在时按档案合并；否则按旧键生成 default，行为与升级前一致。
    档案名写错时不猜别的券商，直接回到旧配置路径，避免连错柜台。
    """
    raw = dict(ctp or {})
    acc = account if isinstance(account, dict) else {}
    profiles = _profiles_box(raw)
    if not profiles:
        return _resolve_legacy(raw, acc, kind_override=kind)
    chosen = str(name or raw.get("ctp_profile") or "").strip()
    if not chosen:
        chosen = selected_profile_name(raw)
    data = profiles.get(chosen)
    if not isinstance(data, dict):
        return _resolve_legacy(raw, acc, kind_override=kind)
    resolved = _resolve_named(raw, chosen, data, acc)
    if kind is not None:
        # 界面上的「CTP操作账」临时预览：只换账号种类，不改档案本身。
        return _resolve_legacy(raw, acc, kind_override=kind) if not profiles else _preview_kind(
            resolved, raw, normalize_ctp_kind(kind), acc
        )
    return resolved


def _preview_kind(
    resolved: dict[str, Any],
    raw: dict[str, Any],
    kind: str,
    account: dict[str, Any],
) -> dict[str, Any]:
    """按指定账号种类再取一次档案；没有对应档案时保持当前档案。"""
    if resolved.get("account_kind") == kind:
        return resolved
    for name, item in _profiles_box(raw).items():
        if isinstance(item, dict) and normalize_ctp_kind(item.get("kind")) == kind:
            return _resolve_named(raw, str(name), item, account)
    return resolved


def profile_validation_notes(
    ctp: dict[str, Any] | None,
    account: dict[str, Any] | None = None,
) -> list[str]:
    """档案自检。只给提示，不拦住启动。"""
    raw = ctp or {}
    resolved = resolve_ctp_profile(raw, account)
    notes: list[str] = []
    kind = normalize_ctp_kind(resolved.get("account_kind"))
    if kind == CTP_KIND_LIVE:
        if not str(resolved.get("td_front") or "").strip():
            notes.append("真实档案缺少交易前置")
        if not str(resolved.get("broker_id") or "").strip():
            notes.append("真实档案缺少 BrokerID")
    td_front = str(resolved.get("td_front") or "").strip()
    md_front = str(resolved.get("md_front") or "").strip()
    if td_front and md_front and td_front == md_front:
        notes.append("交易前置与行情前置相同，行情端口通常不是交易端口")
    auth_env = str(resolved.get("auth_code_env") or "").strip()
    if auth_env and not str(resolved.get("resolved_auth_code") or "").strip():
        notes.append(f"环境变量 {auth_env} 没有读到认证码")
    if _profiles_box(raw):
        stage_kind = normalize_ctp_kind(raw.get("account_kind"))
        profile_kind = normalize_ctp_kind(resolved.get("account_kind"))
        if stage_kind != profile_kind:
            notes.append(
                "当前档案是"
                + ("真实账号" if profile_kind == CTP_KIND_LIVE else "仿真账号")
                + "，与上线阶段的账号种类不一致"
            )
    return notes
