"""
CTP 上线四步。

功能：手工仿真 → 手工真实账户 → 策略自动仿真 → 策略自动真实账户。
原理：四步都调用同一套 CtpTrader。只换「人点还是策略点」和「仿真账还是真实账」。
前一步没勾完成，不允许跳到真实资金自动下单。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from src.trader.base import capabilities_for
from src.trader.ctp_logic import CTP_KIND_LIVE, CTP_KIND_SIM, normalize_ctp_kind

STAGE_DESK_SIM = "desk_sim"
STAGE_DESK_LIVE = "desk_live"
STAGE_AUTO_SIM = "auto_sim"
STAGE_AUTO_LIVE = "auto_live"

STAGE_ORDER = (STAGE_DESK_SIM, STAGE_DESK_LIVE, STAGE_AUTO_SIM, STAGE_AUTO_LIVE)

STAGE_LABELS = {
    STAGE_DESK_SIM: "① 手工仿真（华泰外接测试）",
    STAGE_DESK_LIVE: "② 手工真实账户",
    STAGE_AUTO_SIM: "③ 策略自动仿真",
    STAGE_AUTO_LIVE: "④ 策略自动真实账户",
}

STAGE_HINTS = {
    STAGE_DESK_SIM: "交易台只给人手工测开、平、撤、反手。这一步点「启动常驻」也不会让策略向柜台下单。",
    STAGE_DESK_LIVE: "同一套交易台连真实资金，再手工开平撤一笔。策略仍不会自动下单。",
    STAGE_AUTO_SIM: "测完后点「启动常驻」：策略自动出信号，下单走和交易台同一套 CTP（仿真）。挂机时请先关掉交易台登录，避免同一账号被踢。",
    STAGE_AUTO_LIVE: "点「启动常驻」后策略对真实资金自动下单。接口与交易台相同。挂机时不要再开交易台登录。",
}

# 华泰指引：除重复报单、批量撤单为选测外，其余必测。
HUATAI_REQUIRED = (
    ("adapt_conn", "1 连通性：认证登录，日志能看到账号和 AppID"),
    ("adapt_trade", "2 基础交易：开仓、平仓、撤单都能完成并收到回报"),
    ("mon_conn", "3 连接状态：能显示成功、断开、重连"),
    ("mon_count", "4 报撤单笔数：窗口能看到报单笔数和撤单笔数"),
    ("thresh", "6 阈值预警：报单/撤单到 8 笔时日志有提醒"),
    ("instr", "7 指令检查：错误合约、错误价位、超手数被本机拦住"),
    ("errmsg", "8 错误提示：资金不足、持仓不足、市场状态由柜台退回并显示"),
    ("pause", "9 暂停交易：点暂停后不再向柜台报单"),
    ("log", "11 日志：交易、登录、监测、错误都写进 logs 文件夹"),
    ("stable", "12 连续运行满 10 分钟，没有崩溃"),
    ("penetrate", "13 看穿式：测试机 IP 和 MAC 已填进华泰报告"),
)


def normalize_stage(value: Any) -> str:
    """把界面中文名或配置值收成四个阶段之一。"""
    text = str(value or "").strip()
    if text in STAGE_ORDER:
        return text
    for key, label in STAGE_LABELS.items():
        if text == label or text.endswith(label[2:]):
            return key
    compact = text.replace(" ", "")
    if "④" in compact or "策略自动真实" in compact:
        return STAGE_AUTO_LIVE
    if "③" in compact or "策略自动仿真" in compact:
        return STAGE_AUTO_SIM
    if "②" in compact or "手工真实" in compact:
        return STAGE_DESK_LIVE
    return STAGE_DESK_SIM


def stage_label(value: Any) -> str:
    return STAGE_LABELS[normalize_stage(value)]


def stage_hint(value: Any) -> str:
    return STAGE_HINTS[normalize_stage(value)]


def account_kind_for_stage(value: Any) -> str:
    """这一步该用仿真账还是真实账。"""
    stage = normalize_stage(value)
    if stage in {STAGE_DESK_LIVE, STAGE_AUTO_LIVE}:
        return CTP_KIND_LIVE
    return CTP_KIND_SIM


def allows_strategy_orders(value: Any) -> bool:
    """只有③④才允许策略向柜台发单。①②只用交易台。"""
    return normalize_stage(value) in {STAGE_AUTO_SIM, STAGE_AUTO_LIVE}


def uses_live_account(value: Any) -> bool:
    return account_kind_for_stage(value) == CTP_KIND_LIVE


def go_live_path(root: Path | str) -> Path:
    return Path(root) / "data" / "go_live.json"


def empty_state() -> dict[str, Any]:
    return {
        "stage": STAGE_DESK_SIM,
        "passed": {key: False for key in STAGE_ORDER},
        "huatai": {key: False for key, _title in HUATAI_REQUIRED},
    }


def load_state(root: Path | str) -> dict[str, Any]:
    """读本机上线进度。没有文件就从第一步开始。"""
    path = go_live_path(root)
    state = empty_state()
    if not path.exists():
        return state
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取上线进度失败，按第一步开始: {}", exc)
        return state
    if not isinstance(raw, dict):
        return state
    state["stage"] = normalize_stage(raw.get("stage"))
    passed = raw.get("passed") if isinstance(raw.get("passed"), dict) else {}
    for key in STAGE_ORDER:
        state["passed"][key] = bool(passed.get(key))
    ticks = raw.get("huatai") if isinstance(raw.get("huatai"), dict) else {}
    for key, _title in HUATAI_REQUIRED:
        state["huatai"][key] = bool(ticks.get(key))
    return state


def save_state(root: Path | str, state: dict[str, Any]) -> Path:
    path = go_live_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "stage": normalize_stage(state.get("stage")),
        "passed": {key: bool((state.get("passed") or {}).get(key)) for key in STAGE_ORDER},
        "huatai": {key: bool((state.get("huatai") or {}).get(key)) for key, _title in HUATAI_REQUIRED},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def huatai_incomplete(state: dict[str, Any]) -> list[str]:
    """还没勾的华泰必测项，返回中文说明。"""
    ticks = state.get("huatai") or {}
    return [title for key, title in HUATAI_REQUIRED if not ticks.get(key)]


def current_stage_from_cfg(cfg: dict[str, Any] | None) -> str:
    trade = (cfg or {}).get("trade") or {}
    box = trade.get("go_live") if isinstance(trade.get("go_live"), dict) else {}
    if box.get("stage"):
        return normalize_stage(box.get("stage"))
    root = str((cfg or {}).get("_project_root") or "")
    if root:
        return normalize_stage(load_state(root).get("stage"))
    kind = normalize_ctp_kind(((trade.get("ctp") or {}).get("account_kind")))
    broker = capabilities_for(trade.get("executor") or "sim").orders_via_broker
    if broker and kind == CTP_KIND_LIVE:
        return STAGE_AUTO_LIVE
    if broker:
        return STAGE_AUTO_SIM
    if kind == CTP_KIND_LIVE:
        return STAGE_DESK_LIVE
    return STAGE_DESK_SIM


def apply_stage_to_cfg(cfg: dict[str, Any], stage: Any) -> dict[str, Any]:
    """
    把阶段写进配置：账号种类、执行通道跟阶段走。

    手工两步不改成自动下单。策略两步把执行通道改成 ctp，和交易台同一套接口。
    """
    chosen = normalize_stage(stage)
    trade = cfg.setdefault("trade", {})
    if not isinstance(trade, dict):
        trade = {}
        cfg["trade"] = trade
    ctp = trade.setdefault("ctp", {})
    if not isinstance(ctp, dict):
        ctp = {}
        trade["ctp"] = ctp
    live_box = trade.setdefault("go_live", {})
    if not isinstance(live_box, dict):
        live_box = {}
        trade["go_live"] = live_box
    live_box["stage"] = chosen
    ctp["account_kind"] = account_kind_for_stage(chosen)
    if allows_strategy_orders(chosen):
        trade["executor"] = "ctp"
    return cfg


def why_cannot_enter(state: dict[str, Any], target: Any) -> str:
    """
    为什么还不能进入这一步。空字符串表示可以进入。

    ①随时可进。②要先勾完华泰必测。③也要先过华泰。④要先手工真实账户和策略仿真都完成。
    """
    chosen = normalize_stage(target)
    passed = state.get("passed") or {}
    if chosen == STAGE_DESK_SIM:
        return ""
    if not passed.get(STAGE_DESK_SIM):
        missing = huatai_incomplete(state)
        extra = "还差：" + "；".join(missing[:3]) + ("…" if len(missing) > 3 else "")
        if missing:
            return "请先在交易台做完华泰程序化外接必测，并勾选完成。" + extra
        return "请先勾选「① 手工仿真」已完成。"
    if chosen == STAGE_DESK_LIVE:
        return ""
    if chosen == STAGE_AUTO_SIM:
        return ""
    if chosen == STAGE_AUTO_LIVE:
        if not passed.get(STAGE_DESK_LIVE):
            return "请先用交易台在真实资金账号上手工开平撤一次，再让策略接管。"
        if not passed.get(STAGE_AUTO_SIM):
            return "请先让策略在仿真账号上自动跑通，再接管真实资金账户。"
    return ""


def mark_stage_passed(state: dict[str, Any], stage: Any) -> tuple[bool, str]:
    """勾选当前阶段完成。①还要华泰必测全部勾上。"""
    chosen = normalize_stage(stage)
    if chosen == STAGE_DESK_SIM:
        missing = huatai_incomplete(state)
        if missing:
            return False, "华泰必测还没勾完：" + "；".join(missing[:4]) + ("…" if len(missing) > 4 else "")
    blocked = why_cannot_enter(state, chosen)
    if blocked and chosen != STAGE_DESK_SIM:
        # 标记当前步完成前，当前步本身应当已经能进入。
        pass
    state.setdefault("passed", {})[chosen] = True
    return True, STAGE_LABELS[chosen] + "已勾完成。"


def next_stage(stage: Any) -> str | None:
    chosen = normalize_stage(stage)
    try:
        index = STAGE_ORDER.index(chosen)
    except ValueError:
        return None
    if index + 1 >= len(STAGE_ORDER):
        return None
    return STAGE_ORDER[index + 1]


def strategy_order_block_reason(cfg: dict[str, Any] | None) -> str:
    """策略准备向柜台发单时，若还在手工阶段，返回拦下的原因。"""
    stage = current_stage_from_cfg(cfg)
    if allows_strategy_orders(stage):
        return ""
    return (
        f"当前上线阶段是{stage_label(stage)}。"
        "这一步只用交易台手工下单，策略不会向柜台发单。"
        "要让策略自动下单，请把上线阶段改到③或④。"
    )
