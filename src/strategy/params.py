"""
策略参数默认值（单一事实源）

功能：strategy 根节点的公共参数、各引擎小节的出厂缺省，只在这里写一次。
原理：加载配置补默认、策略内部读兜底、参数存档都从这里取，
      避免同一个数字散落在工厂、策略类、示例配置三四处，改一处漏一处。
      这里只放数字和开关，不含任何交易逻辑。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

# 主策略；旧配置若还写着已删除的引擎名，会回退到这里。
DEFAULT_ENGINE = "daily_body"


@dataclass(frozen=True, slots=True)
class StrategyParams:
    """strategy 根节点里全策略共用的参数。出厂默认值只在这里定义。"""

    engine: str = DEFAULT_ENGINE
    stop_mode: str = "touch"
    entry_mode: str = "close"
    generic_stop_kind: str = "off"
    generic_stop_lookback: int = 20

    def apply_defaults(self, st: dict[str, Any]) -> None:
        """只补缺的键；用户已经写过的值一律不覆盖。"""
        st.setdefault("engine", self.engine)
        st.setdefault("stop_mode", self.stop_mode)
        st.setdefault("entry_mode", self.entry_mode)
        gs = st.get("generic_stop")
        if not isinstance(gs, dict):
            gs = {}
            st["generic_stop"] = gs
        gs.setdefault("kind", self.generic_stop_kind)
        gs.setdefault("lookback", self.generic_stop_lookback)


# 现有策略小节的出厂缺省。sanitize 只补缺的键，不覆盖用户已写的值。
ENGINE_SECTION_DEFAULTS: dict[str, dict[str, Any]] = {
    "daily_body": {
        "eth_minutes": 20,
        "eth_minutes2": 30,
        "stop_extra_ticks": 1,
        "skip_gap_atr_mult": 2.5,
        "max_box_atr_mult": 5.0,
        "max_chase_atr_mult": 1.0,
        "ema_period": 20,
        "ema_buffer_ticks": 2,
        "day_cutoff": "10:30",
        "night_cutoff": "22:15",
        "filter_enabled": True,
        "filter_adx_period": 14,
        "filter_atr_period": 14,
        "filter_lookback": 60,
        "filter_adx_upper": 30.0,
        "filter_atr_high_pct": 0.8,
    },
    "trend_follow": {
        # 文档 5 分钟海龟：20 根高低点进场、10 根出场，止损 2ATR。
        "entry_bars": 20,
        "exit_bars": 10,
        "atr_period": 14,
        "atr_mult": 2.0,
        "flatten_at_close": True,
        # 可选斜率环境门：默认关闭；开启后只做均线斜率同向的突破。
        "slope_gate": {
            "enabled": False,
            "ema_period": 120,
            "lookback": 20,
            "method": "reg",
            "min_deg": 3.0,
            "min_t": 0.0,
            "price_side": False,
            "adaptive": False,
            "adaptive_quantile": 0.8,
            "adaptive_window": 500,
            "adaptive_daily": True,
        },
    },
    "supertrend": {
        "atr_period": 10,
        "multiplier": 3.0,
        "reverse_on_flip": True,
        "flatten_at_close": False,
        # 15 分钟 A/B 通过（lh+fu，3 折 3/3 为正），默认开启；日线请先重新 A/B。
        "slope_gate": {
            "enabled": True,
            "ema_period": 120,
            "lookback": 20,
            "method": "reg",
            "min_deg": 2.5,
            "min_t": 0.0,
            "price_side": False,
            "adaptive": False,
            "adaptive_quantile": 0.8,
            "adaptive_window": 500,
            "adaptive_daily": True,
        },
    },
    "dual_ma": {
        "short_period": 20,
        "long_period": 60,
        "reverse_on_cross": True,
        "atr_period": 14,
        "atr_stop_mult": 2.0,
    },
    "ema_swing": {
        "ema_fast": 60,
        "ema_slow": 120,
        "dir_ema_fast": 60,
        "dir_ema_slow": 120,
        "dir_ema_pair": "60/120",
        "sig_ema_a": 5,
        "sig_ema_b": 10,
        "sig_ema_c": 30,
        "sig_ema_triple": "5/10/30",
        "stop_ema": 120,
        "side_ema": 120,
        "slope_ema": 120,
        "slope_min_deg": 3.0,
        "slope_min_t": 0.0,
        "slope_adaptive": False,
        "slope_adaptive_quantile": 0.8,
        "slope_adaptive_window": 500,
        "slope_adaptive_daily": True,
        "slope_lookback": 20,
        "slope_method": "reg",
        "slope_exit": False,
        "no_open_before_close_min": 30,
        "flatten_at_close": False,
    },
    # 布林突破：中轨方向 + 上下轨突破，ATR 止损，跌回中轨离场。
    "bollinger": {
        "boll_window": 20,
        "boll_dev": 2.0,
        "atr_period": 14,
        "atr_stop_mult": 1.5,
        "flatten_at_close": True,
    },
    # 以损定开：击穿高低点止损聚集区后回流确认，止损放区间外侧。
    "liq_stop": {
        "zone_lookback": 20,
        "zone_tol": 0.005,
        "pivot_span": 2,
        "atr_period": 20,
        "adx_period": 14,
        "adx_min": 20.0,
        "break_window": 3,
        "entry_offset": 0.5,
        "stop_atr": 1.0,
        "take_profit": True,
        "reward_risk": 2.0,
        "vol_mult": 0.0,
    },
    # 凯利突破：收盘突破前 N 根高低点，ATR 止损，固定盈亏比止盈。
    "kelly_break": {
        "lookback": 20,
        "atr_period": 14,
        "stop_atr": 1.2,
        "take_profit": True,
        "reward_risk": 2.5,
        "min_atr_ratio": 0.0,
    },
    # SAR 波段：SAR 翻转 + MA20 方向过滤，SAR 线本身当移动止损。
    "sar_trend": {
        "af_start": 0.02,
        "af_step": 0.02,
        "af_max": 0.2,
        "ma_filter": 20,
        "atr_period": 14,
        "flatten_at_close": True,
    },
    # 多因子 CTA 加权打分。信号周期跟公共 timeframe，不写死 5 分钟。
    "multi_factor": {
        "ma_short": 20,
        "ma_long": 60,
        "channel": 20,
        "boll_window": 20,
        "boll_dev": 2.0,
        "af_start": 0.02,
        "af_step": 0.02,
        "af_max": 0.2,
        "threshold": 0.6,
        "atr_period": 14,
        "atr_stop_mult": 2.0,
        "use_sar": True,
        "disagree_max": 0.7,
        "bias_ma": 60,
        "trend_timeframe": "D",
        "weights": {"ma": 0.30, "turtle": 0.25, "boll": 0.25, "sar": 0.20},
        # 实际听 strategy.flatten_at_close 总开关；缺总开关时才用这一项。
        "flatten_at_close": True,
    },
    "swing_trend": {
        "scale": "D_60",
        "signal_timeframe": "60",
        "trend_timeframe": "D",
        "daily_ma": 20,
        "signal_ma": 20,
        "atr_period": 14,
        "pullback_lookback": 8,
        "touch_atr": 0.6,
        "unit_volume": 1,
        "max_adds": 2,
        "add_min_atr": 1.0,
        "reduce_extend_atr": 2.5,
        "min_core_units": 1,
        "stop_atr": 2.0,
        "flatten_at_close": False,
    },
    "dual_donchian": {
        "trend_timeframe": "D",
        "entry_bars": 55,
        "exit_bars": 20,
        "htf_donchian": 55,
        "adx_period": 14,
        "adx_min": 22.0,
        "atr_period": 14,
        "atr_mult": 2.5,
        "flatten_at_close": False,
    },
    "dual_fractal": {
        "trend_timeframe": "D",
        "trend_mode": "ema",
        "trend_ema": 20,
        "htf_donchian": 55,
        "adx_period": 14,
        "adx_min": 22.0,
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
        "atr_period": 14,
        "atr_mult": 2.8,
        "flatten_at_close": False,
    },
    "swing_box": {
        "lookback": 40,
        "peak_look": 2,
        "min_gap": 5,
        "atr_period": 20,
        "height_atr": 1.2,
        "adx_period": 14,
        "adx_min": 20.0,
        "stop_atr": 0.5,
        "trail_atr": 2.0,
        "trail_start_h": 1.0,
        "vol_mult": 0.0,
        "flatten_at_close": False,
    },
    "rule_dsl": {
        # 规则文件相对 config/ 目录；换成自己的规则只改这里
        "file": "rules/dual_ma_equiv.yaml",
        "use_market_filter": False,
    },
}


def section_defaults(section: str) -> dict[str, Any]:
    """取某个引擎小节的出厂缺省（副本，调用方可随意改）；没登记的返回空字典。"""
    return copy.deepcopy(ENGINE_SECTION_DEFAULTS.get(section) or {})


def section_default(section: str, key: str, fallback: Any) -> Any:
    """取某个引擎小节某一项的出厂缺省；没登记时用调用方给的兜底值。"""
    box = ENGINE_SECTION_DEFAULTS.get(section) or {}
    return box.get(key, fallback)
