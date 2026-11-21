"""
交易账号注册表的领域模型与纯函数校验。

功能：定义账号身份（account_id）、连接模板（profile）和仿真/真实环境约束。
原理：账号是身份，渠道（ctp/gui/sim）只是属性；环境是硬约束，保存前用纯函数校验。
      本模块不读写文件、不碰界面，方便单测，也方便以后原样搬到 SQLite。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Iterable

CHANNEL_CTP = "ctp"
CHANNEL_GUI = "gui"
CHANNEL_SIM = "sim"
CHANNELS = (CHANNEL_CTP, CHANNEL_GUI, CHANNEL_SIM)

ENV_SIM = "sim"
ENV_LIVE = "live"
ENVIRONMENTS = (ENV_SIM, ENV_LIVE)

STATUS_ENABLED = "enabled"
STATUS_DISABLED = "disabled"
STATUS_ARCHIVED = "archived"
STATUSES = (STATUS_ENABLED, STATUS_DISABLED, STATUS_ARCHIVED)

# 只属于仿真柜台的地址标记。真实账号误填这些前置时直接拦住，避免把真单发到仿真环境。
SIM_ONLY_MARKERS = ("simnow", "180.168.146.187", "59.36.3.115")

# 内置程序自测账号。local:// 前置不会交给真实 CTP SDK，只路由到本机独立仿真柜台。
SELF_TEST_ACCOUNT_ID = "acc_program_selftest"
SELF_TEST_CONNECTION_ID = "program_selftest"
SELF_TEST_FRONT = "local://program-selftest"
SELF_TEST_BOOK_ID = "self_test"
SELF_TEST_INVESTOR_ID = "SELFTEST"
SELF_TEST_LABEL = "程序自测仿真柜台"

_ACCOUNT_ID_RE = re.compile(r"^acc_[a-z0-9_]{2,64}$")
_PROFILE_ID_RE = re.compile(r"^[a-z0-9_]{2,64}$")

DEFAULT_RISK_POLICY = "default"


def normalize_environment(value: Any) -> str:
    """把 kind / 仿真 / 真实 这类写法收成 sim 或 live。"""
    text = str(value or "").strip().lower()
    if text in {"live", "real", "prod", "实盘", "真实", "真实账号"}:
        return ENV_LIVE
    return ENV_SIM


def normalize_status(value: Any) -> str:
    """状态认不出来时按停用处理，避免误把未知账号当启用。"""
    text = str(value or "").strip().lower()
    if text in STATUSES:
        return text
    return STATUS_DISABLED


def normalize_channel(value: Any) -> str:
    """渠道认不出来时按 ctp 处理（现有实盘链路只有 ctp）。"""
    text = str(value or "").strip().lower()
    if text in CHANNELS:
        return text
    return CHANNEL_CTP


def normalize_captcha_mode(value: Any) -> str:
    """GUI 登录验证码模式：只认 pause / skip，其余按 pause。"""
    text = str(value or "").strip().lower()
    return text if text in ("pause", "skip") else "pause"


def _split_fronts(value: Any) -> list[str]:
    """前置可能是字符串、分号/换行分隔的字符串或列表，统一成去空的列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value)
    for sep in (";", "\n", "；"):
        text = text.replace(sep, ",")
    return [item.strip() for item in text.split(",") if item.strip()]


def is_self_test_front(value: Any) -> bool:
    """是否为本程序自测专用前置；只认完整标记，避免普通地址被误路由。"""
    return str(value or "").strip().lower() == SELF_TEST_FRONT


def is_self_test_account(account: Any) -> bool:
    return str(getattr(account, "account_id", "") or "").strip().lower() == SELF_TEST_ACCOUNT_ID


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _bool(value: Any, default: bool = True) -> bool:
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


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class BrokerProfileTemplate:
    """一条柜台连接：期货公司接入点，可被多个账号复用；不含资金号和密码。"""

    profile_id: str
    label: str = ""
    broker: str = ""
    environment: str = ENV_SIM
    broker_id: str = ""
    td_fronts: list[str] = field(default_factory=list)
    md_fronts: list[str] = field(default_factory=list)
    app_id: str = ""
    flow_dir: str = ""
    password_env: str = ""
    auth_code_env: str = ""
    password_ref: str = ""
    auth_code_ref: str = ""
    query_interval_sec: float = 30.0
    login_timeout_sec: float = 20.0
    order_wait_sec: float = 8.0
    cancel_wait_sec: float = 3.0
    order_count_warn: int = 8
    cancel_count_warn: int = 8
    close_split_products: list[str] = field(default_factory=lambda: ["bu", "c"])
    price_tick: float = 1.0
    max_order_volume: int = 20
    auto_cancel_on_timeout: bool = True

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "BrokerProfileTemplate":
        """从 YAML / 旧 ctp_profiles 条目读一份模板；多余键忽略。"""
        box = dict(data or {})
        env = normalize_environment(box.get("environment") or box.get("kind"))
        return cls(
            profile_id=str(
                box.get("connection_id") or box.get("profile_id") or box.get("name") or ""
            ).strip().lower(),
            label=str(
                box.get("label")
                or box.get("connection_id")
                or box.get("profile_id")
                or ""
            ).strip(),
            broker=str(box.get("broker") or "").strip(),
            environment=env,
            broker_id=str(box.get("broker_id") or "").strip(),
            td_fronts=_split_fronts(box.get("td_fronts") or box.get("td_front")),
            md_fronts=_split_fronts(box.get("md_fronts") or box.get("md_front")),
            app_id=str(box.get("app_id") or "").strip(),
            flow_dir=str(box.get("flow_dir") or "").strip(),
            password_env=str(box.get("password_env") or "").strip(),
            auth_code_env=(
                "" if box.get("auth_code_env") == "" else str(box.get("auth_code_env") or "").strip()
            ),
            password_ref=str(box.get("password_ref") or "").strip(),
            auth_code_ref=str(box.get("auth_code_ref") or "").strip(),
            query_interval_sec=_float(box.get("query_interval_sec"), 30.0),
            login_timeout_sec=_float(box.get("login_timeout_sec"), 20.0),
            order_wait_sec=_float(box.get("order_wait_sec"), 8.0),
            cancel_wait_sec=_float(box.get("cancel_wait_sec"), 3.0),
            order_count_warn=_int(box.get("order_count_warn"), 8),
            cancel_count_warn=_int(box.get("cancel_count_warn"), 8),
            close_split_products=[
                str(item).lower() for item in (box.get("close_split_products") or ["bu", "c"])
            ],
            price_tick=_float(box.get("price_tick"), 1.0),
            max_order_volume=_int(box.get("max_order_volume"), 20),
            auto_cancel_on_timeout=_bool(box.get("auto_cancel_on_timeout"), True),
        )

    @property
    def connection_id(self) -> str:
        """v2 的叫法；内部仍沿用 profile_id，旧代码不用改。"""
        return self.profile_id

    @connection_id.setter
    def connection_id(self, value: str) -> None:
        self.profile_id = str(value or "")

    def to_mapping(self) -> dict[str, Any]:
        """写 YAML 用；连接条目统一写 connection_id。"""
        data = asdict(self)
        data["connection_id"] = data.pop("profile_id")
        data["td_fronts"] = list(self.td_fronts)
        data["md_fronts"] = list(self.md_fronts)
        data["close_split_products"] = list(self.close_split_products)
        return data


@dataclass
class TradingAccount:
    """一条账号。account_id 稳定，显示名可改；账本只跟着 account_id 走。"""

    account_id: str
    display_name: str = ""
    broker: str = ""
    channel: str = CHANNEL_CTP
    environment: str = ENV_SIM
    status: str = STATUS_ENABLED
    profile_id: str = ""
    investor_id: str = ""
    book_id: str = ""
    password_ref: str = ""
    auth_code_ref: str = ""
    captcha_mode: str = "pause"
    skip_auto_login: bool = True
    risk_policy_id: str = DEFAULT_RISK_POLICY
    max_order_volume: int = 0
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "TradingAccount":
        box = dict(data or {})
        return cls(
            account_id=str(box.get("account_id") or "").strip().lower(),
            display_name=str(box.get("display_name") or "").strip(),
            broker=str(box.get("broker") or "").strip(),
            channel=normalize_channel(box.get("channel")),
            environment=normalize_environment(box.get("environment")),
            status=normalize_status(box.get("status") or STATUS_ENABLED),
            profile_id=str(box.get("connection_id") or box.get("profile_id") or "").strip().lower(),
            investor_id=str(box.get("investor_id") or "").strip(),
            book_id=str(box.get("book_id") or "").strip(),
            password_ref=str(box.get("password_ref") or "").strip(),
            auth_code_ref=str(box.get("auth_code_ref") or "").strip(),
            captcha_mode=normalize_captcha_mode(box.get("captcha_mode")),
            skip_auto_login=_bool(box.get("skip_auto_login"), True),
            risk_policy_id=str(box.get("risk_policy_id") or DEFAULT_RISK_POLICY).strip(),
            max_order_volume=_int(box.get("max_order_volume"), 0),
            notes=str(box.get("notes") or ""),
            created_at=str(box.get("created_at") or _now()),
            updated_at=str(box.get("updated_at") or _now()),
        )

    @property
    def connection_id(self) -> str:
        return self.profile_id

    @connection_id.setter
    def connection_id(self, value: str) -> None:
        self.profile_id = str(value or "")

    def to_mapping(self) -> dict[str, Any]:
        data = asdict(self)
        data["connection_id"] = data.pop("profile_id")
        return data

    def touch(self) -> None:
        self.updated_at = _now()
        if not self.created_at:
            self.created_at = self.updated_at

    @property
    def enabled(self) -> bool:
        return self.status == STATUS_ENABLED


def new_account_id(base: str, existing: Iterable[str]) -> str:
    """生成稳定且不重复的 account_id，例如 acc_huatai_sim_02。"""
    slug = re.sub(r"[^a-z0-9]+", "_", str(base or "account").lower()).strip("_") or "account"
    if slug.startswith("acc_"):
        slug = slug[4:]
    used = {str(item) for item in existing}
    index = 1
    while True:
        candidate = f"acc_{slug}_{index:02d}"
        if candidate not in used:
            return candidate
        index += 1


def account_slug(account_id: str) -> str:
    """账号 → 文件名用的短名：去掉 acc_ 前缀，非法字符转下划线。"""
    slug = str(account_id or "").strip().lower()
    if slug.startswith("acc_"):
        slug = slug[4:]
    slug = re.sub(r"[^a-z0-9_]+", "_", slug).strip("_")
    return slug or "account"


def runtime_profile_key(account: TradingAccount, shared: bool = False) -> str:
    """账号在运行时 ctp_profiles 里的键。

    第一条使用该连接的账号沿用连接 id（旧代码和旧档案继续可用）；
    共用同一条连接的后续账号加后缀，避免资金号互相覆盖。
    """
    if not account.profile_id:
        return ""
    if not shared:
        return account.profile_id
    return f"{account.profile_id}__{account_slug(account.account_id)}"


def _profile_problems(profile: BrokerProfileTemplate) -> list[str]:
    problems: list[str] = []
    if not _PROFILE_ID_RE.match(profile.profile_id):
        problems.append(f"连接 id 不合法：{profile.profile_id or '（空）'}")
    if profile.environment not in ENVIRONMENTS:
        problems.append(f"连接 {profile.profile_id} 的环境不合法：{profile.environment}")
    return problems


def _sim_front_hits(profile: BrokerProfileTemplate) -> list[str]:
    hits: list[str] = []
    for front in (*profile.td_fronts, *profile.md_fronts):
        text = front.lower()
        if any(marker in text for marker in SIM_ONLY_MARKERS):
            hits.append(front)
    return hits


def validate_registry(
    accounts: Iterable[TradingAccount],
    profiles: Iterable[BrokerProfileTemplate],
) -> list[str]:
    """
    账号注册表的硬约束校验。返回中文错误列表；空列表表示可以保存。

    只做能确定的检查：格式、唯一性、环境与模板一致、真实账号不得用仿真前置、
    仿真账号不得填真实资金号。不拦截"没填前置"这类运行期自检。
    """
    problems: list[str] = []
    account_list = list(accounts)
    profile_list = list(profiles)
    profile_map = {item.profile_id: item for item in profile_list}

    for template in profile_list:
        problems.extend(_profile_problems(template))
    if len(profile_map) != len(profile_list):
        problems.append("连接 id 有重复")

    account_ids: set[str] = set()
    book_used: dict[str, str] = {}
    for account in account_list:
        label = account.display_name or account.account_id or "（未命名账号）"
        if not _ACCOUNT_ID_RE.match(account.account_id):
            problems.append(f"{label}：account_id 不合法（应形如 acc_huatai_sim_01）")
        if account.account_id in account_ids:
            problems.append(f"{label}：account_id 重复")
        account_ids.add(account.account_id)
        if account.channel not in CHANNELS:
            problems.append(f"{label}：渠道不合法 {account.channel}")
        if account.environment not in ENVIRONMENTS:
            problems.append(f"{label}：环境不合法 {account.environment}")
        if account.status not in STATUSES:
            problems.append(f"{label}：状态不合法 {account.status}")
        if account.book_id:
            if account.book_id in book_used:
                problems.append(
                    f"{label}：账本 {account.book_id} 与 {book_used[account.book_id]} 重复（一个账号一本账）"
                )
            book_used.setdefault(account.book_id, label)
        if (
            account.channel == CHANNEL_GUI
            and account.status == STATUS_ENABLED
            and not account.investor_id
        ):
            problems.append(f"{label}：启用中的 GUI 账号必须填资金账号")
        if account.channel != CHANNEL_CTP:
            continue
        profile = profile_map.get(account.profile_id)
        self_test = is_self_test_account(account)
        local_front = profile is not None and any(
            is_self_test_front(front) for front in (*profile.td_fronts, *profile.md_fronts)
        )
        if self_test:
            expected = (
                account.channel == CHANNEL_CTP
                and account.environment == ENV_SIM
                and account.profile_id == SELF_TEST_CONNECTION_ID
                and account.investor_id == SELF_TEST_INVESTOR_ID
                and account.book_id == SELF_TEST_BOOK_ID
                and account.status == STATUS_ENABLED
            )
            if not expected:
                problems.append(f"{label}：内置程序自测账号的身份、环境、连接和账本不可修改")
            if profile is None or profile.td_fronts != [SELF_TEST_FRONT] or profile.md_fronts != [SELF_TEST_FRONT]:
                problems.append(f"{label}：必须使用专用本地前置 {SELF_TEST_FRONT}")
        elif local_front:
            problems.append(f"{label}：本地自测前置只能用于内置程序自测账号，真实及普通账号禁止使用")
        if profile is None:
            problems.append(f"{label}：找不到连接 {account.profile_id or '（空）'}")
        else:
            if profile.environment != account.environment:
                problems.append(
                    f"{label}：账号环境是 {account.environment}，连接 {profile.profile_id} 却是 {profile.environment}"
                )
            if account.environment == ENV_LIVE:
                hits = _sim_front_hits(profile)
                if hits:
                    problems.append(
                        f"{label}：真实账号不得使用仿真前置 {hits[0]}"
                        f"（命中 {', '.join(SIM_ONLY_MARKERS)}）"
                    )
        if account.status == STATUS_ENABLED:
            if not account.profile_id:
                problems.append(f"{label}：启用中的 CTP 账号必须绑定连接")
            if not account.investor_id:
                problems.append(f"{label}：启用中的 CTP 账号必须填资金账号")
            if not self_test and not (
                account.password_ref
                or (profile is not None and (profile.password_ref or profile.password_env))
            ):
                problems.append(f"{label}：启用中的 CTP 账号必须保存密码")

    live_ids = {
        item.investor_id
        for item in account_list
        if item.environment == ENV_LIVE and item.investor_id
    }
    for account in account_list:
        if account.channel != CHANNEL_CTP or account.environment != ENV_SIM:
            continue
        if account.investor_id and account.investor_id in live_ids:
            problems.append(
                f"{account.display_name or account.account_id}：仿真账号填了真实资金号 "
                f"{account.investor_id}，请改成仿真号码"
            )
    return problems
