"""
策略工厂（多策略路线注册表）
功能：根据 config strategy.engine 创建对应策略；非法名称时列出可选并回退默认。
原理：注册表模式 —— 以后加新策略只需登记到 REGISTRY，不必改主循环。

精简后只保留：结构简单、参考价值高的几套；形态细则过多的已去掉。
默认引擎是日内实体。
"""

from __future__ import annotations

import copy
from typing import Any, Callable, Protocol

from loguru import logger

from .daily_body import DailyBodyStrategy
from .dual_ma import DualMaStrategy
from .morning_edge import (
    MorningBoxAStrategy,
    MorningStructureBStrategy,
)
from .single_ma import SingleMaStrategy
from .supertrend import SuperTrendStrategy
from .swing_trend import SwingTrendStrategy
from .trend_follow import TrendFollowStrategy
from .types import StrategyAction, StrategyDecision


class StrategyLike(Protocol):
    """策略统一接口：主循环只依赖这些能力。"""

    symbols: list[str]
    timeframe: str

    def evaluate(
        self,
        symbol: str,
        bars: Any,
        now: Any = None,
        trail_bars: Any = None,
    ) -> StrategyDecision: ...

    def on_filled(
        self,
        symbol: str,
        action: StrategyAction,
        price: float,
        volume: int,
        initial_stop: float | None = None,
    ) -> None: ...

    def is_flatten_time(self, now: Any = None) -> bool: ...


# 可选属性：双周期止损。部分策略会用另一套周期移动止损。
# runner 用 getattr(strategy, "trail_timeframe", timeframe)


StrategyFactory = Callable[[dict[str, Any]], StrategyLike]

# 主策略改为日内实体；旧配置若还写着已删除的引擎名，会回退到这里。
DEFAULT_ENGINE = "daily_body"

# 已删除的引擎：加载时明确提示并回退，避免静默当成别的策略。
_REMOVED_ENGINES = {
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
    # 8.0.0 再删：开盘缺口C、SAR 翻转、均值回归
    "morning_gap_c",
    "morning_c",
    "sar_flip",
    "sar",
    "mean_reversion",
}

# 配置根上已不再使用的键（旧 Pin/SAR 细则）。保存时清掉，避免误导。
_OBSOLETE_STRATEGY_KEYS = {
    "sar_af_start",
    "sar_af_step",
    "sar_af_max",
    "sar_activate_r",
    "wick_body_ratio",
    "wick_range_ratio",
    "max_opposite_wick_ratio",
}

# 已删策略留下的整节配置。保存时清掉。
_OBSOLETE_STRATEGY_SECTIONS = {
    "liquidity_exhaustion",
    "liquidity_signs",
    "macd_divergence",
    "mean_reversion",
    "reversal_pivot",
    "ma20_trend",
    "morning_gap_c",
    "sar_flip",
    "pinbar_sar",
}

# 现有策略小节的出厂缺省。sanitize 只补缺的键，不覆盖用户已写的值。
_DEFAULT_ENGINE_SECTIONS: dict[str, dict[str, Any]] = {
    "daily_body": {
        "eth_minutes": 20,
        "eth_minutes2": 30,
        "stop_extra_ticks": 1,
        "skip_gap_ticks": 50,
        "max_box_ticks": 100,
        "day_cutoff": "10:30",
        "night_cutoff": "22:15",
    },
    "trend_follow": {
        "entry_bars": 36,
        "exit_bars": 12,
        "atr_period": 14,
        "atr_mult": 2.0,
        "flatten_at_close": False,
    },
    "supertrend": {
        "atr_period": 10,
        "multiplier": 3.0,
        "reverse_on_flip": True,
        "flatten_at_close": False,
    },
    "single_ma": {
        "ma_period": 20,
        "reverse_on_cross": True,
    },
    "dual_ma": {
        "short_period": 20,
        "long_period": 60,
        "reverse_on_cross": True,
    },
    "morning_box_a": {
        "be_box_mult": 0.9,
        "vol_confirm_bars": 3,
        "fake_break_bars": 2,
        "max_trades_per_day": 2,
        "stop_buffer_ticks": 2,
        "enable_night": True,
        "no_new_before_close_minutes": 15,
    },
    "morning_structure_b": {
        "be_box_mult": 0.9,
        "max_trades_per_day": 2,
        "stop_buffer_ticks": 2,
        "enable_night": True,
        "no_new_before_close_minutes": 15,
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
}

# 交叉测试名单里只留正式引擎名（不含别名）。
_CROSS_ENGINE_ORDER = [
    "daily_body",
    "morning_box_a",
    "morning_structure_b",
    "trend_follow",
    "supertrend",
    "single_ma",
    "dual_ma",
    "swing_trend",
]

_ENGINE_ALIASES = {
    "morning_a": "morning_box_a",
    "morning_b": "morning_structure_b",
    "st": "supertrend",
    "ma_cross": "dual_ma",
    "ma20_trend": "single_ma",
    "ma20": "single_ma",
    "Single_ma": "single_ma",
}


# 策略路线菜单：配置里 engine 填左边的名字
REGISTRY: dict[str, StrategyFactory] = {
    # 主推：开盘箱体收盘突破 + 极值/ATR 止损
    "daily_body": DailyBodyStrategy,
    # 早盘定方向+保本拿收盘。A箱体 / B高低递进
    "morning_box_a": MorningBoxAStrategy,
    "morning_a": MorningBoxAStrategy,
    "morning_structure_b": MorningStructureBStrategy,
    "morning_b": MorningStructureBStrategy,
    # 经典结构趋势
    "trend_follow": TrendFollowStrategy,
    "supertrend": SuperTrendStrategy,
    "st": SuperTrendStrategy,
    "single_ma": SingleMaStrategy,
    "dual_ma": DualMaStrategy,
    "ma_cross": DualMaStrategy,
    # 日线定方向 + 60 分钟回调，支持有限次加仓与伸展减仓
    "swing_trend": SwingTrendStrategy,
}


def sanitize_strategy_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """
    清理已删策略残留，并补上现有策略缺的默认小节。

    原理：旧配置里常还带着流动性耗尽、SAR、均值回归等整节；
    界面保存或应用存档时顺手清掉，并把 ETH 分钟数迁到日内实体。
    已有数字不覆盖，只补缺的键。原地修改并返回同一份 cfg。
    """
    st = cfg.setdefault("strategy", {})
    if not isinstance(st, dict):
        cfg["strategy"] = {}
        st = cfg["strategy"]

    # ETH 分钟：旧配置写在 liquidity_exhaustion 时，先迁到 daily_body 再删旧节。
    body = st.get("daily_body")
    if not isinstance(body, dict):
        body = {}
        st["daily_body"] = body
    if "eth_minutes" not in body:
        old_le = st.get("liquidity_exhaustion")
        if isinstance(old_le, dict) and old_le.get("eth_minutes") is not None:
            body["eth_minutes"] = old_le.get("eth_minutes")

    # 止损改为箱沿极值后这两个参数已不被策略读取；界面保存时也会 pop，这里保持一致。
    for dead in ("initial_atr_mult", "reverse_atr_mult"):
        body.pop(dead, None)

    for key in list(_OBSOLETE_STRATEGY_KEYS):
        st.pop(key, None)
    for key in list(_OBSOLETE_STRATEGY_SECTIONS):
        st.pop(key, None)
    st.pop("ma20_trend", None)

    for section, defaults in _DEFAULT_ENGINE_SECTIONS.items():
        box = st.get(section)
        if not isinstance(box, dict):
            box = {}
            st[section] = box
        for key, value in defaults.items():
            if key not in box:
                box[key] = copy.deepcopy(value)

    # 引擎名别名收成正式名；已删的回退到日内实体。
    raw_engine = str(st.get("engine") or DEFAULT_ENGINE).strip()
    engine = _ENGINE_ALIASES.get(raw_engine, raw_engine)
    engine_l = engine.lower() if engine != "Single_ma" else "single_ma"
    engine = _ENGINE_ALIASES.get(engine_l, engine_l)
    if engine in _REMOVED_ENGINES or engine not in REGISTRY:
        engine = DEFAULT_ENGINE
    st["engine"] = engine

    # 交叉测试名单：丢掉已删名字，缺的正式引擎补上。
    raw_cross = st.get("cross_engines")
    cleaned_cross: list[str] = []
    seen: set[str] = set()
    if isinstance(raw_cross, list):
        for item in raw_cross:
            name = _ENGINE_ALIASES.get(str(item).strip(), str(item).strip())
            name = _ENGINE_ALIASES.get(name.lower(), name.lower() if name != "Single_ma" else "single_ma")
            if name in _REMOVED_ENGINES or name not in _CROSS_ENGINE_ORDER:
                continue
            if name in seen:
                continue
            seen.add(name)
            cleaned_cross.append(name)
    for code in _CROSS_ENGINE_ORDER:
        if code not in seen:
            cleaned_cross.append(code)
    st["cross_engines"] = cleaned_cross
    return cfg


def list_engines() -> list[str]:
    """返回可供配置的主引擎名（不含纯别名重复时仍全部列出，便于对照）。"""
    return sorted(REGISTRY.keys())


def create_strategy(cfg: dict[str, Any]) -> StrategyLike:
    """
    创建策略实例。

    参数:
        cfg: 全局配置，读取 strategy.engine

    返回:
        实现 StrategyLike 的策略对象
    """
    raw = str(cfg.get("strategy", {}).get("engine", DEFAULT_ENGINE)).lower().strip()
    name = raw or DEFAULT_ENGINE
    # 历史配置里若还写着旧代号，收成现在的 single_ma（界面显示 Single_ma）
    if name in {"ma20_trend", "ma20"}:
        name = "single_ma"

    if name in _REMOVED_ENGINES:
        logger.warning(
            "strategy.engine={} 已从程序中移除（精简策略菜单）；回退到 {}",
            name,
            DEFAULT_ENGINE,
        )
        name = DEFAULT_ENGINE

    factory = REGISTRY.get(name)
    if factory is None:
        logger.warning(
            "未知 strategy.engine={}，可选: {}；回退到 {}",
            name,
            ", ".join(list_engines()),
            DEFAULT_ENGINE,
        )
        name = DEFAULT_ENGINE
        factory = REGISTRY[DEFAULT_ENGINE]

    strategy = factory(cfg)
    logger.info(
        "策略引擎已加载: engine={} class={}",
        name,
        type(strategy).__name__,
    )
    return strategy
