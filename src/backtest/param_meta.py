"""优化参数元数据：内置网格、参数中文名与取值说明。

参数表、优化器和界面都读这一份；参数表不再反向依赖优化器。
"""

from __future__ import annotations

import copy
from typing import Any


# 趋势跟随只搜进场看多长、止损放几倍波动。出场通道保持更短的默认，避免和进场通道拧在一起。
GRID_TREND_LIGHT: dict[str, list[Any]] = {
    "entry_bars": [24, 36, 48],
}

GRID_TREND: dict[str, list[Any]] = {
    "entry_bars": [24, 36, 48],
    "atr_mult": [1.5, 2.0, 2.5],
}

# SuperTrend 只搜 ATR 根数和通道倍数。
GRID_ST_LIGHT: dict[str, list[Any]] = {
    "multiplier": [2.5, 3.0, 3.5],
}

GRID_ST: dict[str, list[Any]] = {
    "atr_period": [7, 10, 14],
    "multiplier": [2.5, 3.0, 3.5],
}

# 双均线：常用短/长搭配。
GRID_DUAL_MA_LIGHT: dict[str, list[Any]] = {
    "short_period": [10, 20],
    "long_period": [60, 120],
}
GRID_DUAL_MA: dict[str, list[Any]] = {
    "short_period": [10, 20, 30],
    "long_period": [60, 80, 120],
    "reverse_on_cross": [True, False],
    "atr_stop_mult": [1.5, 2.0, 3.0],
}

# 均线波段网格。想试 EMA(21)/EMA(122) 这类任意根数，改 config/优化参数.xlsx 或配置文件即可，不必改代码。
GRID_MACD_SWING_LIGHT: dict[str, list[Any]] = {
    "timeframe": ["5"],
    "dir_ema_pair": ["60/120"],
    "sig_ema_triple": ["5/10/30", "5/10/20"],
    "stop_ema": [120],
    "side_ema": [120],
    "slope_ema": [120],
    "slope_min_deg": [2.0, 3.0, 5.0],
    "slope_exit": [False, True],
}
GRID_MACD_SWING: dict[str, list[Any]] = {
    "timeframe": ["5", "15"],
    "dir_ema_pair": ["20/60", "60/120"],
    "sig_ema_triple": ["5/10/20", "5/10/30"],
    "stop_ema": [60, 120],
    "side_ema": [60, 120],
    "slope_ema": [60, 120],
    "slope_min_deg": [2.0, 3.0, 5.0, 8.0],
    "slope_lookback": [10, 20],
    "slope_exit": [False, True],
    "stop_mode": ["touch", "close"],
}

# 布林突破：轨道宽度和止损倍数。轨道周期只在常用档里比（20/26/30）。
GRID_BOLL_LIGHT: dict[str, list[Any]] = {
    "boll_window": [20, 30],
    "boll_dev": [2.0],
}
GRID_BOLL: dict[str, list[Any]] = {
    "boll_window": [20, 26, 30],
    "boll_dev": [1.8, 2.0, 2.5],
    "atr_stop_mult": [1.2, 1.5, 2.0],
}

# SAR 波段：只搜方向过滤的均线根数和加速步长，速度上限固定 0.2。
GRID_SAR_LIGHT: dict[str, list[Any]] = {
    "ma_filter": [20, 60],
}
GRID_SAR: dict[str, list[Any]] = {
    "ma_filter": [20, 40, 60],
    "af_step": [0.01, 0.02],
}

# 多因子 CTA：只搜阈值和权重结构相关的几个核心参数，不搜权重本身（容易过拟合）。
GRID_MULTI_LIGHT: dict[str, list[Any]] = {
    "threshold": [0.5, 0.6],
}
GRID_MULTI: dict[str, list[Any]] = {
    "threshold": [0.5, 0.6, 0.7],
    "ma_short": [10, 20],
    "ma_long": [40, 60],
    "channel": [20, 30],
    "atr_stop_mult": [1.5, 2.0, 2.5],
}

# 日内实体：只搜少数结构旋钮，避免又变成影线细则过拟合。
# 精简 24 组；标准 2592 组；完整约 398 万组（都再被「最多试几组」抽样）。
# 通用止损另由 _with_generic_stop 接到所有策略上，见下方公共旋钮。
# eth_minutes / eth_minutes2 写回 daily_body（和配置页「第一/第二ETH窗口」同一处）。
# timeframe 写回 strategy.timeframe；优化拉 K 线时用网格里最细的一档，策略内部再收成信号周期。
GRID_BODY_LIGHT: dict[str, list[Any]] = {
    "eth_minutes": [15, 20, 30],
    "timeframe": ["5", "15"],
    # 止损触发方式：触及即止损 / 收盘价确认。用来比较两种止损的差距。
    "stop_mode": ["touch", "close"],
}

GRID_BODY: dict[str, list[Any]] = {
    "eth_minutes": [15, 20, 30],
    # 第二ETH窗口：0=只用第一窗口；30=从开盘起另算 30 分钟箱子，过点后丢掉第一窗口。
    "eth_minutes2": [0, 30],
    "timeframe": ["5", "15"],
    # 开仓截止钟点：过点就不追，日盘、夜盘各搜三档（都对齐 5/15 分钟的收盘）。
    # 注意 10:15～10:30 是上午休盘：截止 10:15 和 10:30 会拦住同一批 K 线，等于同一组，
    # 所以日盘用 09:45 / 10:15 / 10:45 三档，避开这段休息。
    "day_cutoff": ["09:45", "10:15", "10:45"],
    "night_cutoff": ["22:00", "22:15", "22:30"],
    # 公共交易时段：写回 schedule 小节（和配置页「交易时段」同一处）。
    "session_scope": ["all", "day", "night"],
    # 止损触发方式：触及即止损 / 收盘价确认（写回 strategy 根，全策略共用）。
    "stop_mode": ["touch", "close"],
}

# 完整：把配置页里适合搜的项目都放进来。组合很多，靠「最多试几组」随机抽样。
GRID_BODY_FULL: dict[str, list[Any]] = {
    **GRID_BODY,
    # 箱沿止损再让几跳。
    "stop_extra_ticks": [1, 2],
    # 开盘跳空超过几倍开盘前 ATR 不开（0=关闭）。
    "skip_gap_atr_mult": [0, 2.5],
    # 箱体总宽超过几倍开盘前 ATR 不开（0=关闭）。
    "max_box_atr_mult": [5.0, 8.0],
    # 急破超过几倍开盘前 ATR 不追（0=关闭）。
    "max_chase_atr_mult": [0, 1.0],
    # 等回抽时看哪条 EMA（0=关闭），以及有效打穿要几跳。
    "ema_period": [20, 60],
    "ema_buffer_ticks": [2, 4],
}

# 通用止损：任何策略都能叠加的一层止损（配置页「止损设置 → 通用止损」同一处）。
# 精简只比关/SAR；标准四种都试；完整再把通用止损自己的参数放进来。
GENERIC_STOP_KIND_LIGHT: list[Any] = ["off", "sar"]
GENERIC_STOP_KIND: list[Any] = ["off", "sar", "prior_extreme", "chandelier"]
GENERIC_STOP_PARAMS: dict[str, list[Any]] = {
    # 前高低 / 吊灯的回看根数。
    "generic_stop.lookback": [10, 20, 40],
    # SAR 加速步长。
    "generic_stop.sar_af_step": [0.01, 0.02],
    # 吊灯止损的 ATR 根数和倍数。
    "generic_stop.atr_period": [14, 20],
    "generic_stop.atr_mult": [2.0, 3.0],
}

# 未知引擎：不搜参数，避免误改别的策略小节。
GRID_FIXED: dict[str, list[Any]] = {}

# 长线趋势：主要搜仓位门槛。信号/止损周期跟配置页，优化网格不另搜。
GRID_SWING_LIGHT: dict[str, list[Any]] = {
    "daily_ma": [20, 40],
    "add_min_atr": [0.8, 1.2],
    "reduce_extend_atr": [2.0, 3.0],
}
GRID_SWING: dict[str, list[Any]] = {
    "daily_ma": [20, 40, 60],
    "add_min_atr": [0.8, 1.0, 1.5],
    "reduce_extend_atr": [2.0, 2.5, 3.5],
    "max_adds": [1, 2],
}
GRID_DUAL_DONCHIAN_LIGHT: dict[str, list[Any]] = {
    "entry_bars": [20, 55],
    "atr_mult": [2.0, 2.5],
}
GRID_DUAL_DONCHIAN: dict[str, list[Any]] = {
    "entry_bars": [20, 55],
    "exit_bars": [10, 20],
    "atr_mult": [2.0, 2.5, 3.0],
    "adx_min": [18.0, 22.0],
}
GRID_DUAL_FRACTAL_LIGHT: dict[str, list[Any]] = {
    "trend_ema": [20, 40],
    "atr_mult": [2.5, 2.8],
}
GRID_DUAL_FRACTAL: dict[str, list[Any]] = {
    "trend_ema": [20, 40],
    "atr_mult": [2.5, 2.8, 3.5],
    "adx_min": [18.0, 22.0],
    "trend_mode": ["ema", "donchian"],
}
GRID_SWING_BOX_LIGHT: dict[str, list[Any]] = {
    "lookback": [30, 40],
    "adx_min": [18, 22],
}
GRID_SWING_BOX: dict[str, list[Any]] = {
    "lookback": [30, 40, 60],
    "height_atr": [1.0, 1.2],
    "trail_atr": [2.0, 2.5],
    "adx_min": [18, 20],
}


def _with_generic_stop(
    grid: dict[str, list[Any]],
    kinds: list[Any],
    *,
    full: bool = False,
) -> dict[str, list[Any]]:
    """把通用止损接到任一策略的网格上。返回深拷贝，不改原常量。"""
    out = copy.deepcopy(grid)
    out["generic_stop.kind"] = list(kinds)
    if full:
        out.update(copy.deepcopy(GENERIC_STOP_PARAMS))
    return out


def _depth_grid(
    light: dict[str, list[Any]],
    standard: dict[str, list[Any]],
    full: dict[str, list[Any]] | None,
    depth: str,
) -> dict[str, list[Any]]:
    """按深度挑网格，再统一接上通用止损。没有单独完整网格时，完整 = 标准 + 通用止损参数。"""
    if depth == "精简":
        return _with_generic_stop(light, GENERIC_STOP_KIND_LIGHT)
    if depth == "完整":
        return _with_generic_stop(full or standard, GENERIC_STOP_KIND, full=True)
    return _with_generic_stop(standard, GENERIC_STOP_KIND)


# 参数中文说明（界面展示用）。只保留现有策略还会搜到的项。
PARAM_LABELS_CN: dict[str, str] = {
    "eth_minutes": "第一ETH窗口（分钟）",
    "entry_bars": "趋势进场看前几根",
    "exit_bars": "趋势出场看前几根",
    "atr_period": "ATR用几根K线",
    "atr_mult": "趋势止损用几倍波动",
    "multiplier": "SuperTrend通道倍数",
    "short_period": "双均线短根数",
    "long_period": "双均线长根数",
    "reverse_on_cross": "均线反向时是否反手",
    "reverse_on_flip": "SuperTrend翻向时是否反手",
    "atr_stop_mult": "止损用几倍波动",
    "boll_window": "布林轨道周期",
    "boll_dev": "布林标准差倍数",
    "ma_filter": "SAR方向过滤均线根数",
    "af_step": "SAR加速步长",
    "threshold": "多因子总分阈值",
    "ma_short": "多因子短均线根数",
    "ma_long": "多因子长均线根数",
    "channel": "多因子唐奇安通道根数",
    "use_sar": "多因子是否启用SAR",
    "disagree_max": "多因子分歧上限",
    "timeframe": "信号周期（分钟）",
    "day_cutoff": "日盘开仓截止时间",
    "night_cutoff": "夜盘开仓截止时间",
    "session_scope": "交易时段",
    "stop_mode": "止损触发方式",
    "daily_ma": "长线日线均线根数",
    "add_min_atr": "长线加仓最少浮盈（倍波动）",
    "reduce_extend_atr": "长线伸展减仓（倍波动）",
    "max_adds": "长线最多加仓次数",
    "vol_mult": "放量倍数",
    "lookback": "长线箱子回看根数",
    "height_atr": "箱子最矮几倍ATR",
    "adx_min": "ADX下限",
    "trend_ema": "趋势过滤均线根数",
    "trend_mode": "趋势过滤方式（ema/donchian）",
    "trail_atr": "长线跟踪ATR倍数",
    "trail_start_h": "几倍箱高才跟踪",
    # 日内实体配置页上的结构旋钮。
    "eth_minutes2": "第二ETH窗口（分钟，0=不用）",
    "stop_extra_ticks": "箱沿止损外扩跳数",
    "skip_gap_atr_mult": "跳空超过几倍ATR不开（0=关闭）",
    "max_box_atr_mult": "箱体超过几倍ATR不开（0=关闭）",
    "max_chase_atr_mult": "急破超过几倍ATR不追（0=关闭）",
    "ema_period": "等回抽参考的EMA根数",
    "ema_buffer_ticks": "EMA 有效打穿跳数",
    "be_atr_mult": "浮盈几倍ATR后保本",
    "stop_atr_mult": "止损几倍开盘前ATR",
    "slope_min_deg": "最小倾角(度)",
    "slope_lookback": "斜率回看根数",
    "slope_exit": "倾角不够就平仓",
    "no_open_before_close_min": "尾盘不开仓(分钟)",
    "swing_side_bars": "高低点左右各看几根",
    "cluster_width": "快线聚合差价（已不用）",
    "gap_align_atr_mult": "跳空超过几倍ATR只顺向（0=关闭）",
    "macd_timeframe": "MACD周期（分钟）",
    "macd_combo": "MACD参数（快/慢/平滑）",
    "dir_ema_pair": "方向选择器（两根均线）",
    "sig_ema_triple": "信号发生器（三根均线）",
    "stop_ema": "止损均线根数",
    "side_ema": "多空分界线根数",
    "slope_ema": "倾角均线根数",
    "flatten_at_close": "收盘清仓",
    "direction": "方向选择器",
    # 通用止损（全策略公共设置，写回 strategy.generic_stop）。
    "generic_stop.kind": "通用止损类型",
    "generic_stop.lookback": "通用止损回看根数",
    "generic_stop.sar_af_step": "SAR 加速步长",
    "generic_stop.atr_period": "通用止损 ATR 根数",
    "generic_stop.atr_mult": "通用止损 ATR 倍数",
}


def default_grid_for_engine(engine: str, depth: str = "标准") -> dict[str, list[Any]]:
    """
    代码内置的默认网格（优化参数表缺行时回退用）。

    depth:
        精简 = 少参数、跑得快
        标准 = 含 Pinbar 定义等（推荐）
        完整 = 参数更全，组合更多，靠随机抽样控制次数
    """
    e = (engine or "daily_body").lower()
    d = (depth or "标准").strip()
    if e == "trend_follow":
        return _depth_grid(GRID_TREND_LIGHT, GRID_TREND, None, d)
    if e in ("supertrend", "st"):
        return _depth_grid(GRID_ST_LIGHT, GRID_ST, None, d)
    if e in ("dual_ma", "ma_cross"):
        return _depth_grid(GRID_DUAL_MA_LIGHT, GRID_DUAL_MA, None, d)
    if e in ("ema_swing", "macd_swing"):
        return _depth_grid(GRID_MACD_SWING_LIGHT, GRID_MACD_SWING, None, d)
    if e in ("bollinger", "boll"):
        return _depth_grid(GRID_BOLL_LIGHT, GRID_BOLL, None, d)
    if e in ("sar_trend", "parabolic_sar"):
        return _depth_grid(GRID_SAR_LIGHT, GRID_SAR, None, d)
    if e in ("multi_factor", "cta"):
        return _depth_grid(GRID_MULTI_LIGHT, GRID_MULTI, None, d)
    if e == "daily_body":
        return _depth_grid(GRID_BODY_LIGHT, GRID_BODY, GRID_BODY_FULL, d)
    if e == "swing_trend":
        return _depth_grid(GRID_SWING_LIGHT, GRID_SWING, None, d)
    if e == "swing_box":
        return _depth_grid(GRID_SWING_BOX_LIGHT, GRID_SWING_BOX, None, d)
    if e == "dual_donchian":
        return _depth_grid(GRID_DUAL_DONCHIAN_LIGHT, GRID_DUAL_DONCHIAN, None, d)
    if e == "dual_fractal":
        return _depth_grid(GRID_DUAL_FRACTAL_LIGHT, GRID_DUAL_FRACTAL, None, d)
    # 未知引擎：不搜参数，避免误改别的策略小节
    return copy.deepcopy(GRID_FIXED)


PARAM_VALUE_LABELS_CN: dict[str, dict[str, str]] = {
    "stop_mode": {"touch": "触及即止损", "close": "收盘价确认"},
    "session_scope": {
        "all": "全时段",
        "day": "只做日盘",
        "night": "只做夜盘",
        "linked": "夜日连续（跳空小）",
    },
    "generic_stop.kind": {
        "off": "关闭",
        "sar": "SAR 移动止损",
        "prior_extreme": "前高低点",
        "chandelier": "ATR 吊灯",
    },
    "macd_timeframe": {"1": "1分钟", "5": "5分钟"},
    "macd_combo": {
        "12/26/9": "12 / 26 / 9（常用）",
        "5/34/5": "5 / 34 / 5",
        "8/17/9": "8 / 17 / 9",
        "6/13/5": "6 / 13 / 5",
    },
    "dir_ema_pair": {
        "5/20": "EMA5 / EMA20",
        "20/60": "EMA20 / EMA60",
        "60/120": "EMA60 / EMA120",
    },
    "direction": {
        "eth_trend": "观察结束顺大方向",
        "day_cluster": "观察结束看昨日密集区",
        "opening_box": "开盘箱体突破",
        "macd_cycle": "MACD一轮循环",
    },
    "flatten_at_close": {"true": "当天清仓", "false": "可拿到隔夜"},
    "slope_exit": {"true": "倾角不够就平仓", "false": "只拦新开仓"},
}
