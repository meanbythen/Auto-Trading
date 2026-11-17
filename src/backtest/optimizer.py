"""
策略优化（网格搜索）
功能：在选定时间段/策略上跑多组回测，按能不能赚钱的得分选出一组，并可写回配置。
得分不看胜率。盈利因子超过 2 不再加分，还要看净赚能不能盖住回撤。
原理：
  - 一次切断：前面一段打分，后面一段只验收；检验比例可手调。
  - 滚动前进：把检验段拆成多段，走一步再验一步；最后一段挑出的参数才考虑写入。
  - 多品种验收：参数在优化用的合约上过关后，再拿到其它关注品种上跑一遍，不重新挑参数。
  - 前一段赚钱、检验段亏掉超过一半，或其它品种上大半都大亏，不准写入。
  - 前一段分数接近时，留更靠近出厂参数的一组。
  - 最终半年默认冻结：搜索碰不到；过关后再验收一次，看过就不能再用同一段。
  - 把检验成交打乱重抽：净盈亏经常变成亏的，不准写入。
"""

from __future__ import annotations

import copy
import itertools
import json
import random
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from loguru import logger

from src.backtest.engine import BacktestResult, filter_bars_by_range, run_backtest
from src.backtest.evaluation import (
    EvaluationReport,
    EvaluationThresholds,
    evaluate_backtest,
    verdict_text,
)
from src.backtest.experiment import archive_report, new_experiment, snapshot_bars
from src.backtest.frozen_oos import (
    MIN_FROZEN_BARS,
    attempt_key,
    describe_freeze_shortage,
    freeze_days_from_cfg,
    freeze_enabled_from_cfg,
    freeze_start_from_bars,
    has_seen_freeze,
    record_freeze_attempt,
    registry_path,
    split_searchable_frozen,
)
from src.backtest.monte_carlo import (
    DEFAULT_MC_RUNS,
    judge_monte_carlo,
    run_trade_monte_carlo,
    trade_pnls,
)
from src.data.kline import KlineProvider
from src.data.periods import canonical_period
from src.strategy.generic_stop import GenericStopConfig, normalize_generic_stop
from src.strategy.session_scope import normalize_scope
from src.strategy.types import normalize_stop_mode


def _opt_bool(value: Any, default: bool = False) -> bool:
    """优化网格里的开关。认 true/false、是/否、开/关。"""
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "是", "开", "on"}:
        return True
    if text in {"0", "false", "no", "n", "否", "关", "off"}:
        return False
    return bool(default)


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
# 日内浮盈飘单：晚开仓、ATR 止损/保本、跳空顺向。
GRID_FLOAT_LIGHT: dict[str, list[Any]] = {
    "eth_minutes": [30, 60],
    "stop_atr_mult": [1.0, 1.5, 2.0],
    "flatten_at_close": [True, False],
    "stop_mode": ["touch", "close"],
}
GRID_FLOAT: dict[str, list[Any]] = {
    "eth_minutes": [20, 45, 60],
    "stop_atr_mult": [1.0, 1.5, 2.0],
    "be_atr_mult": [2.0, 3.0],
    "flatten_at_close": [True, False],
    "stop_mode": ["touch", "close"],
    "day_cutoff": ["10:45", "14:45"],
    "session_scope": ["all", "day", "night"],
}
GRID_FLOAT_FULL: dict[str, list[Any]] = {
    **GRID_FLOAT,
    "macd_timeframe": ["1", "5"],
    "macd_combo": ["12/26/9", "5/34/5"],
    "dir_ema_pair": ["5/20", "20/60", "60/120"],
}

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


# 参数写进策略自己读的那一节。
_ENGINE_PARAM_SECTION = {
    "supertrend": "supertrend",
    "st": "supertrend",
    "dual_ma": "dual_ma",
    "ma_cross": "dual_ma",
    "ema_swing": "ema_swing",
    "macd_swing": "ema_swing",
    "bollinger": "bollinger",
    "boll": "bollinger",
    "sar_trend": "sar_trend",
    "parabolic_sar": "sar_trend",
    "multi_factor": "multi_factor",
    "cta": "multi_factor",
    "swing_trend": "swing_trend",
    "swing_box": "swing_box",
    "dual_donchian": "dual_donchian",
    "dual_fractal": "dual_fractal",
}

# 默认网格跟主策略日内实体一致
DEFAULT_GRID = GRID_BODY

# 通用止损是公共设置：网格里用 generic_stop.xxx 这种带点的键，写回 strategy.generic_stop。
_GENERIC_STOP_PREFIX = "generic_stop."

# 默认：前 70% 挑参数，后 30% 只验收。界面上的「检验比例%」默认 30。
TRAIN_RATIO = 0.70
DEFAULT_HOLDOUT_PCT = 30.0
# 检验比例允许 10%～50%，避免训练段太短或检验段几乎没有。
MIN_HOLDOUT_PCT = 10.0
MAX_HOLDOUT_PCT = 50.0
# 前一段分数相差不超过这个数，就算差不多，改留更靠近出厂参数的一组。
SCORE_TIE = 0.02
# 后一段亏掉的钱，超过前一段盈利的这个比例，就算由赚变成大亏。
HOLDOUT_LOSS_RATIO = 0.50
# 检验方式：一次切断 = 旧逻辑；滚动前进 = 走一步再验一步。
MODE_ONCE = "一次切断"
MODE_WALK = "滚动前进"
DEFAULT_WALK_STEPS = 3

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
    if e == "float_hold":
        return _depth_grid(GRID_FLOAT_LIGHT, GRID_FLOAT, GRID_FLOAT_FULL, d)
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


def grid_for_engine(
    engine: str,
    depth: str = "标准",
    cfg: dict[str, Any] | None = None,
) -> dict[str, list[Any]]:
    """
    按策略引擎 + 搜索深度返回网格。

    优先读 config/优化参数.xlsx 的「参数网格」；表里没有这一段时回退内置默认。
    表是用户可改的，所以每次按文件修改时间重新读，改完保存即生效。
    """
    try:
        from src.backtest import param_table

        table = param_table.load_param_table(param_table.resolve_param_table_path(cfg))
        grid = table.grid(engine, depth)
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取优化参数表失败，回退内置默认: {}", exc)
        grid = None
    if grid is not None:
        return copy.deepcopy(grid)
    return default_grid_for_engine(engine, depth)


def _table_labels() -> dict[str, str]:
    """优化参数表里的中文名；读不到就用内置。"""
    try:
        from src.backtest import param_table

        return param_table.load_param_table(create_if_missing=False).labels
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取参数中文名失败，用内置: {}", exc)
        return {}


def _table_value_labels(key: str) -> dict[str, str]:
    """优化参数表里某个参数的各取值中文说明；读不到就用内置。"""
    try:
        from src.backtest import param_table

        return param_table.load_param_table(create_if_missing=False).value_labels.get(key) or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取参数取值说明失败，用内置: {}", exc)
        return {}


def param_label(key: str) -> str:
    """参数中文名：表里优先，其次内置，最后原键名。"""
    return _table_labels().get(key) or PARAM_LABELS_CN.get(key, key)


def describe_grid(grid: dict[str, list[Any]]) -> str:
    """生成给人看的网格说明文字。"""
    lines = []
    total = 1
    for k, vals in grid.items():
        total *= max(len(vals), 1)
        label = param_label(k)
        lines.append(f"  · {label} ({k}): {vals}")
    lines.insert(
        0,
        f"共 {len(grid)} 个旋钮，理论组合约 {total} 组"
        "（实际按「最多试几组」分层抽样：每个取值先各试一遍，再随机补足）",
    )
    return "\n".join(lines)


# 几个开关型参数的取值中文，优化结果里「值（代号）」一起写，方便对照配置页下拉。
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


def format_param_value(key: str, value: Any) -> str:
    """参数值的中文写法。开关型参数写成「中文（代号）」，其余按原值。"""
    if key == "flatten_at_close":
        on = value if isinstance(value, bool) else _opt_bool(value, True)
        return "当天清仓" if on else "可拿到隔夜"
    if key == "slope_exit":
        on = value if isinstance(value, bool) else _opt_bool(value, False)
        return "倾角不够就平仓" if on else "只拦新开仓"
    if isinstance(value, bool):
        return "是" if value else "否"
    table = _table_value_labels(key) or PARAM_VALUE_LABELS_CN.get(key) or {}
    label = table.get(str(value).strip().lower())
    if label:
        return f"{label}（{value}）"
    return str(value)


def describe_params(params: dict[str, Any] | None) -> str:
    """
    把一组优化参数排成「中文名=值」，用在优化结果、写入确认和参数版本备注。

    开关型参数写成「中文（代号）」，和配置页下拉的选项对得上。
    """
    if not params:
        return "（无）"
    return "；".join(
        f"{param_label(key)}={format_param_value(key, value)}"
        for key, value in params.items()
    )


def _finest_grid_timeframe(grid: dict[str, list[Any]] | None, fallback: str) -> str:
    """
    网格里若搜信号周期，拉 K 线用最细的那一档。

    原理：5 分钟可以收成 15 分钟再判断；若只拉 15 分钟，无法公平试 5 分钟。
    """
    vals = list((grid or {}).get("timeframe") or [])
    vals.extend((grid or {}).get("macd_timeframe") or [])
    minutes: list[int] = []
    for raw in vals:
        period = canonical_period(str(raw))
        if period.isdigit() and int(period) > 0:
            minutes.append(int(period))
    if minutes:
        return str(min(minutes))
    period = canonical_period(str(fallback or "5"))
    return period if period.isdigit() else str(fallback or "5")


def _positions_at(index: int, values: list[list[Any]]) -> tuple[int, ...]:
    """把 0～总组合数 的下标还原成「每个旋钮取第几档」。"""
    picked: list[int] = []
    for vals in reversed(values):
        n = max(len(vals), 1)
        index, pos = divmod(index, n)
        picked.append(pos)
    return tuple(reversed(picked))


def _combo_index(pos: tuple[int, ...], values: list[list[Any]]) -> int:
    """把「每个旋钮取第几档」压成一个下标；只用来去重，进制和 _positions_at 一致。"""
    index = 0
    for i, pos_i in enumerate(pos):
        index = index * max(len(values[i]), 1) + pos_i
    return index


def _sample_combos(
    keys: list[str],
    values: list[list[Any]],
    max_trials: int,
    *,
    seed: int = 42,
) -> list[tuple[Any, ...]]:
    """
    从全部组合里取最多 max_trials 组。

    组合不够多：全部跑。
    组合太多：先分层（每个旋钮的每个取值至少排进去一次，顺序随机打乱），
    再从剩下的组合里随机补足；不把几百万组全部展开，省内存。
    固定种子，同样网格再跑抽到同一批，可复现。
    """
    total = 1
    for vals in values:
        total *= max(len(vals), 1)
    if total <= max_trials:
        return list(itertools.product(*values))

    rng = random.Random(seed)
    positions: list[tuple[int, ...]] = []
    seen: set[int] = set()

    def _add(pos: tuple[int, ...]) -> None:
        code = _combo_index(pos, values)
        if code in seen:
            return
        seen.add(code)
        positions.append(pos)

    # 第一层：分层。第 j 组里，每个旋钮取它自己打乱后的第 j 档；
    # 打乱后刚好保证每个旋钮的每个取值都至少出现一次。
    orders: list[list[int]] = []
    for vals in values:
        order = list(range(max(len(vals), 1)))
        rng.shuffle(order)
        orders.append(order)
    depth = max((len(order) for order in orders), default=1)
    for j in range(min(depth, max_trials)):
        _add(tuple(order[j % len(order)] for order in orders))

    # 第二层：随机补足到 max_trials。用下标随机取，避免展开全部组合。
    attempts = 0
    limit = max_trials * 4 + depth
    while len(positions) < max_trials and attempts < limit:
        attempts += 1
        _add(_positions_at(rng.randrange(total), values))

    # 补不满时（组合空间本身很小，或随机重复太多）：按下标顺序扫，保证够数。
    if len(positions) < max_trials:
        for code in range(total):
            _add(_positions_at(code, values))
            if len(positions) >= max_trials:
                break

    return [
        tuple(values[i][pos_i] if values[i] else None for i, pos_i in enumerate(pos))
        for pos in positions
    ]


def clamp_holdout_pct(pct: float | int | str | None) -> float:
    """把界面上的检验比例收成 10～50。空着或乱填就用默认 30。"""
    try:
        value = float(pct) if pct is not None and str(pct).strip() != "" else DEFAULT_HOLDOUT_PCT
    except (TypeError, ValueError):
        value = DEFAULT_HOLDOUT_PCT
    return min(MAX_HOLDOUT_PCT, max(MIN_HOLDOUT_PCT, value))


def train_ratio_from_holdout_pct(pct: float | int | str | None) -> float:
    """检验比例 30 → 训练比例 0.70。"""
    return 1.0 - clamp_holdout_pct(pct) / 100.0


def split_train_holdout(n_bars: int, ratio: float = TRAIN_RATIO) -> int:
    """
    返回切断位置。前面 [0, cut) 用来挑参数，后面只验收一次。

    行情太短就直接说明，不再把整段拿去打分。前一段至少 50 根，后一段至少 30 根。
    ratio 是训练比例（0.5～0.9），不是检验比例。
    """
    if n_bars < 80:
        raise RuntimeError(
            f"这段行情只有 {n_bars} 根，分不出前一段和后一段。"
            "请把时间段放长，或选「全部可用」。"
        )
    ratio = min(0.90, max(0.50, float(ratio)))
    cut = int(n_bars * ratio)
    # 夹在「前一段够长」和「后一段还剩 30 根」之间，避免某一段只剩几根 K 线。
    return min(max(cut, 50), n_bars - 30)


def walk_forward_folds(n_bars: int, holdout_pct: float, steps: int) -> list[tuple[int, int]]:
    """
    滚动前进的每一段：(训练结束下标, 检验结束下标)，左闭右开。

    原理：把总检验长度拆成 steps 段，挨在行情末尾。
    第 1 段用更早的行情挑参数，验下一段；第 2 段把上一段检验行情并进训练，再验再下一段。
    这样是「走一步再验一步」，检验段从不参加这一步的打分。
    """
    steps = max(1, int(steps))
    holdout_pct = clamp_holdout_pct(holdout_pct)
    # 每一段检验至少 30 根；整段至少留下 50 根给第一段训练。
    min_total_oos = steps * 30
    want_oos = max(min_total_oos, int(n_bars * holdout_pct / 100.0))
    if n_bars < 50 + min_total_oos:
        raise RuntimeError(
            f"这段行情只有 {n_bars} 根，做不了 {steps} 段滚动前进。"
            "请把时间段放长，或减少滚动段数、把检验比例调小。"
        )
    total_oos = min(want_oos, n_bars - 50)
    # 总检验长度不够「每段 30 根」时，自动少做几段，避免硬拆出几根 K 线的空段。
    steps = min(steps, max(1, total_oos // 30))
    oos_len = total_oos // steps
    total_oos = oos_len * steps
    first_train_end = n_bars - total_oos
    folds: list[tuple[int, int]] = []
    for i in range(steps):
        train_end = first_train_end + i * oos_len
        oos_end = train_end + oos_len
        folds.append((train_end, oos_end))
    return folds


def judge_cross_symbols(train_net: float, rows: list[dict[str, Any]]) -> tuple[bool, str]:
    """
    其它关注品种上的样本外验收。参数不再重挑，只看会不会大半都大亏。

    训练段没赚钱时，前面的时间检验已经拦住了，这里直接放行说明。
    """
    usable = [row for row in rows if not row.get("error")]
    if not usable:
        return True, "没有其它关注品种可做多品种验收。"
    if train_net <= 0:
        return True, "训练段没有赚钱，多品种结果只供查看。"
    bad: list[str] = []
    for row in usable:
        net = float(row.get("net_pnl") or 0)
        if net < -HOLDOUT_LOSS_RATIO * train_net:
            bad.append(str(row.get("symbol") or "?"))
    if len(bad) > len(usable) / 2:
        return (
            False,
            "在超过一半的其它关注品种上，亏掉的超过训练盈利的一半："
            + "、".join(bad)
            + "。这次不写入。",
        )
    if bad:
        return True, "其它关注品种里有大亏的（" + "、".join(bad) + "），但不到一半，可以写入，请先看一眼。"
    return True, "其它关注品种上没有大面积大亏，可以写入。"


# 公开渠道（MultiCharts / 期货滚动优化常见说法）里：
# 检验效率 = 检验净盈亏 ÷ 训练净盈亏，大约 0.5 以上算还能保住一半；低于 0.3 很虚。
# 检验段赚钱比例：至少一半的滚动段检验是赚的，才比较像稳健，而不是偶发一段。
MIN_WALK_EFFICIENCY = 0.30
MIN_OOS_WINDOW_WIN_RATE = 0.50


def walk_forward_metrics(
    folds: list[dict[str, Any]],
    *,
    train_net: float,
    hold_net: float,
) -> dict[str, Any]:
    """
    滚动前进的公开常用指标：检验效率、检验段赚钱比例、参数稳不稳。

    原理：只看已经跑完、有检验成绩的段。效率用最后一段训练净盈亏去除总检验净盈亏，
    和「拼接样本外曲线」同一套口径，方便和外面软件对照，不是另一套神秘公式。
    """
    scored = [row for row in folds if row.get("holdout_performance") and not row.get("error")]
    profitable = 0
    for row in scored:
        if float((row.get("holdout_performance") or {}).get("net_pnl") or 0) > 0:
            profitable += 1
    oos_win_rate = (profitable / len(scored)) if scored else 0.0
    if train_net > 1e-9:
        efficiency = float(hold_net) / float(train_net)
    else:
        efficiency = 0.0
    stability = _param_stability_text(scored)
    return {
        "fold_count": len(scored),
        "oos_profitable_folds": profitable,
        "oos_win_rate": round(oos_win_rate, 4),
        "walk_efficiency": round(efficiency, 4),
        "param_stability": stability,
    }


def _param_stability_text(folds: list[dict[str, Any]]) -> str:
    """相邻两段挑出的参数改了多少。改动特别多时提醒可能在贴合噪声。"""
    params_list = [dict(row.get("params") or {}) for row in folds if row.get("params")]
    if len(params_list) < 2:
        return "段数不够，看不出参数稳不稳。"
    jumps = 0
    for left, right in zip(params_list, params_list[1:]):
        keys = set(left) | set(right)
        jumps += sum(1 for key in keys if left.get(key) != right.get(key))
    avg = jumps / (len(params_list) - 1)
    if avg <= 1.0:
        return "相邻两段参数改动很少，比较稳。"
    if avg <= 3.0:
        return "相邻两段参数有一些改动，请对照每一段看一眼。"
    return "相邻两段参数改动较多，可能在贴合噪声，请谨慎写入。"


def judge_walk_forward(metrics: dict[str, Any]) -> tuple[bool, str]:
    """
    滚动前进的额外门槛（一次切断不算这些）。

    检验段赚钱比例过低，或检验效率过低，都不写入。
    段数少于 2 时只提示，不额外拦（行情太短时仍靠原来的总检验判断）。
    """
    folds = int(metrics.get("fold_count") or 0)
    if folds < 2:
        return True, "滚动段少于 2，稳健性指标只供查看。"
    oos_win = float(metrics.get("oos_win_rate") or 0)
    efficiency = float(metrics.get("walk_efficiency") or 0)
    if oos_win + 1e-9 < MIN_OOS_WINDOW_WIN_RATE:
        return (
            False,
            f"滚动检验里赚钱的段只有 {oos_win:.0%}，不到一半，这次不写入。",
        )
    if efficiency + 1e-9 < MIN_WALK_EFFICIENCY:
        return (
            False,
            f"检验效率 {efficiency:.2f}（检验净盈亏÷训练净盈亏）低于 {MIN_WALK_EFFICIENCY:.2f}，"
            "训练里的赚法在检验段保不住，这次不写入。",
        )
    return (
        True,
        f"滚动检验赚钱段 {oos_win:.0%}，检验效率 {efficiency:.2f}。"
        + str(metrics.get("param_stability") or ""),
    )


def once_cut_efficiency(train_net: float, hold_net: float) -> float:
    """一次切断也可看检验效率，方便和公开说法对照。"""
    if train_net > 1e-9:
        return round(float(hold_net) / float(train_net), 4)
    return 0.0


def format_perf_block(
    title: str,
    perf: dict[str, Any] | None,
    *,
    bars: int = 0,
    span: str = "",
    data_from: str = "",
    data_to: str = "",
) -> list[str]:
    """
    把一段绩效写成和历史回测「整体结果」同一套字段。

    训练成绩、检验成绩并排对照时用这一份，避免一边写简略、一边写详细。
    盈利覆盖、每笔收益、日均和交叉测试同一套算法，方便对照。
    """
    from src.backtest.metrics import format_expectancy_line, format_payoff_lines
    from src.screen.picker import format_cover_lines

    p = perf or {}
    lines = [f"—— {title} ——"]
    if bars:
        lines.append(f"K线根数   {bars}" + (f"（{span}）" if span else ""))
    elif span:
        lines.append(f"区间      {span}")
    lines.extend(
        [
            f"成交      {int(p.get('trades') or 0)} 笔"
            f"（赢{int(p.get('wins') or 0)} / 亏{int(p.get('losses') or 0)}）",
            f"胜率      {float(p.get('win_rate') or 0):.0%}",
            *format_payoff_lines(
                float(p.get("avg_win") or 0),
                float(p.get("avg_loss") or 0),
                p.get("payoff"),
            ),
            format_expectancy_line(
                float(p.get("win_rate") or 0),
                float(p.get("avg_win") or 0),
                float(p.get("avg_loss") or 0),
            ),
            f"净盈亏    {float(p.get('net_pnl') or 0):.0f} 元",
            f"盈利因子  {float(p.get('profit_factor') or 0):.2f}",
            f"最大回撤  {float(p.get('max_drawdown') or 0):.0f} 元"
            f"（{float(p.get('max_drawdown_pct') or 0):.2%}）",
            f"综合得分  {float(p.get('score') or 0):.3f}",
        ]
    )
    lines.extend(
        format_cover_lines(
            p.get("net_pnl"),
            p.get("max_drawdown"),
            p.get("trades"),
            data_from,
            data_to,
        )
    )
    return lines


def _factory_values(engine: str) -> dict[str, Any]:
    """出厂参数。分数接近时用来判断哪一组改得更少。"""
    from src.backtest.param_history import FACTORY_DEFAULT_SNAPSHOT, _ENGINE_SECTION

    snap = FACTORY_DEFAULT_SNAPSHOT
    section = _ENGINE_SECTION.get(engine, "")
    values = dict(snap.get(section) or {})
    # 通用止损是公共设置，出厂值取 GenericStopConfig 的默认（关闭）。
    gs = GenericStopConfig()
    values.update(
        {
            "generic_stop.kind": gs.kind,
            "generic_stop.lookback": gs.lookback,
            "generic_stop.sar_af_step": gs.sar_af_step,
            "generic_stop.atr_period": gs.atr_period,
            "generic_stop.atr_mult": gs.atr_mult,
        }
    )
    if engine == "daily_body":
        # eth_minutes / timeframe / 两个截止钟点也会被优化搜到，
        # 分数接近时要能和出厂比距离。
        body = snap.get("daily_body") or {}
        for key in ("eth_minutes", "day_cutoff", "night_cutoff"):
            if key not in values and key in body:
                values[key] = body[key]
        if "timeframe" not in values and "timeframe" in snap:
            values["timeframe"] = snap["timeframe"]
        # 止损触发方式是全策略公共设置，出厂值在 strategy 根。
        if "stop_mode" not in values and "stop_mode" in snap:
            values["stop_mode"] = snap["stop_mode"]
        # 交易时段写在 schedule 小节，出厂是全时段。
        values.setdefault("session_scope", "all")
    if engine == "float_hold":
        hold = snap.get("float_hold") or {}
        values.setdefault("macd_timeframe", hold.get("macd_timeframe", "5"))
        values.setdefault(
            "macd_combo",
            f"{hold.get('macd_fast', 12)}/{hold.get('macd_slow', 26)}/{hold.get('macd_signal', 9)}",
        )
        values.setdefault(
            "dir_ema_pair",
            f"{hold.get('dir_ema_fast', 5)}/{hold.get('dir_ema_slow', 20)}",
        )
        values.setdefault("flatten_at_close", bool(snap.get("flatten_at_close", True)))
        if "stop_mode" not in values and "stop_mode" in snap:
            values["stop_mode"] = snap["stop_mode"]
    if engine in ("ema_swing", "macd_swing"):
        box = snap.get("ema_swing") or snap.get("macd_swing") or {}
        values.setdefault("timeframe", snap.get("timeframe", "5"))
        values.setdefault(
            "dir_ema_pair",
            f"{box.get('dir_ema_fast', 60)}/{box.get('dir_ema_slow', 120)}",
        )
        values.setdefault(
            "sig_ema_triple",
            f"{box.get('sig_ema_a', 5)}/{box.get('sig_ema_b', 10)}/{box.get('sig_ema_c', 30)}",
        )
        values.setdefault("stop_ema", box.get("stop_ema", 120))
        values.setdefault("side_ema", box.get("side_ema", box.get("stop_ema", 120)))
        values.setdefault("slope_ema", box.get("slope_ema", box.get("stop_ema", 120)))
        values.setdefault("slope_exit", bool(box.get("slope_exit", False)))
        values.setdefault(
            "no_open_before_close_min",
            box.get("no_open_before_close_min", 30),
        )
        if "stop_mode" not in values and "stop_mode" in snap:
            values["stop_mode"] = snap["stop_mode"]
    return values


def param_distance(params: dict[str, Any], factory: dict[str, Any]) -> float:
    """
    和出厂参数差多远。数字按出厂值的大小缩放，开关不同记 1。

    只在前一段分数接近时使用，不拿后一段的盈亏来决定留哪一组。
    """
    dist = 0.0
    for key, value in params.items():
        base = factory.get(key)
        if base is None:
            dist += 1.0
            continue
        # bool 也是 int 的一种，必须先比开关，不能当成数字去减。
        if isinstance(value, bool) or isinstance(base, bool):
            dist += 0.0 if bool(value) == bool(base) else 1.0
            continue
        if isinstance(value, (int, float)) and isinstance(base, (int, float)):
            scale = abs(float(base)) if abs(float(base)) > 1e-9 else 1.0
            dist += abs(float(value) - float(base)) / scale
            continue
        if value != base:
            dist += 1.0
    return dist


def pick_train_winner(trials: list[dict[str, Any]], engine: str) -> dict[str, Any] | None:
    """只看前一段分数。成交太少的组（分数约 -999）不参加。分数接近时留更靠近出厂的。"""
    usable = [
        row for row in trials if float(row.get("score", -999)) > -100 and row.get("params")
    ]
    if not usable:
        return None
    best_score = max(float(row["score"]) for row in usable)
    near = [row for row in usable if best_score - float(row["score"]) <= SCORE_TIE]
    factory = _factory_values(engine)
    near.sort(key=lambda row: (param_distance(row["params"], factory), -float(row["score"])))
    return near[0]


def judge_holdout(train_net: float, hold_net: float, hold_trades: int) -> tuple[bool, str]:
    """
    后一段只看一次，不再改参数。

    前一段没赚钱：不写入。
    后一段没开仓：没有样本外证据，不写入。
    后一段还在赚钱，或只是小亏：可以写入。
    后一段把前一段的盈利亏掉一半以上：不写入。
    """
    if train_net <= 0:
        return False, "前一段没有赚到钱，这组参数不写入。"
    if hold_trades <= 0:
        return (
            False,
            "后一段没有成交，没有样本外证据，这组参数不写入。至少要有 1 笔成交才能评审。",
        )
    if hold_net >= 0:
        return True, "后一段也是赚的，可以写入。"
    if abs(hold_net) > HOLDOUT_LOSS_RATIO * train_net:
        return (
            False,
            f"前一段净赚 {train_net:.0f}，后一段净亏 {hold_net:.0f}，"
            "亏掉的超过前一段盈利的一半，这次不写入。",
        )
    return (
        True,
        f"后一段小亏 {hold_net:.0f}，没有超过前一段盈利的一半，可以写入。请先看一眼。",
    )


def _prepare_searchable_bars(
    cfg: dict[str, Any],
    bars_signal: pd.DataFrame,
    bars_trail: pd.DataFrame | None,
) -> tuple[pd.DataFrame, pd.DataFrame | None, dict[str, Any]]:
    """
    打开冻结时：最后 N 天从搜索里拿掉。不够长就直接报错，不偷偷缩短冻结天数。
    """
    meta: dict[str, Any] = {
        "enabled": False,
        "days": 0,
        "start": "",
        "end": "",
        "search_end": "",
        "bars": 0,
        "searchable_bars": len(bars_signal),
    }
    if not freeze_enabled_from_cfg(cfg):
        return bars_signal, bars_trail, meta
    days = freeze_days_from_cfg(cfg)
    freeze_start = freeze_start_from_bars(bars_signal, days)
    searchable, frozen = split_searchable_frozen(bars_signal, freeze_start)
    search_n = 0 if searchable is None else len(searchable)
    frozen_n = 0 if frozen is None else len(frozen)
    shortage = describe_freeze_shortage(search_n, frozen_n, days)
    if shortage:
        raise RuntimeError(shortage)
    search_trail, _frozen_trail = split_searchable_frozen(bars_trail, freeze_start)
    last_search = searchable.index[-1]
    last_all = bars_signal.index[-1]
    meta.update(
        {
            "enabled": True,
            "days": days,
            "start": _stamp(freeze_start),
            "end": _stamp(last_all),
            "search_end": _stamp(last_search),
            "bars": frozen_n,
            "searchable_bars": search_n,
            "freeze_start_ts": freeze_start,
        }
    )
    return searchable, search_trail if bars_trail is not None else None, meta


def _gate_freeze_and_monte_carlo(
    cfg: dict[str, Any],
    *,
    accepted: bool,
    reason: str,
    symbol: str,
    engine_name: str,
    params: dict[str, Any],
    bars_signal: pd.DataFrame,
    bars_trail: pd.DataFrame | None,
    freeze_meta: dict[str, Any],
    train_net: float,
    oos_trades: list[Any],
    gate: bool,
    report: Callable[[str], None] | None = None,
) -> tuple[bool, str, dict[str, Any], dict[str, Any]]:
    """
    最终半年只验收一次，再把检验段成交打乱。

    前面的训练/检验没过关时，不看最终半年，免得先偷看再改参数。
    """
    freeze_box: dict[str, Any] = {
        "enabled": bool(freeze_meta.get("enabled")),
        "days": int(freeze_meta.get("days") or 0),
        "start": str(freeze_meta.get("start") or ""),
        "end": str(freeze_meta.get("end") or ""),
        "bars": int(freeze_meta.get("bars") or 0),
        "already_seen": False,
        "performance": {},
        "reason": "",
        "skip_reason": "",
    }
    mc_trades = list(oos_trades or [])
    freeze_start_ts = freeze_meta.get("freeze_start_ts")
    if accepted and freeze_meta.get("enabled") and params:
        period = str((cfg.get("strategy") or {}).get("timeframe") or "")
        key = attempt_key(
            engine=engine_name,
            symbol=symbol,
            period=period,
            freeze_start=str(freeze_meta.get("start") or ""),
            freeze_end=str(freeze_meta.get("end") or ""),
        )
        path = registry_path(cfg)
        if has_seen_freeze(path, key):
            freeze_box["already_seen"] = True
            freeze_box["reason"] = (
                "这段最终半年已经看过一次，不能再当验收。"
                "请等后面长出还没看过的新行情，或把结束日往前挪、另留一段没看过的。"
            )
            if gate:
                accepted = False
            reason = (reason + freeze_box["reason"]).strip()
        else:
            if report is not None:
                report("最终半年只验收一次")
            frozen_n = int(freeze_meta.get("bars") or 0)
            if frozen_n < MIN_FROZEN_BARS:
                freeze_box["skip_reason"] = describe_freeze_shortage(
                    int(freeze_meta.get("searchable_bars") or 0), frozen_n, int(freeze_meta.get("days") or 0)
                )
                if gate:
                    accepted = False
                reason = (reason + freeze_box["skip_reason"]).strip()
            else:
                freeze_perf, freeze_trades = _holdout_perf_for_params(
                    cfg,
                    symbol=symbol,
                    engine_name=engine_name,
                    params=params,
                    bars_signal=bars_signal,
                    bars_trail=bars_trail,
                    holdout_start_ts=freeze_start_ts,
                )
                freeze_box["performance"] = freeze_perf
                freeze_ok, freeze_reason = judge_holdout(
                    float(train_net),
                    float(freeze_perf.get("net_pnl") or 0),
                    int(freeze_perf.get("trades") or 0),
                )
                freeze_reason = "最终半年：" + freeze_reason
                freeze_box["reason"] = freeze_reason
                record_freeze_attempt(
                    path,
                    key,
                    extra={"engine": engine_name, "symbol": symbol, "accepted": bool(freeze_ok)},
                )
                mc_trades = list(freeze_trades or [])
                if gate and not freeze_ok:
                    accepted = False
                reason = (reason + freeze_reason).strip()

    if report is not None:
        report("成交打乱")
    capital = float((cfg.get("risk") or {}).get("capital") or 100_000)
    ev = cfg.get("evaluation") if isinstance(cfg.get("evaluation"), dict) else {}
    try:
        mc_runs = int(ev.get("mc_runs") or DEFAULT_MC_RUNS)
    except (TypeError, ValueError):
        mc_runs = DEFAULT_MC_RUNS
    mc_box = run_trade_monte_carlo(
        trade_pnls(mc_trades),
        capital=capital,
        n_runs=mc_runs,
        seed=42,
    )
    mc_ok, mc_reason = judge_monte_carlo(mc_box)
    mc_box["reason"] = mc_reason
    if (not mc_box.get("skipped")) and gate and not mc_ok:
        accepted = False
    if mc_reason:
        reason = (reason + mc_reason).strip()
    return accepted, reason, freeze_box, mc_box


def _stamp(value: Any) -> str:
    """K 线时间写成界面上好读的日期。"""
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value)


def _equity_from_trades(trades: list[Any], capital: float) -> list[float]:
    """按平仓盈亏累加权益，用来算后一段的回撤。后一段不另跑网格。"""
    equity = [float(capital)]
    cash = float(capital)
    for trade in trades:
        cash += float(getattr(trade, "pnl", 0) or 0)
        equity.append(cash)
    return equity


def _naive_ts(value: Any) -> pd.Timestamp:
    """去掉时区再比时间。K 线索引和成交时间有一边带时区时，直接比会报错。"""
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is not None:
        return stamp.tz_convert("Asia/Shanghai").tz_localize(None)
    return stamp


def _trades_on_or_after(trades: list[Any], start: Any) -> list[Any]:
    """只留下开仓时间落在后一段的成交。前一段已经平掉的单不拿来验收。"""
    start_ts = _naive_ts(start)
    kept: list[Any] = []
    for trade in trades:
        raw = getattr(trade, "entry_time", "") or ""
        if not raw:
            continue
        try:
            when = _naive_ts(raw)
        except Exception:
            continue
        if when >= start_ts:
            kept.append(trade)
    return kept


@dataclass
class OptimizationResult:
    symbol: str
    best_params: dict[str, Any]
    best_score: float
    best_performance: dict[str, Any]
    trials: list[dict[str, Any]] = field(default_factory=list)
    bars: int = 0
    engine: str = ""
    start_date: str = ""
    end_date: str = ""
    depth: str = "标准"
    grid_keys: list[str] = field(default_factory=list)
    grid_describe: str = ""
    # 最优那一组回测实际用过的开仓限制，方便在结果里核对盈亏是不是按实盘手数算的。
    best_limits: dict[str, Any] = field(default_factory=dict)
    # 后一段验收过了才允许写入。没过时 best_params 仍是前一段挑出的那组，只是不能保存。
    accepted: bool = False
    accept_reason: str = ""
    train_bars: int = 0
    holdout_bars: int = 0
    train_end: str = ""
    holdout_start: str = ""
    train_performance: dict[str, Any] = field(default_factory=dict)
    holdout_performance: dict[str, Any] = field(default_factory=dict)
    used_factory_tie: bool = False
    # 检验方式、检验比例、滚动每一段的明细、其它品种上的样本外成绩。
    mode: str = MODE_ONCE
    holdout_pct: float = DEFAULT_HOLDOUT_PCT
    walk_steps: int = 0
    walk_folds: list[dict[str, Any]] = field(default_factory=list)
    cross_symbol_rows: list[dict[str, Any]] = field(default_factory=list)
    # 公开渠道常用的稳健性数字：检验效率、滚动段赚钱比例、参数稳不稳说明。
    walk_efficiency: float = 0.0
    oos_win_rate: float = 0.0
    param_stability: str = ""
    # 按 docs/策略评估标准 打的完整评估，以及滑点/扰动两项压力测试。
    evaluation: dict[str, Any] = field(default_factory=dict)
    slippage: dict[str, Any] = field(default_factory=dict)
    perturbation: dict[str, Any] = field(default_factory=dict)
    evaluation_ok: bool = True
    evaluation_reason: str = ""
    experiment: dict[str, Any] = field(default_factory=dict)
    freeze: dict[str, Any] = field(default_factory=dict)
    monte_carlo: dict[str, Any] = field(default_factory=dict)
    _experiment_data: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "engine": self.engine,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "depth": self.depth,
            "grid_keys": self.grid_keys,
            "grid_describe": self.grid_describe,
            "best_params": self.best_params,
            "best_score": self.best_score,
            "best_performance": self.best_performance,
            "trials": self.trials,
            "bars": self.bars,
            "best_limits": self.best_limits,
            "accepted": self.accepted,
            "accept_reason": self.accept_reason,
            "train_bars": self.train_bars,
            "holdout_bars": self.holdout_bars,
            "train_end": self.train_end,
            "holdout_start": self.holdout_start,
            "train_performance": self.train_performance,
            "holdout_performance": self.holdout_performance,
            "used_factory_tie": self.used_factory_tie,
            "mode": self.mode,
            "holdout_pct": self.holdout_pct,
            "walk_steps": self.walk_steps,
            "walk_folds": self.walk_folds,
            "cross_symbol_rows": self.cross_symbol_rows,
            "walk_efficiency": self.walk_efficiency,
            "oos_win_rate": self.oos_win_rate,
            "param_stability": self.param_stability,
            "evaluation": self.evaluation,
            "slippage": self.slippage,
            "perturbation": self.perturbation,
            "evaluation_ok": self.evaluation_ok,
            "evaluation_reason": self.evaluation_reason,
            "experiment": self.experiment,
            "freeze": self.freeze,
            "monte_carlo": self.monte_carlo,
        }


# 开仓截止钟点写回 daily_body；读不出来就用出厂的 10:30 / 22:15。
_CUTOFF_DEFAULTS = {"day_cutoff": "10:30", "night_cutoff": "22:15"}


def _cutoff_text(value: Any, fallback: str) -> str:
    """把钟点写成两位的 HH:MM；填错就用出厂值。"""
    try:
        hour, minute = str(value).strip().split(":")
        hour_i, minute_i = int(hour), int(minute)
    except (TypeError, ValueError):
        return fallback
    if not (0 <= hour_i <= 23 and 0 <= minute_i <= 59):
        return fallback
    return f"{hour_i:02d}:{minute_i:02d}"


def _apply_generic_stop(st: dict[str, Any], params: dict[str, Any]) -> None:
    """通用止损是公共设置：任何策略网格里的 generic_stop.* 都写到 strategy.generic_stop。"""
    raw = st.get("generic_stop")
    box = raw if isinstance(raw, dict) else {}
    for key, value in params.items():
        field = key[len(_GENERIC_STOP_PREFIX):]
        if field == "kind":
            box["kind"] = normalize_generic_stop(value)
        elif field in ("lookback", "atr_period"):
            try:
                box[field] = int(value)
            except (TypeError, ValueError):
                continue
        else:
            try:
                num = float(value)
            except (TypeError, ValueError):
                continue
            if num > 0:
                box[field] = num
    st["generic_stop"] = box


def _apply_params(cfg: dict[str, Any], params: dict[str, Any], engine: str | None = None) -> dict[str, Any]:
    """
    把一组参数打进配置副本。

    engine 指定写进哪一套。只改那一节，不改配置里正在使用的策略名字。
    没指定时，仍按配置里的 strategy.engine 决定写到哪一节。
    """
    cfg = copy.deepcopy(cfg)
    st = cfg.setdefault("strategy", {})
    engine = str(engine or st.get("engine") or "daily_body").lower().strip()
    # 通用止损写回 strategy.generic_stop；和策略小节无关，任何引擎都一样。
    generic = {k: v for k, v in params.items() if k.startswith(_GENERIC_STOP_PREFIX)}
    if generic:
        _apply_generic_stop(st, generic)
        params = {k: v for k, v in params.items() if not k.startswith(_GENERIC_STOP_PREFIX)}
    if engine in ("ma20_trend", "ma20"):
        engine = "daily_body"
    if engine == "trend_follow":
        trend = st.setdefault("trend_follow", {})
        for k, v in params.items():
            trend[k] = v
    elif engine in ("supertrend", "st"):
        box = st.setdefault("supertrend", {})
        for k, v in params.items():
            box[k] = v
    elif engine in ("dual_ma", "ma_cross"):
        box = st.setdefault("dual_ma", {})
        for k, v in params.items():
            box[k] = v
    elif engine in ("daily_body", "float_hold"):
        body = st.setdefault(engine, {})
        eth_fallback = 60 if engine == "float_hold" else 20
        for k, v in params.items():
            if k == "eth_minutes":
                # 与配置页「第一ETH窗口」同一项；写在对应策略小节。
                try:
                    body["eth_minutes"] = max(0, int(v))
                except (TypeError, ValueError):
                    body["eth_minutes"] = eth_fallback
            elif k == "timeframe":
                tf = str(v).strip() or "5"
                st["timeframe"] = tf
                # 止损不另用更粗周期，跟信号周期保持一致，避免优出一套用不上的止损周期。
                st["trail_timeframe"] = tf
            elif k == "session_scope":
                # 公共设置，写在 schedule 小节；和配置页「交易时段」同一项。
                cfg.setdefault("schedule", {})["session_scope"] = normalize_scope(v)
            elif k in _CUTOFF_DEFAULTS:
                # 开仓截止钟点，和配置页「日盘/夜盘开仓截止」同一项。
                body[k] = _cutoff_text(v, _CUTOFF_DEFAULTS[k])
            elif k == "stop_mode":
                # 止损触发方式，全策略共用，写在 strategy 根；和配置页同一项。
                st["stop_mode"] = normalize_stop_mode(v)
            elif engine == "float_hold" and k == "flatten_at_close":
                # 可搜「当天清仓」或「拿到隔夜」，写在策略公共开关上。
                st["flatten_at_close"] = _opt_bool(v, True)
            elif engine == "float_hold" and k == "direction":
                from src.strategy.float_hold import normalize_direction_key

                body["direction"] = normalize_direction_key(v)
            elif engine == "float_hold" and k == "macd_combo":
                from src.strategy.float_hold import parse_macd_combo

                fast, slow, signal = parse_macd_combo(v)
                body["macd_fast"] = fast
                body["macd_slow"] = slow
                body["macd_signal"] = signal
                body["macd_combo"] = f"{fast}/{slow}/{signal}"
                if "direction" not in params:
                    body["direction"] = "macd_cycle"
            elif engine == "float_hold" and k == "dir_ema_pair":
                from src.strategy.float_hold import parse_ema_pair_label

                ema_fast, ema_slow = parse_ema_pair_label(v)
                body["dir_ema_fast"] = ema_fast
                body["dir_ema_slow"] = ema_slow
                body["dir_ema_pair"] = f"{ema_fast}/{ema_slow}"
                if "direction" not in params:
                    body["direction"] = "macd_cycle"
            elif engine == "float_hold" and k == "macd_timeframe":
                from src.strategy.float_hold import normalize_macd_timeframe

                tf = normalize_macd_timeframe(v)
                body["macd_timeframe"] = tf
                if "direction" not in params:
                    body["direction"] = "macd_cycle"
                st["timeframe"] = tf
                st["trail_timeframe"] = tf
            else:
                body[k] = v
    elif engine in _ENGINE_PARAM_SECTION:
        box = st.setdefault(_ENGINE_PARAM_SECTION[engine], {})
        for k, v in params.items():
            # 长线箱体的周期写在公共项，不写进自己那一节，否则回测页改了也读不到。
            if engine in ("swing_box", "swing_trend", "dual_donchian", "dual_fractal") and k == "timeframe":
                fallback = "D" if engine in ("swing_box", "dual_donchian", "dual_fractal") else "60"
                tf = str(v).strip() or fallback
                st["timeframe"] = tf
                if "trail_timeframe" not in params:
                    st["trail_timeframe"] = tf
                continue
            if engine in ("swing_box", "swing_trend", "dual_donchian", "dual_fractal") and k == "trail_timeframe":
                fallback = "D" if engine in ("swing_box", "dual_donchian", "dual_fractal") else "60"
                st["trail_timeframe"] = str(v).strip() or st.get("timeframe") or fallback
                continue
            if engine in ("ema_swing", "macd_swing") and k == "timeframe":
                tf = str(v).strip() or "5"
                st["timeframe"] = tf
                st["trail_timeframe"] = tf
                continue
            if engine in ("ema_swing", "macd_swing") and k == "stop_mode":
                st["stop_mode"] = normalize_stop_mode(v)
                continue
            if engine in ("ema_swing", "macd_swing") and k in (
                "dir_ema_pair",
                "dir_ema_fast",
                "dir_ema_slow",
            ):
                from src.strategy.macd_swing import parse_dir_pair, parse_ema_len

                # 优化表可写 21/122，也可分开写两根。短的会排到前面。
                if k == "dir_ema_pair":
                    a, b = parse_dir_pair(v, box.get("dir_ema_fast", 60), box.get("dir_ema_slow", 120))
                elif k == "dir_ema_fast":
                    a, b = parse_dir_pair(
                        None,
                        parse_ema_len(v, 60),
                        parse_ema_len(box.get("dir_ema_slow", 120), 120),
                    )
                else:
                    a, b = parse_dir_pair(
                        None,
                        parse_ema_len(box.get("dir_ema_fast", 60), 60),
                        parse_ema_len(v, 120),
                    )
                box["dir_ema_fast"] = a
                box["dir_ema_slow"] = b
                box["dir_ema_pair"] = f"{a}/{b}"
                box["ema_fast"] = a
                box["ema_slow"] = b
                continue
            if engine in ("ema_swing", "macd_swing") and k in (
                "sig_ema_triple",
                "sig_ema_a",
                "sig_ema_b",
                "sig_ema_c",
            ):
                from src.strategy.macd_swing import format_sig_triple, parse_ema_len, parse_sig_triple

                if k == "sig_ema_triple":
                    a, b, c = parse_sig_triple(v)
                else:
                    box[k] = parse_ema_len(v, {"sig_ema_a": 5, "sig_ema_b": 10, "sig_ema_c": 30}[k])
                    a, b, c = parse_sig_triple(
                        None,
                        box.get("sig_ema_a", 5),
                        box.get("sig_ema_b", 10),
                        box.get("sig_ema_c", 30),
                    )
                box["sig_ema_a"] = a
                box["sig_ema_b"] = b
                box["sig_ema_c"] = c
                box["sig_ema_triple"] = format_sig_triple(a, b, c)
                continue
            if engine in ("ema_swing", "macd_swing") and k == "slope_exit":
                box["slope_exit"] = _opt_bool(v, False)
                continue
            if engine in ("ema_swing", "macd_swing") and k == "no_open_before_close_min":
                from src.strategy.macd_swing import parse_close_guard_min

                box["no_open_before_close_min"] = parse_close_guard_min(v, 30)
                continue
            if engine in ("ema_swing", "macd_swing") and k in ("stop_ema", "side_ema", "slope_ema"):
                from src.strategy.macd_swing import parse_ema_len

                box[k] = parse_ema_len(v, 120)
                continue
            box[k] = v
    else:
        # 未单独列出的引擎：参数写在 strategy 根上
        for k, v in params.items():
            st[k] = v
    return cfg


def _evaluate_champion_full_range(
    cfg: dict[str, Any],
    *,
    best_params: dict[str, Any],
    symbol: str,
    engine_name: str,
    param_keys: list[str],
    start_date: str | None,
    end_date: str | None,
    thresholds: EvaluationThresholds,
    runner: Callable[..., BacktestResult] | None = None,
) -> tuple[EvaluationReport, dict[str, Any], dict[str, Any]]:
    """用冠军参数独立复跑完整请求区间，评估和两项压力共用这一套口径。"""
    eval_cfg = _apply_params(cfg, best_params, engine_name)
    fn = runner or run_backtest
    try:
        eval_result = fn(
            eval_cfg,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            engine=engine_name,
        )
        report = evaluate_backtest(eval_result, thresholds=thresholds)
        # 压力和顺序交给共用入口，和历史回测页保持一致。
        from src.backtest.stress import evaluate_with_stress

        report, slip_box, pert_box, _note = evaluate_with_stress(
            eval_cfg,
            eval_result,
            report=report,
            symbol=symbol,
            engine=engine_name,
            start_date=start_date,
            end_date=end_date,
            param_keys=list(param_keys),
            thresholds=thresholds,
            runner=fn,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("冠军参数全区间评估没跑成 {}: {}", symbol, exc)
        return EvaluationReport(skipped=f"冠军参数全区间评估失败：{exc}"), {}, {}
    return report, slip_box, pert_box


def _slice_trail(bars_trail: pd.DataFrame | None, end_ts: Any) -> pd.DataFrame | None:
    """止损周期 K 线截到训练段结束时刻。没有止损线就返回 None。"""
    if bars_trail is None or not len(bars_trail):
        return None
    return bars_trail.loc[bars_trail.index <= end_ts]


def _score_grid_on_slice(
    cfg: dict[str, Any],
    *,
    symbol: str,
    engine_name: str,
    combos: list[tuple[Any, ...]],
    keys: list[str],
    train_signal: pd.DataFrame,
    train_trail: pd.DataFrame | None,
    min_trades: int,
    fold_tag: str = "",
    on_step: Callable[[int, int], None] | None = None,
) -> list[dict[str, Any]]:
    """只在给定训练段上给每一组参数打分。检验段绝不走进这里。"""
    from src.backtest.metrics import calc_performance

    trials: list[dict[str, Any]] = []
    prefix = f"{fold_tag} " if fold_tag else ""
    for i, combo in enumerate(combos, 1):
        params = dict(zip(keys, combo))
        try:
            result = run_backtest(
                _apply_params(cfg, params, engine_name),
                symbol=symbol,
                bars_signal=train_signal,
                bars_trail=train_trail,
                engine=engine_name,
            )
            perf = calc_performance(result.trades, result.equity, min_trades=min_trades)
            trials.append({"params": params, "score": perf.score, "performance": perf.to_dict()})
            logger.info(
                "{}[{}/{}] 训练 score={:.3f} trades={} net={:.0f} | {}",
                prefix,
                i,
                len(combos),
                perf.score,
                perf.trades,
                perf.net_pnl,
                params,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("{}参数组失败 {}: {}", prefix, params, e)
            trials.append({"params": params, "score": -999, "error": str(e)})
        if on_step is not None:
            try:
                on_step(i, len(combos))
            except Exception as e:  # noqa: BLE001
                # 进度只是提示，界面出错也不该中断优化。
                logger.debug("优化进度回调失败: {}", e)
    trials.sort(key=lambda x: float(x.get("score", -999)), reverse=True)
    return trials


def _holdout_perf_for_params(
    cfg: dict[str, Any],
    *,
    symbol: str,
    engine_name: str,
    params: dict[str, Any],
    bars_signal: pd.DataFrame,
    bars_trail: pd.DataFrame | None,
    holdout_start_ts: Any,
    holdout_end_ts: Any | None = None,
) -> tuple[dict[str, Any], list[Any]]:
    """
    只用检验段独立跑一遍，训练段的仓位和资金不带进来。

    以前是从头跑、再截取检验段成交：训练段结束时可能还持仓，资金曲线也带着训练段的
    盈亏，检验成绩并不独立。现在从检验段开始空仓、按本金重新起跑，只让 warmup
    之前的K线进来看指标。返回 (绩效字典, 检验段成交列表)。
    """
    from src.backtest.metrics import calc_performance

    start = _naive_ts(holdout_start_ts)
    end = _naive_ts(holdout_end_ts) if holdout_end_ts is not None else None
    trail = _slice_trail(bars_trail, holdout_end_ts)
    full = run_backtest(
        _apply_params(cfg, params, engine_name),
        symbol=symbol,
        bars_signal=bars_signal,
        bars_trail=trail,
        engine=engine_name,
        start_date=str(start),
        end_date=str(end) if end is not None else None,
    )
    hold_trades = _trades_on_or_after(full.trades, holdout_start_ts)
    if holdout_end_ts is not None:
        end_ts = _naive_ts(holdout_end_ts)
        kept = []
        for trade in hold_trades:
            raw = getattr(trade, "entry_time", "") or ""
            try:
                when = _naive_ts(raw)
            except Exception:
                continue
            if when <= end_ts:
                kept.append(trade)
        hold_trades = kept
    capital = float((cfg.get("risk") or {}).get("capital") or 1_000_000)
    hold = calc_performance(
        hold_trades,
        _equity_from_trades(hold_trades, capital),
        min_trades=1,
    )
    return hold.to_dict(), hold_trades


def _cross_symbol_count(symbols: list[str] | None, skip_symbol: str) -> int:
    """多品种验收实际会跑几个合约（去掉重复和优化用的那个）。"""
    if not symbols:
        return 0
    skip = str(skip_symbol or "").strip().lower()
    seen: set[str] = set()
    for raw in symbols:
        code = str(raw or "").strip()
        if not code or code == "（还没有）":
            continue
        key = code.lower()
        if key == skip or key in seen:
            continue
        seen.add(key)
    return len(seen)


def validate_params_on_symbols(
    cfg: dict[str, Any],
    *,
    params: dict[str, Any],
    engine: str,
    symbols: list[str],
    skip_symbol: str = "",
    start_date: str | None = None,
    end_date: str | None = None,
    on_step: Callable[[int, int], None] | None = None,
) -> list[dict[str, Any]]:
    """
    把已经挑好的参数，原样拿到其它合约上跑一遍。

    原理：这才是多品种样本外验收。交叉测试页的「每套策略×每个合约」用的是配置里的固定参数，
    那是对照筛选，不会在这里重新搜旋钮。
    """
    from src.backtest.metrics import calc_performance

    cfg = copy.deepcopy(cfg)
    st = cfg.setdefault("strategy", {})
    st["engine"] = str(engine)
    # 先把最优参数写进配置，再用里面的信号周期拉 K 线（日内实体可能优出 5 或 15）。
    cfg = _apply_params(cfg, params, engine)
    st = cfg.setdefault("strategy", {})
    provider = KlineProvider(cfg)
    tf = str(st.get("timeframe", "15"))
    trail_tf = str(st.get("trail_timeframe", tf))
    max_bars = int((cfg.get("backtest") or {}).get("max_bars", 1200))
    skip = str(skip_symbol or "").strip().lower()
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    codes: list[str] = []
    for raw in symbols:
        code = str(raw or "").strip()
        if not code or code == "（还没有）":
            continue
        key = code.lower()
        if key == skip or key in seen:
            continue
        seen.add(key)
        codes.append(code)
    for idx, code in enumerate(codes, 1):
        try:
            bars_signal = provider.fetch(
                code, period=tf, max_bars=max_bars, keep_history=True, start=start_date, end=end_date
            )
            bars_trail = None
            if trail_tf != tf:
                try:
                    bars_trail = provider.fetch(
                        code,
                        period=trail_tf,
                        max_bars=max_bars,
                        keep_history=True,
                        start=start_date,
                        end=end_date,
                    )
                except Exception:  # noqa: BLE001
                    bars_trail = None
            bars_signal = filter_bars_by_range(bars_signal, start_date, end_date)
            bars_trail = filter_bars_by_range(bars_trail, start_date, end_date)
            if bars_signal is None or len(bars_signal) < 30:
                rows.append({"symbol": code, "error": "K线不足", "net_pnl": 0, "trades": 0})
                continue
            result = run_backtest(
                cfg,
                symbol=code,
                bars_signal=bars_signal,
                bars_trail=bars_trail,
                engine=engine,
            )
            perf = calc_performance(result.trades, result.equity, min_trades=1)
            row = {"symbol": code, **perf.to_dict(), "bars": len(bars_signal)}
            report = getattr(result, "evaluation", None)
            if report is not None:
                row["evaluation"] = verdict_text(report)
                row["verdict"] = report.conclusion
                row["fail_items"] = report.failed_text()
            rows.append(row)
            logger.info(
                "多品种验收 {} trades={} net={:.0f} pf={:.2f}",
                code,
                perf.trades,
                perf.net_pnl,
                perf.profit_factor,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("多品种验收失败 {}: {}", code, e)
            rows.append({"symbol": code, "error": str(e), "net_pnl": 0, "trades": 0})
        finally:
            if on_step is not None:
                try:
                    on_step(idx, len(codes))
                except Exception as e:  # noqa: BLE001
                    logger.debug("多品种验收进度回调失败: {}", e)
    return rows


def check_params_on_holdout(
    cfg: dict[str, Any],
    *,
    params: dict[str, Any],
    symbol: str,
    engine: str | None = None,
    depth: str = "标准",
    holdout_pct: float | int | str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    cross_symbols: list[str] | None = None,
    min_trades: int = 3,
    progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    """
    复核手动选中的一组参数：按一次切断的后一段验收，再跑多品种。

    检验成绩只对训练最优那组算过；换个参数组必须重算，不能直接沿用。
    返回 accepted / reason / train_performance / holdout_performance / cross_rows 等。
    """
    from src.backtest.metrics import calc_performance

    cfg = copy.deepcopy(cfg)
    st = cfg.setdefault("strategy", {})
    if engine:
        st["engine"] = str(engine)
    engine_name = str(st.get("engine", "daily_body"))
    holdout_pct = clamp_holdout_pct(holdout_pct)
    grid = grid_for_engine(engine_name, depth, cfg)
    tf = _finest_grid_timeframe(grid, str(st.get("timeframe", "15")))
    trail_tf = str(st.get("trail_timeframe", tf))
    if engine_name == "daily_body":
        trail_tf = tf
    max_bars = int((cfg.get("backtest") or {}).get("max_bars", 1200))
    provider = KlineProvider(cfg)
    bars_signal = filter_bars_by_range(
        provider.fetch(
            symbol, period=tf, max_bars=max_bars, keep_history=True, start=start_date, end=end_date
        ),
        start_date,
        end_date,
    )
    bars_trail = None
    if trail_tf != tf:
        try:
            bars_trail = filter_bars_by_range(
                provider.fetch(
                    symbol,
                    period=trail_tf,
                    max_bars=max_bars,
                    keep_history=True,
                    start=start_date,
                    end=end_date,
                ),
                start_date,
                end_date,
            )
        except Exception:  # noqa: BLE001
            bars_trail = None
    if bars_signal is None or len(bars_signal) < 80:
        got = 0 if bars_signal is None else len(bars_signal)
        raise RuntimeError(f"复核用K线只有 {got} 根，分不出训练段和检验段。请改用「全部可用」时间段。")
    full_signal, full_trail = bars_signal, bars_trail
    bars_signal, bars_trail, freeze_meta = _prepare_searchable_bars(cfg, bars_signal, bars_trail)
    search_end_date = str(freeze_meta.get("search_end") or end_date or "") or end_date

    cross_n = _cross_symbol_count(cross_symbols, symbol)
    total_steps = 2 + cross_n

    def _report(done: int, phase: str) -> None:
        if progress is None:
            return
        try:
            progress(int(done), max(1, total_steps), phase)
        except Exception as e:  # noqa: BLE001
            logger.debug("复核进度回调失败: {}", e)

    _report(0, "复核训练段")
    cut = split_train_holdout(len(bars_signal), train_ratio_from_holdout_pct(holdout_pct))
    train_end_ts = bars_signal.index[cut - 1]
    holdout_start_ts = bars_signal.index[cut]
    train_signal = bars_signal.iloc[:cut]
    train_trail = _slice_trail(bars_trail, train_end_ts)
    train_result = run_backtest(
        _apply_params(cfg, dict(params), engine_name),
        symbol=symbol,
        bars_signal=train_signal,
        bars_trail=train_trail,
        engine=engine_name,
    )
    train_perf = calc_performance(train_result.trades, train_result.equity, min_trades=min_trades).to_dict()

    _report(1, "复核检验段")
    hold_perf, hold_trades = _holdout_perf_for_params(
        cfg,
        symbol=symbol,
        engine_name=engine_name,
        params=dict(params),
        bars_signal=bars_signal,
        bars_trail=bars_trail,
        holdout_start_ts=holdout_start_ts,
    )
    train_net = float(train_perf.get("net_pnl") or 0)
    hold_net = float(hold_perf.get("net_pnl") or 0)
    accepted, reason = judge_holdout(train_net, hold_net, int(hold_perf.get("trades") or 0))

    cross_rows: list[dict[str, Any]] = []
    if cross_symbols:
        _report(2, "多品种验收")
        cross_rows = validate_params_on_symbols(
            cfg,
            params=dict(params),
            engine=engine_name,
            symbols=list(cross_symbols),
            skip_symbol=symbol,
            start_date=start_date,
            end_date=search_end_date,
            on_step=lambda i, n: _report(2 + i, f"多品种验收 {i}/{n}"),
        )
        cross_ok, cross_reason = judge_cross_symbols(train_net, cross_rows)
        if accepted and not cross_ok:
            accepted = False
            reason = cross_reason
    accepted, reason, freeze_box, mc_box = _gate_freeze_and_monte_carlo(
        cfg,
        accepted=accepted,
        reason=reason,
        symbol=symbol,
        engine_name=engine_name,
        params=dict(params),
        bars_signal=full_signal,
        bars_trail=full_trail,
        freeze_meta=freeze_meta,
        train_net=train_net,
        oos_trades=list(hold_trades or []),
        gate=True,
        report=lambda phase: _report(total_steps, phase),
    )
    _report(total_steps, "复核完成")
    return {
        "accepted": bool(accepted),
        "reason": str(reason),
        "train_performance": train_perf,
        "holdout_performance": hold_perf,
        "cross_rows": cross_rows,
        "train_bars": len(train_signal),
        "holdout_bars": len(bars_signal) - cut,
        "train_end": _stamp(train_end_ts),
        "holdout_start": _stamp(holdout_start_ts),
        "hold_trades": len(hold_trades),
        "freeze": freeze_box,
        "monte_carlo": mc_box,
    }


@dataclass
class _SearchOutcome:
    """一次参数搜索的结果盒。两种搜索模式都写同一组字段，主函数只管交叉验收和收尾。"""

    trials: list[dict[str, Any]] = field(default_factory=list)
    walk_folds: list[dict[str, Any]] = field(default_factory=list)
    best_params: dict[str, Any] = field(default_factory=dict)
    best_score: float = -999.0
    train_perf: dict[str, Any] = field(default_factory=dict)
    hold_perf: dict[str, Any] = field(default_factory=dict)
    best_result: BacktestResult | None = None
    accepted: bool = False
    reason: str = "前一段成交太少，没有挑出能用的参数，这次不写入。"
    used_tie: bool = False
    train_bars: int = 0
    holdout_bars: int = 0
    train_end: str = ""
    holdout_start: str = ""
    walk_metrics: dict[str, Any] = field(default_factory=dict)
    actual_steps: int = 0
    total_steps: int = 0
    hold_trades: list[Any] = field(default_factory=list)


def _load_optimization_bars(
    cfg: dict[str, Any],
    *,
    symbol: str,
    engine_name: str,
    grid: dict[str, list[Any]],
    start_date: str | None,
    end_date: str | None,
) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    """按网格最细的信号周期拉K线，截好区间；不足 50 根直接报错。"""
    st = cfg.get("strategy") or {}
    provider = KlineProvider(cfg)
    # 网格若含 timeframe，用最细一档拉信号 K；策略按每组参数自己的周期判断（细周期可收成粗周期）。
    tf = _finest_grid_timeframe(grid, str(st.get("timeframe", "15")))
    trail_tf = str(st.get("trail_timeframe", tf))
    if engine_name == "daily_body":
        trail_tf = tf
    bt_cfg = cfg.get("backtest") or {}
    max_bars = int(bt_cfg.get("max_bars", 1200))
    if "timeframe" in (grid or {}) and tf != str(st.get("timeframe", "")):
        logger.info(
            "优化按网格最细信号周期拉K线: {} 分钟（配置里原是 {}）",
            tf,
            st.get("timeframe"),
        )

    bars_signal = provider.fetch(
        symbol,
        period=tf,
        max_bars=max_bars,
        keep_history=True,
        start=start_date,
        end=end_date,
    )
    bars_trail = None
    if trail_tf != tf:
        try:
            bars_trail = provider.fetch(
                symbol,
                period=trail_tf,
                max_bars=max_bars,
                keep_history=True,
                start=start_date,
                end=end_date,
            )
        except Exception:  # noqa: BLE001
            bars_trail = None

    bars_signal = filter_bars_by_range(bars_signal, start_date, end_date)
    bars_trail = filter_bars_by_range(bars_trail, start_date, end_date)

    if bars_signal is None or len(bars_signal) < 50:
        raise RuntimeError(
            f"优化用K线不足 bars={0 if bars_signal is None else len(bars_signal)}，"
            "请改用「全部可用」时间段或检查网络。"
        )
    return bars_signal, bars_trail


def _walk_forward_search(
    cfg: dict[str, Any],
    *,
    symbol: str,
    engine_name: str,
    combos: list[tuple[Any, ...]],
    keys: list[str],
    bars_signal: pd.DataFrame,
    bars_trail: pd.DataFrame | None,
    min_trades: int,
    holdout_pct: float,
    walk_steps: int,
    cross_n: int,
    report: Callable[[int, int, str], None],
) -> _SearchOutcome:
    """滚动前进：把检验段拆成多段，走一步验一步；最后一段挑出的参数才拿去写入。"""
    from src.backtest.metrics import calc_performance

    folds = walk_forward_folds(len(bars_signal), holdout_pct, walk_steps)
    actual_steps = len(folds)
    # 每段：试参数 len(combos) 步 + 冠军复跑 1 步；最后是验收/整理。
    total_steps = len(combos) * actual_steps + actual_steps + cross_n
    report(0, total_steps, "准备搜索")
    all_oos_trades: list[Any] = []
    logger.info(
        "滚动前进优化 symbol={} engine={} 检验比例={}% 计划{}段实际{}段 K线={} 实跑每段{}组",
        symbol,
        engine_name,
        holdout_pct,
        walk_steps,
        actual_steps,
        len(bars_signal),
        len(combos),
    )
    trials: list[dict[str, Any]] = []
    walk_folds: list[dict[str, Any]] = []
    best_params: dict[str, Any] = {}
    best_score = -999.0
    train_perf: dict[str, Any] = {}
    hold_perf: dict[str, Any] = {}
    best_result: BacktestResult | None = None
    accepted = False
    reason = "前一段成交太少，没有挑出能用的参数，这次不写入。"
    used_tie = False
    train_bars = 0
    holdout_bars = 0
    train_end = ""
    holdout_start = ""
    train_end_ts: Any = None
    for fold_i, (train_end_i, oos_end_i) in enumerate(folds, 1):
        train_end_ts = bars_signal.index[train_end_i - 1]
        oos_start_ts = bars_signal.index[train_end_i]
        oos_end_ts = bars_signal.index[oos_end_i - 1]
        train_signal = bars_signal.iloc[:train_end_i]
        train_trail = _slice_trail(bars_trail, train_end_ts)
        base = (fold_i - 1) * (len(combos) + 1)

        def _on_step(
            i: int, _n: int, *, base: int = base, fold_i: int = fold_i
        ) -> None:
            report(base + i, total_steps, f"第{fold_i}段 试参数")

        fold_trials = _score_grid_on_slice(
            cfg,
            symbol=symbol,
            engine_name=engine_name,
            combos=combos,
            keys=keys,
            train_signal=train_signal,
            train_trail=train_trail,
            min_trades=min_trades,
            fold_tag=f"第{fold_i}段",
            on_step=_on_step,
        )
        winner = pick_train_winner(fold_trials, engine_name)
        fold_row: dict[str, Any] = {
            "fold": fold_i,
            "train_bars": train_end_i,
            "holdout_bars": oos_end_i - train_end_i,
            "train_end": _stamp(train_end_ts),
            "holdout_start": _stamp(oos_start_ts),
            "holdout_end": _stamp(oos_end_ts),
        }
        if winner is None:
            fold_row["error"] = "训练段没有可用参数"
            walk_folds.append(fold_row)
            continue
        fold_params = dict(winner["params"])
        fold_train = dict(winner.get("performance") or {})
        fold_hold, segment_trades = _holdout_perf_for_params(
            cfg,
            symbol=symbol,
            engine_name=engine_name,
            params=fold_params,
            bars_signal=bars_signal,
            bars_trail=bars_trail,
            holdout_start_ts=oos_start_ts,
            holdout_end_ts=oos_end_ts,
        )
        # 各段检验成交拼在一起，最后算总检验成绩。不再为同一段重跑一遍。
        all_oos_trades.extend(segment_trades)
        fold_ok, fold_reason = judge_holdout(
            float(fold_train.get("net_pnl") or 0),
            float(fold_hold.get("net_pnl") or 0),
            int(fold_hold.get("trades") or 0),
        )
        fold_row.update(
            {
                "params": fold_params,
                "train_performance": fold_train,
                "holdout_performance": fold_hold,
                "accepted": fold_ok,
                "accept_reason": fold_reason,
                "score": float(winner.get("score") or -999),
            }
        )
        walk_folds.append(fold_row)
        # 最后一段的参数才拿去写入；前面几段只负责检验稳健性。
        trials = fold_trials
        best_params = fold_params
        best_score = float(winner.get("score") or -999)
        train_perf = fold_train
        train_bars = train_end_i
        holdout_bars = oos_end_i - train_end_i
        train_end = _stamp(train_end_ts)
        holdout_start = _stamp(oos_start_ts)
        top_score = max(float(row.get("score", -999)) for row in fold_trials)
        used_tie = best_score + 1e-9 < top_score
        best_result = run_backtest(
            _apply_params(cfg, best_params, engine_name),
            symbol=symbol,
            bars_signal=train_signal,
            bars_trail=train_trail,
            engine=engine_name,
        )
        report(base + len(combos) + 1, total_steps, f"第{fold_i}段 冠军复跑")

    capital = float((cfg.get("risk") or {}).get("capital") or 1_000_000)
    # 总检验成绩必须用最后写入的那组参数重跑，不能把各段冠军的不同参数混在一起。
    fold_errors = [f for f in walk_folds if f.get("error")]
    final_oos_trades: list[Any] = []
    if best_params and not fold_errors:
        for solved in walk_folds:
            _seg_perf, seg_trades = _holdout_perf_for_params(
                cfg,
                symbol=symbol,
                engine_name=engine_name,
                params=best_params,
                bars_signal=bars_signal,
                bars_trail=bars_trail,
                holdout_start_ts=solved.get("holdout_start"),
                holdout_end_ts=solved.get("holdout_end"),
            )
            final_oos_trades.extend(seg_trades)
        use_trades = final_oos_trades
    else:
        # 有段没有可用参数时整体不通过，这里保留已有成交只为展示。
        use_trades = all_oos_trades
    hold = calc_performance(
        use_trades,
        _equity_from_trades(use_trades, capital),
        min_trades=1,
    )
    hold_perf = hold.to_dict()
    holdout_bars = sum(int(f.get("holdout_bars") or 0) for f in walk_folds)
    # 训练成绩也用最终参数在同一段训练区间重跑，和检验成绩同一套参数、同一口径。
    walk_train_net = float(train_perf.get("net_pnl") or 0)
    if best_params and not fold_errors and train_end_ts is not None:
        try:
            train_full = run_backtest(
                _apply_params(cfg, best_params, engine_name),
                symbol=symbol,
                bars_signal=bars_signal.loc[bars_signal.index <= train_end_ts],
                bars_trail=_slice_trail(bars_trail, train_end_ts),
                engine=engine_name,
            )
            walk_train_net = float(train_full.performance.net_pnl)
        except Exception as exc:  # noqa: BLE001
            logger.warning("滚动前进训练段用最终参数复跑失败，沿用最后一段成绩: {}", exc)
    walk_metrics = walk_forward_metrics(
        walk_folds,
        train_net=walk_train_net,
        hold_net=float(hold.net_pnl),
    )
    if best_params:
        accepted, reason = judge_holdout(
            walk_train_net,
            float(hold.net_pnl),
            int(hold.trades),
        )
        if fold_errors:
            accepted = False
            reason = (
                f"第{fold_errors[0].get('fold')}段训练段没有可用参数，"
                "滚动前进缺一段，整体不通过。"
            )
        walk_ok, walk_reason = judge_walk_forward(walk_metrics)
        if accepted and not walk_ok:
            accepted = False
            reason = walk_reason
        elif walk_reason and not fold_errors:
            reason = reason + walk_reason
        if used_tie:
            reason = "最后一段训练分数接近，留了更靠近出厂参数的一组。" + reason
        reason = f"滚动前进共 {actual_steps} 段。" + reason
    else:
        reason = "滚动前进每一段都没有挑出能用的参数，这次不写入。"
        walk_metrics = walk_forward_metrics(walk_folds, train_net=0.0, hold_net=0.0)
    return _SearchOutcome(
        trials=trials,
        walk_folds=walk_folds,
        best_params=best_params,
        best_score=best_score,
        train_perf=train_perf,
        hold_perf=hold_perf,
        best_result=best_result,
        accepted=accepted,
        reason=reason,
        used_tie=used_tie,
        train_bars=train_bars,
        holdout_bars=holdout_bars,
        train_end=train_end,
        holdout_start=holdout_start,
        walk_metrics=walk_metrics,
        actual_steps=actual_steps,
        total_steps=total_steps,
        hold_trades=list(all_oos_trades),
    )


def _once_cut_search(
    cfg: dict[str, Any],
    *,
    symbol: str,
    engine_name: str,
    combos: list[tuple[Any, ...]],
    keys: list[str],
    bars_signal: pd.DataFrame,
    bars_trail: pd.DataFrame | None,
    min_trades: int,
    train_ratio: float,
    holdout_pct: float,
    cross_n: int,
    total_steps: int,
    report: Callable[[int, int, str], None],
) -> _SearchOutcome:
    """一次切断：前一段打分，后一段只验收。"""
    walk_metrics: dict[str, Any] = {
        "fold_count": 0,
        "oos_profitable_folds": 0,
        "oos_win_rate": 0.0,
        "walk_efficiency": 0.0,
        "param_stability": "",
    }
    trials: list[dict[str, Any]] = []
    best_params: dict[str, Any] = {}
    best_score = -999.0
    train_perf: dict[str, Any] = {}
    hold_perf: dict[str, Any] = {}
    best_result: BacktestResult | None = None
    accepted = False
    reason = "前一段成交太少，没有挑出能用的参数，这次不写入。"
    used_tie = False
    train_bars = 0
    holdout_bars = 0
    train_end = ""
    holdout_start = ""
    hold_trades_list: list[Any] = []

    cut = split_train_holdout(len(bars_signal), train_ratio)
    train_end_ts = bars_signal.index[cut - 1]
    holdout_start_ts = bars_signal.index[cut]
    train_signal = bars_signal.iloc[:cut]
    train_trail = _slice_trail(bars_trail, train_end_ts)
    train_bars = len(train_signal)
    holdout_bars = len(bars_signal) - cut
    train_end = _stamp(train_end_ts)
    holdout_start = _stamp(holdout_start_ts)

    logger.info(
        "一次切断优化 symbol={} engine={} 检验比例={}% 训练{}根 检验从{}起 实跑{}组",
        symbol,
        engine_name,
        holdout_pct,
        train_bars,
        holdout_start,
        len(combos),
    )
    report(0, total_steps, "搜索参数")

    def _on_step(i: int, _n: int) -> None:
        report(i, total_steps, "搜索参数")

    trials = _score_grid_on_slice(
        cfg,
        symbol=symbol,
        engine_name=engine_name,
        combos=combos,
        keys=keys,
        train_signal=train_signal,
        train_trail=train_trail,
        min_trades=min_trades,
        on_step=_on_step,
    )
    report(len(combos), total_steps, "检验冠军参数")
    winner = pick_train_winner(trials, engine_name)
    if winner is not None:
        best_params = dict(winner["params"])
        best_score = float(winner["score"])
        train_perf = dict(winner.get("performance") or {})
        top_score = max(float(row.get("score", -999)) for row in trials)
        used_tie = best_score + 1e-9 < top_score
        best_result = run_backtest(
            _apply_params(cfg, best_params, engine_name),
            symbol=symbol,
            bars_signal=train_signal,
            bars_trail=train_trail,
            engine=engine_name,
        )
        hold_perf, hold_trades_list = _holdout_perf_for_params(
            cfg,
            symbol=symbol,
            engine_name=engine_name,
            params=best_params,
            bars_signal=bars_signal,
            bars_trail=bars_trail,
            holdout_start_ts=holdout_start_ts,
        )
        report(len(combos) + 2, total_steps, "多品种验收" if cross_n else "整理结果")
        accepted, reason = judge_holdout(
            float(train_perf.get("net_pnl") or 0),
            float(hold_perf.get("net_pnl") or 0),
            int(hold_perf.get("trades") or 0),
        )
        walk_metrics["walk_efficiency"] = once_cut_efficiency(
            float(train_perf.get("net_pnl") or 0),
            float(hold_perf.get("net_pnl") or 0),
        )
        if used_tie:
            reason = "前一段分数接近，留了更靠近出厂参数的一组。" + reason
    return _SearchOutcome(
        trials=trials,
        best_params=best_params,
        best_score=best_score,
        train_perf=train_perf,
        hold_perf=hold_perf,
        best_result=best_result,
        accepted=accepted,
        reason=reason,
        used_tie=used_tie,
        train_bars=train_bars,
        holdout_bars=holdout_bars,
        train_end=train_end,
        holdout_start=holdout_start,
        walk_metrics=walk_metrics,
        actual_steps=0,
        total_steps=total_steps,
        hold_trades=hold_trades_list,
    )


def optimize_strategy(
    cfg: dict[str, Any],
    *,
    symbol: str | None = None,
    grid: dict[str, list[Any]] | None = None,
    max_trials: int = 80,
    min_trades: int = 3,
    start_date: str | None = None,
    end_date: str | None = None,
    engine: str | None = None,
    depth: str = "标准",
    seed: int = 42,
    holdout_pct: float | int | str | None = None,
    mode: str = MODE_ONCE,
    walk_steps: int = DEFAULT_WALK_STEPS,
    cross_symbols: list[str] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> OptimizationResult:
    """
    网格搜索最优参数。

    一次切断：按检验比例切开，前一段打分，后一段只验收。
    滚动前进：把检验段拆成多段，每段先在更早行情上挑参数，再验下一段；写入用最后一段挑出的参数。
    多品种：参数在优化合约上过关后，再拿到其它关注品种上原样验收。

    progress(done, total, phase)：给界面报进度用的回调，只报数，不影响挑选逻辑。
    """
    cfg = copy.deepcopy(cfg)
    st = cfg.setdefault("strategy", {})
    if engine:
        st["engine"] = str(engine)
    engine_name = str(st.get("engine", "daily_body"))
    depth = (depth or "标准").strip() or "标准"
    mode = str(mode or MODE_ONCE).strip() or MODE_ONCE
    if mode not in (MODE_ONCE, MODE_WALK):
        mode = MODE_ONCE
    holdout_pct = clamp_holdout_pct(holdout_pct)
    train_ratio = train_ratio_from_holdout_pct(holdout_pct)
    walk_steps = max(1, int(walk_steps or DEFAULT_WALK_STEPS))
    grid = grid or grid_for_engine(engine_name, depth, cfg)
    symbol = symbol or (st.get("symbols") or [cfg.get("trade", {}).get("symbol", "bu2612")])[0]

    bars_signal, bars_trail = _load_optimization_bars(
        cfg,
        symbol=symbol,
        engine_name=engine_name,
        grid=grid,
        start_date=start_date,
        end_date=end_date,
    )
    full_signal, full_trail = bars_signal, bars_trail
    bars_signal, bars_trail, freeze_meta = _prepare_searchable_bars(cfg, bars_signal, bars_trail)
    search_end_date = str(freeze_meta.get("search_end") or end_date or "") or end_date

    keys = list(grid.keys())
    values = [grid[k] for k in keys]
    full_n = 1
    for vs in values:
        full_n *= max(len(vs), 1)
    combos = _sample_combos(keys, values, max_trials, seed=seed)
    if full_n > len(combos):
        logger.warning(
            "理论组合 {} 组，分层随机抽 {} 组试跑（depth={} seed={}），"
            "每个旋钮的取值都会先各排一次，再随机补足",
            full_n,
            len(combos),
            depth,
            seed,
        )
    grid_text = describe_grid(grid)

    # 进度总数：搜索每组算一步，冠军复跑/检验各一步，多品种每个合约一步。
    cross_n = _cross_symbol_count(cross_symbols, symbol)
    total_steps = len(combos) + 2 + cross_n

    def _report(done: int, total: int, phase: str) -> None:
        if progress is None:
            return
        try:
            progress(int(done), max(1, int(total)), phase)
        except Exception as e:  # noqa: BLE001
            logger.debug("优化进度回调失败: {}", e)

    if mode == MODE_WALK:
        outcome = _walk_forward_search(
            cfg,
            symbol=symbol,
            engine_name=engine_name,
            combos=combos,
            keys=keys,
            bars_signal=bars_signal,
            bars_trail=bars_trail,
            min_trades=min_trades,
            holdout_pct=holdout_pct,
            walk_steps=walk_steps,
            cross_n=cross_n,
            report=_report,
        )
    else:
        outcome = _once_cut_search(
            cfg,
            symbol=symbol,
            engine_name=engine_name,
            combos=combos,
            keys=keys,
            bars_signal=bars_signal,
            bars_trail=bars_trail,
            min_trades=min_trades,
            train_ratio=train_ratio,
            holdout_pct=holdout_pct,
            cross_n=cross_n,
            total_steps=total_steps,
            report=_report,
        )

    trials = outcome.trials
    walk_folds = outcome.walk_folds
    best_params = outcome.best_params
    best_score = outcome.best_score
    train_perf = outcome.train_perf
    hold_perf = outcome.hold_perf
    best_result = outcome.best_result
    accepted = outcome.accepted
    reason = outcome.reason
    used_tie = outcome.used_tie
    train_bars = outcome.train_bars
    holdout_bars = outcome.holdout_bars
    train_end = outcome.train_end
    holdout_start = outcome.holdout_start
    walk_metrics = outcome.walk_metrics
    actual_steps = outcome.actual_steps
    total_steps = outcome.total_steps

    cross_rows: list[dict[str, Any]] = []
    _report(
        total_steps - cross_n,
        total_steps,
        "多品种验收" if (best_params and cross_n) else "整理结果",
    )
    if best_params and cross_symbols:
        cross_rows = validate_params_on_symbols(
            cfg,
            params=best_params,
            engine=engine_name,
            symbols=list(cross_symbols),
            skip_symbol=symbol,
            start_date=start_date,
            end_date=search_end_date,
            on_step=lambda i, n: _report(
                total_steps - cross_n + i, total_steps, f"多品种验收 {i}/{n}"
            ),
        )
        cross_ok, cross_reason = judge_cross_symbols(
            float(train_perf.get("net_pnl") or 0),
            cross_rows,
        )
        if cross_reason:
            reason = reason + cross_reason
        if accepted and not cross_ok:
            accepted = False
            reason = cross_reason

    eval_th = EvaluationThresholds.from_cfg(cfg)
    ev_report = None
    slip_box: dict[str, Any] = {}
    pert_box: dict[str, Any] = {}
    eval_reason = ""
    if best_result is not None and eval_th.enabled:
        _report(total_steps, total_steps, "全区间冠军评估与压力测试")
        ev_report, slip_box, pert_box = _evaluate_champion_full_range(
            cfg,
            best_params=best_params,
            symbol=symbol,
            engine_name=engine_name,
            param_keys=list(keys),
            start_date=start_date,
            end_date=search_end_date,
            thresholds=eval_th,
        )
        if ev_report.skipped:
            eval_reason = f"评估未完成：{ev_report.skipped}"
        elif ev_report.conclusion == "reject":
            eval_reason = f"评估红线：{ev_report.failed_text()}"
        if eval_reason:
            eval_reason += "，这次不写入。" if eval_th.gate_on_optimize else "；仅提示，不拦截写入。"
            if eval_th.gate_on_optimize:
                accepted = False
            reason = (reason + eval_reason).strip()

    freeze_box: dict[str, Any] = {"enabled": bool(freeze_meta.get("enabled"))}
    mc_box: dict[str, Any] = {}
    if best_params:

        def _anti_phase(phase: str) -> None:
            _report(total_steps, total_steps, phase)

        accepted, reason, freeze_box, mc_box = _gate_freeze_and_monte_carlo(
            cfg,
            accepted=accepted,
            reason=reason,
            symbol=symbol,
            engine_name=engine_name,
            params=best_params,
            bars_signal=full_signal,
            bars_trail=full_trail,
            freeze_meta=freeze_meta,
            train_net=float(train_perf.get("net_pnl") or 0),
            oos_trades=list(outcome.hold_trades or []),
            gate=True,
            report=_anti_phase,
        )

    experiment_details = {
        "symbol": symbol,
        "engine": engine_name,
        "mode": mode,
        "depth": depth,
        "start_date": str(start_date or ""),
        "end_date": str(end_date or ""),
        "data_source": (cfg.get("data") or {}).get("source", ""),
        "random_seed": int(seed),
        "max_trials": int(max_trials),
        "min_trades": int(min_trades),
        "grid": grid,
        "cross_symbols": list(cross_symbols or []),
        "freeze_final_oos": bool(freeze_meta.get("enabled")),
        "freeze_final_days": int(freeze_meta.get("days") or 0),
        "costs": {
            "slippage_ticks": (
                best_result.params_snapshot.get("slippage_ticks") if best_result is not None else None
            ),
            "price_tick": best_result.params_snapshot.get("price_tick") if best_result is not None else None,
        },
    }
    opt = OptimizationResult(
        symbol=symbol,
        best_params=best_params,
        best_score=float(best_score) if best_params else -999.0,
        best_performance=train_perf,
        trials=trials,
        bars=len(bars_signal),
        engine=engine_name,
        start_date=str(start_date or ""),
        end_date=str(end_date or ""),
        depth=depth,
        grid_keys=list(keys),
        grid_describe=grid_text,
        best_limits=dict((best_result.params_snapshot if best_result is not None else {}) or {}),
        accepted=accepted,
        accept_reason=reason,
        train_bars=train_bars,
        holdout_bars=holdout_bars,
        train_end=train_end,
        holdout_start=holdout_start,
        train_performance=train_perf,
        holdout_performance=hold_perf,
        used_factory_tie=used_tie,
        evaluation=ev_report.to_dict() if ev_report is not None else {},
        slippage=slip_box,
        perturbation=pert_box,
        evaluation_ok=(
            ev_report is None or (not ev_report.skipped and ev_report.conclusion != "reject")
        ),
        evaluation_reason=eval_reason,
        mode=mode,
        holdout_pct=float(holdout_pct),
        walk_steps=actual_steps if mode == MODE_WALK else 0,
        walk_folds=walk_folds,
        cross_symbol_rows=cross_rows,
        walk_efficiency=float(walk_metrics.get("walk_efficiency") or 0),
        oos_win_rate=float(walk_metrics.get("oos_win_rate") or 0),
        param_stability=str(walk_metrics.get("param_stability") or ""),
        experiment=new_experiment("optimization", cfg, details=experiment_details),
        freeze=freeze_box,
        monte_carlo=mc_box,
        _experiment_data=snapshot_bars(signal=full_signal, trail=full_trail),
    )
    _report(total_steps, total_steps, "整理结果")
    return opt


def apply_run_switches(
    cfg: dict[str, Any],
    engine: str | None,
    *,
    flatten_at_close: bool,
) -> dict[str, Any]:
    """
    把这次优化实际用来打分的开关写进配置。

    不改正在用的策略名字、品种和周期。
    听「收盘清仓」总开关的策略会写入 flatten_at_close。
    趋势跟随 / SuperTrend 不听这个开关，所以不写。
    """
    out = copy.deepcopy(cfg)
    st = out.setdefault("strategy", {})
    name = str(engine or "")
    if name in (
        "daily_body",
        "float_hold",
        "dual_ma",
        "ma_cross",
        "kelly_break",
        "liq_stop",
    ):
        st["flatten_at_close"] = bool(flatten_at_close)
    return out


def describe_run_switches(
    engine: str | None,
    *,
    flatten_at_close: bool,
) -> str:
    """写成给人看的几行，说明这次打分到底听了哪些开关。"""
    name = str(engine or "")
    if name == "trend_follow":
        return "开关      趋势跟随不听收盘清仓，用它自己的设置。"
    if name in ("supertrend", "st"):
        return "开关      SuperTrend 默认不听收盘清仓总开关；翻向可反手，靠通道线离场。"
    if name in ("dual_ma", "ma_cross"):
        flat = "开" if flatten_at_close else "关"
        return (
            f"收盘清仓  {flat}\n"
            "双均线趋势听配置页「收盘清仓」总开关；短线上穿长线做多、下穿做空，止损跟长均线。"
        )
    if name in ("ema_swing", "macd_swing"):
        return (
            "开关      均线波段默认可隔夜，不听收盘清仓总开关。\n"
            "入场：方向均线定大方向；信号三均线排好；价格在多空分界线正确一侧。"
            "倾角均线走平不开。空仓时收盘前若干分钟不开（出厂 30，可改）。周期和仓位用公共项。"
        )
    if name in ("bollinger", "boll", "bollinger_break"):
        return (
            "开关      布林带突破用它自己的设置，不跟收盘清仓总开关。\n"
            "入场：收盘同时站上中轨和上轨做多、跌破中轨和下轨做空；只顺势，不做反转。"
        )
    if name in ("sar_trend", "sar", "parabolic_sar"):
        return (
            "开关      SAR 波段用它自己的设置，不跟收盘清仓总开关。\n"
            "入场：SAR 翻向且收盘在均线同侧；离场靠 SAR 当移动止损。"
        )
    if name in ("multi_factor", "multi", "cta"):
        return (
            "开关      多因子 CTA 用它自己的设置，不跟收盘清仓总开关。\n"
            "入场：均线 / 唐奇安 / 布林 / SAR 加权打分，日线定方向，总分越过阈值才开仓。"
        )
    flat = "开" if flatten_at_close else "关"
    if name == "daily_body":
        return (
            f"收盘清仓  {flat}\n"
            "开仓：ETH 画开盘箱体，信号周期收盘站上箱顶做多、跌破箱底做空；影线刺出不算；"
            "太单边或波动大得过分不开新仓；日盘约 10:30、夜盘约 22:15 后不追；日盘/夜盘各只开一次。\n"
            "止损：开盘箱体对沿再让 1 跳并冻结（多守箱底、空守箱顶）；"
            "触发方式可搜「触及即止损」或「收盘价确认」，中间不做回撤止损，没触发就拿到收盘。\n"
            "优化按深度搜：精简只比 ETH、信号周期、止损触发方式和通用止损开关；"
            "标准再加截止钟点、交易时段；完整再放开箱体过滤项和通用止损参数。"
        )
    if name == "float_hold":
        return (
            f"收盘清仓  {flat}（优化可搜当天清仓或拿到隔夜）\n"
            "开仓：MACD一轮循环可搜 1/5 分钟、常见 MACD 参数、均线对 EMA5/20、20/60、60/120；"
            "走完一轮红绿柱后再按均线对定方向。"
            "止损离进场价大约 0.5 倍开盘前波动；浮盈大约 2 倍波动后保本。"
            "开盘箱体那一套仍可手选，不在这几项优化里。"
        )
    if name == "swing_box":
        return (
            "收盘清仓  本策略默认关（仓可隔夜）\n"
            "震荡箱子，收盘走出箱子才顺着做；开仓后箱子冻住；假突破收回箱子就走。"
            "浮盈够一个箱子高度后用 ATR 跟踪。信号周期、止损周期跟配置页/回测页走。"
        )
    if name == "liq_stop":
        return (
            f"收盘清仓  {flat}\n"
            "用最近 N 根高低点拐点估公共止损区；价格击穿止损区、把止损单吃掉后再收回区间外侧，"
            "才顺着 DI 方向进场。止损放区间外侧 K 倍 ATR，止盈按固定盈亏比。ADX 太低不做。"
        )
    if name == "kelly_break":
        return (
            f"收盘清仓  {flat}\n"
            "开仓：收盘站上前 N 根最高价做多、跌破前 N 根最低价做空，影线刺出不算。"
            "止损在进场价外 K 倍 ATR；止盈按固定盈亏比，收盘到价才平。手数用公共凯利/每笔风险。"
        )
    return f"收盘清仓  {flat}"


def apply_params_to_config(
    cfg: dict[str, Any],
    params: dict[str, Any],
    engine: str | None = None,
) -> dict[str, Any]:
    """把最优参数写进配置字典（供保存）。engine 有值时只写那一套，不切换正在用的策略。"""
    return _apply_params(cfg, params, engine=engine)


def save_optimization_report(result: OptimizationResult, out_path: Path | str) -> Path:
    path = Path(out_path)
    # 报告太大时只保留前 30 名明细
    payload = result.to_dict()
    payload["trials"] = payload["trials"][:30]
    # 跑完时刻写进结果文件；导出 Excel 时作为「测试时间」，用来分清是哪一次优化。
    payload["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    details = {
        "symbol": result.symbol,
        "engine": result.engine,
        "mode": result.mode,
        "depth": result.depth,
        "costs": {
            "slippage_ticks": result.best_limits.get("slippage_ticks"),
            "price_tick": result.best_limits.get("price_tick"),
        },
    }
    archive_report(result, path, payload, kind="optimization", details=details)
    return path


def current_params_from_config(
    cfg: dict[str, Any],
    engine: str,
    keys: list[str] | None = None,
) -> dict[str, Any]:
    """
    从现在这份配置里抽出「网格会搜到的那些旋钮」的当前值。

    功能：模拟重跑时拿「正在用的参数」当对照，不拿出厂值当对照。
    原理：写入时 `_apply_params` 把旋钮写进策略小节 / 公共项；这里按同一套键读回来。
    缺的键不硬填，避免把没有的旋钮当成 0 去跑回测。
    """
    st = cfg.get("strategy") or {}
    name = str(engine or st.get("engine") or "daily_body").lower().strip()
    if name in ("ma20_trend", "ma20"):
        name = "daily_body"
    section = _ENGINE_PARAM_SECTION.get(name)
    if section is None:
        if name == "trend_follow":
            section = "trend_follow"
        elif name == "daily_body":
            section = "daily_body"
        else:
            section = name
    box = st.get(section) if isinstance(st.get(section), dict) else {}
    gs = st.get("generic_stop") if isinstance(st.get("generic_stop"), dict) else {}
    schedule = cfg.get("schedule") if isinstance(cfg.get("schedule"), dict) else {}
    if keys is None:
        keys = list(grid_for_engine(name, "标准", cfg).keys())
    out: dict[str, Any] = {}
    for key in keys:
        if key.startswith(_GENERIC_STOP_PREFIX):
            field = key[len(_GENERIC_STOP_PREFIX) :]
            if field in gs:
                out[key] = gs[field]
            continue
        if key == "timeframe" and st.get("timeframe") is not None:
            out[key] = st.get("timeframe")
            continue
        if key == "stop_mode" and st.get("stop_mode") is not None:
            out[key] = st.get("stop_mode")
            continue
        if key == "flatten_at_close":
            out[key] = bool(st.get("flatten_at_close", True))
            continue
        if key == "macd_combo":
            out[key] = (
                f"{int(box.get('macd_fast', 12))}/"
                f"{int(box.get('macd_slow', 26))}/"
                f"{int(box.get('macd_signal', 9))}"
            )
            continue
        if key == "dir_ema_pair":
            out[key] = f"{int(box.get('dir_ema_fast', 5))}/{int(box.get('dir_ema_slow', 20))}"
            continue
        if key == "session_scope":
            out[key] = schedule.get("session_scope", "all")
            continue
        if key in box:
            out[key] = box[key]
            continue
        if key in st:
            out[key] = st[key]
    return out


def _params_same(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """两组旋钮是不是同一套。数字允许 1 和 1.0 算一样。"""
    keys = set(left) | set(right)
    for key in keys:
        a, b = left.get(key), right.get(key)
        if isinstance(a, bool) or isinstance(b, bool):
            if bool(a) != bool(b):
                return False
            continue
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if abs(float(a) - float(b)) > 1e-9:
                return False
            continue
        if str(a) != str(b):
            return False
    return True


def _cover(perf: dict[str, Any] | None) -> float:
    """净赚盖住回撤后还剩多少。回撤在成绩里是正数。"""
    p = perf or {}
    return float(p.get("net_pnl") or 0) - float(p.get("max_drawdown") or 0)


def judge_better_than_current(
    new_hold: dict[str, Any] | None,
    old_hold: dict[str, Any] | None,
    *,
    accepted: bool,
    new_params: dict[str, Any] | None = None,
    old_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    判断新参数在同一段检验上是否全面优于现在这套。

    功能：给「一键切实盘」当闸。没过闸时按钮不能点，避免只看训练曲线就换实盘。
    原理：只比检验段（样本外），和说明书第五节同一口径：净盈亏、回撤、覆盖、盈利因子。
    回撤坏超过现在的两成，或净赚/覆盖掉下去，都不算更好。
    """
    checks: list[str] = []
    new_hold = dict(new_hold or {})
    old_hold = dict(old_hold or {})
    if not accepted:
        checks.append("新参数没有通过检验闸（训练/检验/滚动/多品种）。")
        return {
            "better": False,
            "can_apply_live": False,
            "reason": "新参数没有通过检验，默认不切实盘。",
            "checks": checks,
        }
    checks.append("新参数通过了检验闸。")
    if new_params is not None and old_params is not None and _params_same(new_params, old_params):
        checks.append("挑出的旋钮和现在正在用的一样，没有可切的新方案。")
        return {
            "better": False,
            "can_apply_live": False,
            "reason": "和现在一样，不必切。",
            "checks": checks,
        }
    new_net = float(new_hold.get("net_pnl") or 0)
    old_net = float(old_hold.get("net_pnl") or 0)
    new_dd = float(new_hold.get("max_drawdown") or 0)
    old_dd = float(old_hold.get("max_drawdown") or 0)
    new_pf = float(new_hold.get("profit_factor") or 0)
    old_pf = float(old_hold.get("profit_factor") or 0)
    new_cover = _cover(new_hold)
    old_cover = _cover(old_hold)
    fails: list[str] = []
    if new_net + 1e-9 < old_net:
        fails.append(f"检验净盈亏 {new_net:.0f} 没有超过现在的 {old_net:.0f}")
    else:
        checks.append(f"检验净盈亏 {new_net:.0f} ≥ 现在 {old_net:.0f}")
    if new_cover + 1e-9 < old_cover:
        fails.append(f"检验盈利覆盖 {new_cover:.0f} 没有超过现在的 {old_cover:.0f}")
    else:
        checks.append(f"检验盈利覆盖 {new_cover:.0f} ≥ 现在 {old_cover:.0f}")
    if old_dd > 0 and new_dd > old_dd * 1.2 + 1e-9:
        fails.append(f"检验最大回撤 {new_dd:.0f} 比现在的 {old_dd:.0f} 坏超过两成")
    else:
        checks.append(f"检验最大回撤 {new_dd:.0f}（现在 {old_dd:.0f}）没有明显恶化")
    if new_pf + 1e-9 < 1.0 and old_pf + 1e-9 >= 1.0:
        fails.append(f"检验盈利因子 {new_pf:.2f} 掉到 1 以下，现在是 {old_pf:.2f}")
    else:
        checks.append(f"检验盈利因子 {new_pf:.2f}（现在 {old_pf:.2f}）")
    if fails:
        checks.extend(fails)
        return {
            "better": False,
            "can_apply_live": False,
            "reason": "新参数没有全面优于现在这套，默认不切实盘。" + "；".join(fails),
            "checks": checks,
        }
    return {
        "better": True,
        "can_apply_live": True,
        "reason": "检验段全面优于现在这套，可以一键切实盘。切之前会先存档。",
        "checks": checks,
    }


def save_evolve_report(payload: dict[str, Any], out_path: Path | str) -> Path:
    """把对照报告写成 JSON，方便以后打开看，不改配置。"""
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = dict(payload)
    body["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
