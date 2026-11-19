"""Shared UI labels, normalization helpers, and the thread-safe log adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import customtkinter as ctk


from src.strategy.factory import (
    ENGINE_ALIASES,
    ENGINE_LABELS as _CANONICAL_ENGINE_LABELS,
    REGISTRY as _STRATEGY_REGISTRY,
    REMOVED_ENGINES,
)

# 引擎显示名：正式名 + 别名都指向同一个中文名，统一由策略工厂提供。
ENGINE_LABELS: dict[str, str] = {
    **dict(_CANONICAL_ENGINE_LABELS),
    **{
        alias: _CANONICAL_ENGINE_LABELS[target]
        for alias, target in ENGINE_ALIASES.items()
        if target in _CANONICAL_ENGINE_LABELS
    },
}

# 配置页下拉顺序：多因子放最前，其余按注册表顺序，别名不重复列。
_ALIAS_KEYS = set(ENGINE_ALIASES)
ENGINE_CHOICES: list[str] = [
    "multi_factor",
    *[key for key in _STRATEGY_REGISTRY if key not in _ALIAS_KEYS and key != "multi_factor"],
]

_ENGINE_ALIASES = dict(ENGINE_ALIASES)


_REMOVED_ENGINES: set[str] = set(REMOVED_ENGINES)


def register_rule_engines(directory: str | Path | None = None) -> list[str]:
    """
    把 config/rules/*.yaml 注册成可选策略：引擎代号 rule_dsl:<文件名>。

    程序启动时调一次；配置页保存/删除规则后再调一次，策略下拉框立刻能选到或去掉。
    重复调用不会重复添加；yaml 已被删除的自定义引擎会被剔出下拉。
    返回这次新加进去的代号。
    """
    from src.strategy.rule_dsl import RULE_ENGINE_PREFIX, list_rule_files, rule_engine_code

    existing = {rule_engine_code(info["stem"]): info for info in list_rule_files(directory)}
    # 先剔除已不存在的自定义规则引擎，保证删除后下拉框立即少一项。
    for code in [c for c in ENGINE_CHOICES if c.startswith(RULE_ENGINE_PREFIX)]:
        if code not in existing:
            ENGINE_CHOICES.remove(code)
            ENGINE_LABELS.pop(code, None)
    added: list[str] = []
    for info in existing.values():
        code = rule_engine_code(info["stem"])
        ENGINE_LABELS[code] = f"自定义：{info['name']}"
        if code not in ENGINE_CHOICES:
            ENGINE_CHOICES.append(code)
            added.append(code)
    return added


def _ui_engine(name: object) -> str:
    """下拉框只保留还在的策略。删掉的名字回到日内实体，避免界面打不开。"""
    text = str(name or "daily_body")
    text = _ENGINE_ALIASES.get(text, text)
    if text in _REMOVED_ENGINES:
        return "daily_body"
    if text not in ENGINE_LABELS and text not in ENGINE_CHOICES:
        return "daily_body"
    return text


def _eth_minutes_from_strategy(st: dict) -> int:
    """ETH 分钟数：优先 daily_body；旧配置若还写在 liquidity_exhaustion 里也能读到。默认与策略代码一致为 20。"""
    body = st.get("daily_body") or {}
    if "eth_minutes" in body:
        try:
            return max(0, int(body.get("eth_minutes", 20)))
        except (TypeError, ValueError):
            return 20
    le = st.get("liquidity_exhaustion") or {}
    try:
        return max(0, int(le.get("eth_minutes", 20)))
    except (TypeError, ValueError):
        return 20


EXEC_LABELS = {
    "sim": "模拟盘 sim",
    "gui": "同花顺点击 gui",
    "ctp": "CTP 柜台 ctp",
}


def _short_ctp_error(text: str) -> str:
    """把柜台报错收成卡片能放下的一句，不把整段日志塞进状态栏。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    if "认证码" in raw:
        return "密钥没读到"
    if "交易密码" in raw:
        return "缺交易密码"
    if "资金账号" in raw:
        return "缺资金账号"
    if "前置" in raw:
        return "缺交易前置"
    if len(raw) > 16:
        return raw[:16] + "…"
    return raw


def format_exec_card(
    executor: str,
    *,
    running: bool = False,
    daily_pnl: float | None = None,
    dry_run: bool = True,
    ctp_ready: bool = False,
    ctp_user: str = "",
    ctp_kind: str = "",
    ctp_error: str = "",
) -> str:
    """
    状态栏「执行通道」卡片文字。

    通道名不够：CTP 常驻连没连上必须一眼能看出来。
    交易台是点「连接登录」才连；常驻是点「启动常驻」后自己连同一套柜台。
    """
    name = EXEC_LABELS.get(executor, executor)
    pnl_txt = ""
    if running and daily_pnl is not None:
        pnl_txt = f"今日{daily_pnl:+.0f}元"
    if str(executor or "").strip().lower() != "ctp":
        if pnl_txt:
            return f"{name} · {pnl_txt}"
        return name
    kind = str(ctp_kind or "").strip().lower()
    kind_txt = "真实" if kind in {"live", "real", "prod", "真实", "真实账号"} else "仿真"
    if not running:
        return "CTP 柜台\n未挂机"
    if dry_run:
        lines = ["CTP 模拟", "不连柜台"]
        if pnl_txt:
            lines.append(pnl_txt)
        return "\n".join(lines)
    if ctp_ready:
        who = f"{kind_txt} {ctp_user}".strip() if str(ctp_user).strip() else kind_txt
        lines = ["CTP 已连接", who]
        if pnl_txt:
            lines.append(pnl_txt)
        return "\n".join(lines)
    err = _short_ctp_error(ctp_error) or "未登录"
    return f"CTP 未连接\n{err}"


def _clock_text(value: object, default: str) -> str:
    """保存钟点参数：能读成 HH:MM 就规整成两位，读不出来就用默认值。"""
    try:
        hour, minute = str(value or "").strip().split(":")
        hour_i, minute_i = int(hour), int(minute)
    except (TypeError, ValueError):
        return default
    if not (0 <= hour_i <= 23 and 0 <= minute_i <= 59):
        return default
    return f"{hour_i:02d}:{minute_i:02d}"


def _same_contract(left: object, right: object) -> bool:
    """两个写法是不是同一个合约。郑商所三位和四位年月写法算同一个。"""
    a = str(left or "").strip().lower()
    b = str(right or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    product_a = "".join(ch for ch in a if ch.isalpha())
    product_b = "".join(ch for ch in b if ch.isalpha())
    return product_a == product_b and a[-3:] == b[-3:]


def _duration_text(seconds: float) -> str:
    """秒数写成「12秒」「3分05秒」「1小时20分」，给进度提示和预计剩余时间用。"""
    sec = int(max(0.0, seconds))
    if sec < 60:
        return f"{sec}秒"
    minutes, sec = divmod(sec, 60)
    if minutes < 60:
        return f"{minutes}分{sec:02d}秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}小时{minutes:02d}分"


def bind_wrap_to_parent(label: Any, parent: Any = None, pad: int = 32, minimum: int = 160) -> None:
    """
    让长提示文字按面板的实际宽度折行，而不是写死一个 wraplength。

    写死时窗口拉宽它也不跟着变：明明一行放得下，却把句末的「。」单独挤到下一行。
    这里跟着面板走：够宽就排成一行，窗口变窄再折，折行宽度始终不超过面板。
    """
    box = parent if parent is not None else label.master

    def _apply(_event: object = None) -> None:
        available = box.winfo_width() - pad
        if available < minimum:
            return
        # CTk 把 wraplength 按窗口缩放换成屏幕像素，这里先按同一比例折回逻辑宽度。
        scale = float(ctk.ScalingTracker.get_window_scaling(box.winfo_toplevel()) or 1.0)
        width = int(available / max(scale, 0.5))
        if width >= minimum and width != label.cget("wraplength"):
            label.configure(wraplength=width)

    box.bind("<Configure>", _apply, add="+")
    label.after_idle(_apply)


class QueueLogHandler:
    """把 loguru 日志放进线程安全队列；界面在主线程里取，避免跨线程调用 Tk。"""

    def __init__(self, emit: Any) -> None:
        self._emit = emit

    def write(self, message: str) -> None:
        text = message.rstrip()
        if text:
            self._emit(text)


