"""Shared UI labels, normalization helpers, and the thread-safe log adapter."""

from __future__ import annotations

from typing import Any

import customtkinter as ctk


ENGINE_LABELS = {
    "daily_body": "日内实体（主推）",
    "float_hold": "日内浮盈飘单",
    "trend_follow": "趋势跟随",
    "supertrend": "SuperTrend趋势",
    "st": "SuperTrend趋势",
    "dual_ma": "双均线趋势",
    "ma_cross": "双均线趋势",
    "ema_swing": "均线波段",
    "macd_swing": "均线波段",
    "bollinger": "布林带突破",
    "boll": "布林带突破",
    "sar_trend": "SAR波段趋势",
    "parabolic_sar": "SAR波段趋势",
    "multi_factor": "多因子CTA（加权打分）",
    "cta": "多因子CTA（加权打分）",
    "swing_trend": "长线趋势加减仓",
    "swing_box": "长线箱体突破",
}


ENGINE_CHOICES = [
    "multi_factor",
    "daily_body",
    "float_hold",
    "trend_follow",
    "supertrend",
    "dual_ma",
    "ema_swing",
    "bollinger",
    "sar_trend",
    "swing_trend",
    "swing_box",
]


_ENGINE_ALIASES = {
    "st": "supertrend",
    "ma_cross": "dual_ma",
    "boll": "bollinger",
    "bollinger_break": "bollinger",
    "sar": "sar_trend",
    "parabolic_sar": "sar_trend",
    "multi": "multi_factor",
    "cta": "multi_factor",
    "macd_swing": "ema_swing",
}


_REMOVED_ENGINES: set[str] = {
    "liquidity_exhaustion",
    "liquidity_signs",
    "macd_divergence",
    "macd_top_bottom",
    "macd_ths",
    "macd_zero",
    "macd",
    "reversal_pivot",
    "fvg",
    "intraday_breakout",
    "intraday",
    "pinbar_sar",
    "ema_pin_momentum",
    "ema_pin",
    "price_action",
    "pa_liquidity",
    "custom",
    "morning_gap_c",
    "morning_c",
    "sar_flip",
    "sar",
    "mean_reversion",
    "single_ma",
    "Single_ma",
    "ma20_trend",
    "ma20",
    "morning_structure_b",
    "morning_b",
    "morning_box_a",
    "morning_a",
    "jesse_casper",
    "casper",
    "jesse",
    "multi_factor_adapt",
    "multi_hybrid",
    "hybrid",
    "multi_vote",
    "vote",
    "daily_body_expand",
}


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


