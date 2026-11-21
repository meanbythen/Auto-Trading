"""
CTP 券商档案。

功能：
  把「用哪家期货公司、连哪个前置、用哪个账号」收进 trade.ctp_profiles。
  trade.ctp 里仍可写公共默认值，档案里的同名字段覆盖它。
  45.0.0 起只认 ctp_profiles：旧写法（trade.ctp.td_front / sim / live）不再读取，
  档案缺失或档案名写错时留空连接字段，交给登录阶段明确报错。

原理：这里只合并配置，不连柜台、不读密码文件。密码和认证码由 config_loader 按档案的
password_env / auth_code_env 解析后写回 resolved_*，本模块只负责按档案选值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.trader.ctp_limits import effective_max_order_volume as effective_max_order_volume

CTP_KIND_SIM = "sim"
CTP_KIND_LIVE = "live"

DEFAULT_PROFILE_NAME = "default"
DEFAULT_SIM_PASSWORD_ENV = "CTP_PASSWORD"
DEFAULT_LIVE_PASSWORD_ENV = "CTP_LIVE_PASSWORD"
DEFAULT_AUTH_CODE_ENV = "CTP_AUTH_CODE"

# 这些是档案小节，不参与「公共默认」的合并。
_PROFILE_SECTIONS = {"sim", "live", "ctp_profiles", "ctp_profile", "profiles"}
# 已删除的旧写法里的连接字段。残留时一律忽略，避免旧值偷偷接上柜台。
_LEGACY_CONNECTION_KEYS = {"td_front", "md_front", "broker_id", "user_id", "sim", "live"}
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
        from src.trader.ctp_fronts import collect_fronts

        kind = normalize_ctp_kind(box.get("kind") or box.get("account_kind"))
        products = box.get("close_split_products") or ["bu", "c"]
        td_list = collect_fronts(box.get("td_front"), box.get("td_fronts"))
        md_list = collect_fronts(box.get("md_front"), box.get("md_fronts"))
        return cls(
            name=name,
            label=str((data or {}).get("label") or name),
            kind=kind,
            broker_id=str(box.get("broker_id") or "").strip(),
            td_front=td_list[0] if td_list else "",
            md_front=md_list[0] if md_list else "",
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
    """配置页下拉用。还没建档时给一份 default，保存后落成正式档案。"""
    names = [str(name) for name in _profiles_box(ctp)]
    return names or [DEFAULT_PROFILE_NAME]


def profile_name_for_kind(ctp: dict[str, Any] | None, kind: Any) -> str:
    """按仿真/真实挑档案。当前选中的那份种类对得上就沿用，对不上再找第一份同类。"""
    raw = ctp or {}
    profiles = _profiles_box(raw)
    if not profiles:
        return DEFAULT_PROFILE_NAME
    wanted_kind = normalize_ctp_kind(kind)
    wanted = str(raw.get("ctp_profile") or "").strip()
    item = profiles.get(wanted)
    if isinstance(item, dict) and normalize_ctp_kind(item.get("kind")) == wanted_kind:
        return wanted
    # 写了不存在的档案名：不要偷偷换成另一家。
    if wanted and wanted not in profiles:
        return wanted
    for name, row in profiles.items():
        if isinstance(row, dict) and normalize_ctp_kind(row.get("kind")) == wanted_kind:
            return str(name)
    if wanted in profiles:
        return wanted
    return str(next(iter(profiles)))


def sim_profile_user_id(ctp: dict[str, Any] | None) -> str:
    """读仿真档案里的资金账号，例如华泰仿真 28077。没有档案时返回空。"""
    profiles = _profiles_box(ctp)
    if not profiles:
        return ""
    name = profile_name_for_kind(ctp, CTP_KIND_SIM)
    item = profiles.get(name)
    if not isinstance(item, dict):
        return ""
    return str(item.get("user_id") or "").strip()


def set_sim_profile_user_id(ctp: dict[str, Any] | None, user_id: str) -> str:
    """
    把仿真账号写进仿真档案，不改真实档案。

    原理：账号页是仿真号码的唯一入口；配置页「档案资金账号」仍可看当前档案，
    但保存时仿真档案以这里为准，避免真实资金号写进 28077 那份。
    """
    if not isinstance(ctp, dict):
        return ""
    profiles = ctp.get("ctp_profiles")
    if not isinstance(profiles, dict) or not profiles:
        return ""
    name = profile_name_for_kind(ctp, CTP_KIND_SIM)
    item = profiles.get(name)
    if not isinstance(item, dict):
        return name
    item["user_id"] = str(user_id or "").strip()
    return name


def selected_profile_name(ctp: dict[str, Any] | None) -> str:
    """
    当前用哪份档案。

    操作账是仿真就用仿真档案，是真实就用真实档案。
    不会因为配置里还写着 huatai_sim，就把真实资金号拿去连仿真前置。
    """
    raw = ctp or {}
    return profile_name_for_kind(raw, raw.get("account_kind"))


def _join_public(ctp: dict[str, Any]) -> dict[str, Any]:
    """trade.ctp 里的公共默认值，去掉档案小节、运行时密码和已删除的旧连接字段。"""
    return {
        key: value
        for key, value in ctp.items()
        if key not in _PROFILE_SECTIONS
        and key not in _RUNTIME_KEYS
        and key not in _LEGACY_CONNECTION_KEYS
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
    from src.trader.ctp_fronts import collect_fronts

    td_list = collect_fronts(data.get("td_front"), data.get("td_fronts"), base.get("td_front"))
    md_list = collect_fronts(data.get("md_front"), data.get("md_fronts"), base.get("md_front"))
    merged.update(
        {
            "kind": profile.kind,
            "account_kind": profile.kind,
            "profile": profile.name,
            "profile_label": profile.label,
            "broker_id": profile.broker_id,
            "td_front": td_list[0] if td_list else "",
            "md_front": md_list[0] if md_list else "",
            "td_fronts": td_list,
            "md_fronts": md_list,
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


def _resolve_missing(
    ctp: dict[str, Any],
    account: dict[str, Any],
    kind_override: Any = None,
    name: str = "",
) -> dict[str, Any]:
    """
    没有可用档案时的空连接字段。

    旧写法（trade.ctp.sim / live、顶层前置）已删除，不再回退；
    这里只保留公共参数和账号种类，让登录因缺少前置而明确失败。
    """
    kind = normalize_ctp_kind(
        kind_override if kind_override is not None else ctp.get("account_kind")
    )
    merged = _join_public(ctp)
    merged.pop("kind", None)
    for key in ("td_front", "md_front", "broker_id", "user_id"):
        merged[key] = ""
    merged["resolved_password"] = str(
        (
            ctp.get("resolved_live_password")
            if kind == CTP_KIND_LIVE
            else ctp.get("resolved_sim_password")
        )
        or account.get("resolved_password")
        or ""
    ).strip()
    merged["resolved_auth_code"] = str(ctp.get("resolved_auth_code") or "").strip()
    merged["account_kind"] = kind
    merged["profile"] = ""
    merged["profile_label"] = ""
    merged["profile_missing"] = name or DEFAULT_PROFILE_NAME
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

    只认 trade.ctp_profiles；档案缺失或档案名写错时返回空连接字段，
    不再回退旧写法，避免连错柜台。
    """
    raw = dict(ctp or {})
    acc = account if isinstance(account, dict) else {}
    profiles = _profiles_box(raw)
    chosen = str(name or "").strip()
    if not chosen:
        chosen = profile_name_for_kind(raw, kind if kind is not None else raw.get("account_kind"))
    if not profiles:
        return _resolve_missing(raw, acc, kind_override=kind)
    data = profiles.get(chosen)
    if not isinstance(data, dict):
        return _resolve_missing(raw, acc, kind_override=kind, name=chosen)
    resolved = _resolve_named(raw, chosen, data, acc)
    if kind is not None:
        # 界面上的「CTP操作账」临时预览：只换账号种类，不改档案本身。
        return _preview_kind(resolved, raw, normalize_ctp_kind(kind), acc)
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
    resolved = resolve_ctp_profile(raw, account, kind=raw.get("account_kind"))
    notes: list[str] = []
    if not _profiles_box(raw):
        notes.append("还没有配置券商档案 ctp_profiles，请先在配置页建档")
    elif not str(resolved.get("profile") or "").strip():
        name = str(resolved.get("profile_missing") or "").strip()
        notes.append(f"券商档案 {name or '（未命名）'} 不存在，请重新选择")
    kind = normalize_ctp_kind(resolved.get("account_kind"))
    if kind == CTP_KIND_LIVE:
        if not str(resolved.get("td_front") or "").strip():
            notes.append("真实档案缺少交易前置")
        if not str(resolved.get("broker_id") or "").strip():
            notes.append("真实档案缺少 BrokerID")
    from src.trader.ctp_fronts import collect_fronts

    td_list = collect_fronts(resolved.get("td_front"), resolved.get("td_fronts"))
    md_list = collect_fronts(resolved.get("md_front"), resolved.get("md_fronts"))
    td_front = td_list[0] if td_list else ""
    md_front = md_list[0] if md_list else ""
    if td_front and md_front and td_front == md_front:
        notes.append("交易前置与行情前置相同，行情端口通常不是交易端口")
    if len(td_list) > 1:
        notes.append(f"交易前置共 {len(td_list)} 台，断线由柜台在已登记地址里换")
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
