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

from .cta_filter import ensure_cta_filter_box
from .market_state import TREND_FILTER_SECTIONS, ensure_trend_filter_defaults
from .bollinger_break import BollingerBreakStrategy
from .daily_body import DailyBodyStrategy
from .dual_ma import DualMaStrategy
from .jesse_casper import JesseCasperStrategy
from .morning_edge import MorningBoxAStrategy
from .multi_factor import MultiFactorStrategy, MultiHybridStrategy, MultiVoteStrategy
from .multi_factor_adapt import MultiFactorAdaptStrategy
from .sar_trend import SarTrendStrategy
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
    # 9.40.0 删掉：单均线（被双均线覆盖）、早盘高低递进B（与早盘箱体A同源）
    "single_ma",
    "Single_ma",
    "ma20_trend",
    "ma20",
    "morning_structure_b",
    "morning_b",
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
    "single_ma",
    "morning_structure_b",
    "ma20",
    "rotation_scan",
}

# 现有策略小节的出厂缺省。sanitize 只补缺的键，不覆盖用户已写的值。
_DEFAULT_ENGINE_SECTIONS: dict[str, dict[str, Any]] = {
    "daily_body": {
        "eth_minutes": 20,
        "eth_minutes2": 30,
        "stop_extra_ticks": 1,
        "skip_gap_ticks": 50,
        "max_box_ticks": 100,
        "max_chase_ticks": 20,
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
    },
    "supertrend": {
        "atr_period": 10,
        "multiplier": 3.0,
        "reverse_on_flip": True,
        "flatten_at_close": False,
    },
    "dual_ma": {
        "short_period": 20,
        "long_period": 60,
        "reverse_on_cross": True,
        "atr_period": 14,
        "atr_stop_mult": 2.0,
    },
    # 布林突破：中轨方向 + 上下轨突破，ATR 止损，跌回中轨离场。
    "bollinger": {
        "boll_window": 20,
        "boll_dev": 2.0,
        "atr_period": 14,
        "atr_stop_mult": 1.5,
        "flatten_at_close": True,
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
    # 多因子 CTA 三套对照共用这一组因子参数。信号周期跟公共 timeframe，不写死 5 分钟。
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
        "flatten_at_close": True,
    },
    # 自适应对照：开平仓抄自主推，权重只在平仓后慢改。不自动替换实盘。
    "multi_factor_adapt": {
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
        "flatten_at_close": True,
        "adapt_enabled": True,
        "lookback_trades": 20,
        "min_weight": 0.05,
        "max_weight": 0.40,
        "recent_mix": 0.30,
        "max_step": 0.03,
    },
    "multi_vote": {
        "min_votes": 2,
        "min_vote_diff": 2,
        "disagree_max": 0.7,
        "atr_stop_mult": 2.0,
        "use_sar": True,
        "flatten_at_close": True,
    },
    "multi_hybrid": {
        "min_votes": 2,
        "min_vote_diff": 2,
        "threshold": 0.6,
        "min_position_ratio": 0.5,
        "max_position_ratio": 1.0,
        "disagree_max": 0.7,
        "atr_stop_mult": 2.0,
        "use_sar": True,
        "flatten_at_close": True,
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
    "jesse_casper": {
        "trend_timeframe": "15",
        "lookback_liq": 20,
        "vol_ma_len": 20,
        "vol_mult": 1.2,
        "displacement_body_ratio": 0.5,
        "min_rr": 3.0,
        "price_tick": 1.0,
        "va_pct": 0.70,
        "use_va": True,
        "use_kill_zone": True,
        "use_daily_halt": True,
        "max_losses_day": 2,
        "max_wins_day": 1,
        "kill_day_start": "09:00",
        "kill_day_end": "10:00",
        "kill_night_start": "21:00",
        "kill_night_end": "22:30",
        "setup_wait_bars": 16,
        "target_lookback": 40,
        "flatten_at_close": True,
    },
}

# 交叉测试名单里只留正式引擎名（不含别名）。
_CROSS_ENGINE_ORDER = [
    "daily_body",
    "morning_box_a",
    "trend_follow",
    "supertrend",
    "dual_ma",
    "bollinger",
    "sar_trend",
    "multi_factor",
    "multi_factor_adapt",
    "multi_vote",
    "multi_hybrid",
    "swing_trend",
    "jesse_casper",
]

_ENGINE_ALIASES = {
    "morning_a": "morning_box_a",
    "st": "supertrend",
    "ma_cross": "dual_ma",
    "boll": "bollinger",
    "bollinger_break": "bollinger",
    "sar": "sar_trend",
    "parabolic_sar": "sar_trend",
    "multi": "multi_factor",
    "cta": "multi_factor",
    "vote": "multi_vote",
    "hybrid": "multi_hybrid",
    "casper": "jesse_casper",
    "jesse": "jesse_casper",
}


# 策略路线菜单：配置里 engine 填左边的名字
REGISTRY: dict[str, StrategyFactory] = {
    # 主推：开盘箱体收盘突破 + 极值/ATR 止损
    "daily_body": DailyBodyStrategy,
    # 早盘定方向+保本拿收盘
    "morning_box_a": MorningBoxAStrategy,
    "morning_a": MorningBoxAStrategy,
    # 经典结构趋势
    "trend_follow": TrendFollowStrategy,
    "supertrend": SuperTrendStrategy,
    "st": SuperTrendStrategy,
    "dual_ma": DualMaStrategy,
    "ma_cross": DualMaStrategy,
    # 布林带趋势突破（不做轨道反转）
    "bollinger": BollingerBreakStrategy,
    "boll": BollingerBreakStrategy,
    # SAR 抛物转向波段（MA20 过滤，SAR 当移动止损）
    "sar_trend": SarTrendStrategy,
    "parabolic_sar": SarTrendStrategy,
    # 多因子 CTA 三套对照：加权打分 / 等权投票 / 投票+打分混合
    "multi_factor": MultiFactorStrategy,
    "cta": MultiFactorStrategy,
    "multi_factor_adapt": MultiFactorAdaptStrategy,
    "multi_vote": MultiVoteStrategy,
    "vote": MultiVoteStrategy,
    "multi_hybrid": MultiHybridStrategy,
    "hybrid": MultiHybridStrategy,
    # 日线定方向 + 60 分钟回调，支持有限次加仓与伸展减仓
    "swing_trend": SwingTrendStrategy,
    # Jesse Casper 简化 SMC：K线+成交量+价值区，不是原版订单流
    "jesse_casper": JesseCasperStrategy,
    "casper": JesseCasperStrategy,
    "jesse": JesseCasperStrategy,
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

    # 止损触发方式已从日内实体挪到 strategy 根，全策略共用；旧配置写在 daily_body 里，迁上来。
    legacy_stop_mode = body.pop("stop_mode", None)
    if legacy_stop_mode is not None and "stop_mode" not in st:
        st["stop_mode"] = legacy_stop_mode
    st.setdefault("stop_mode", "touch")
    # 开仓触发方式（全策略共用）：trigger=触到即进，close=收盘确认（默认保守）。
    st.setdefault("entry_mode", "close")
    # 公共行情过滤已拆进各策略。旧配置里的 market_filter 只用来给趋势策略补缺省。
    mf = st.get("market_filter") if isinstance(st.get("market_filter"), dict) else {}
    for section in TREND_FILTER_SECTIONS:
        box = st.get(section)
        if not isinstance(box, dict):
            box = {}
            st[section] = box
        ensure_trend_filter_defaults(box, mf)
    # 通用止损（独立于策略自身止损）：默认关闭；结构固定，界面/回测/优化都读这里。
    gs = st.get("generic_stop")
    if not isinstance(gs, dict):
        gs = {}
        st["generic_stop"] = gs
    gs.setdefault("kind", "off")
    gs.setdefault("lookback", 20)

    for key in list(_OBSOLETE_STRATEGY_KEYS):
        st.pop(key, None)
    for key in list(_OBSOLETE_STRATEGY_SECTIONS):
        st.pop(key, None)

    for section, defaults in _DEFAULT_ENGINE_SECTIONS.items():
        box = st.get(section)
        if not isinstance(box, dict):
            box = {}
            st[section] = box
        for key, value in defaults.items():
            if key not in box:
                box[key] = copy.deepcopy(value)

    # 三套多因子各自补一份行情闸门；缺的键才填，已经写过的数字不改。
    for section in ("multi_factor", "multi_factor_adapt", "multi_vote", "multi_hybrid"):
        box = st.get(section)
        if isinstance(box, dict):
            ensure_cta_filter_box(box, section)

    # 引擎名别名收成正式名；已删的回退到日内实体。
    raw_engine = str(st.get("engine") or DEFAULT_ENGINE).strip().lower()
    engine = _ENGINE_ALIASES.get(raw_engine, raw_engine)
    if engine in _REMOVED_ENGINES or engine not in REGISTRY:
        engine = DEFAULT_ENGINE
    st["engine"] = engine

    # 交叉测试名单：丢掉已删名字，缺的正式引擎补上。
    raw_cross = st.get("cross_engines")
    cleaned_cross: list[str] = []
    seen: set[str] = set()
    if isinstance(raw_cross, list):
        for item in raw_cross:
            name = str(item).strip().lower()
            name = _ENGINE_ALIASES.get(name, name)
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
    # 旧代号先收成正式名（ma_cross→dual_ma、sar→sar_trend、boll→bollinger）。
    name = _ENGINE_ALIASES.get(name, name)

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
    # 两个全策略公共开关统一塞一份到实例上，避免每个策略再各自从 cfg 读一遍。
    # 个别策略也可以在 __init__ 里覆写（目前没有）。
    from .types import entry_mode_from_cfg, stop_mode_from_cfg

    strategy.stop_mode = stop_mode_from_cfg(cfg)
    strategy.stop_on_touch = strategy.stop_mode == "touch"
    strategy.entry_mode = entry_mode_from_cfg(cfg)
    logger.info(
        "策略引擎已加载: engine={} class={} stop_mode={} entry_mode={}",
        name,
        type(strategy).__name__,
        strategy.stop_mode,
        strategy.entry_mode,
    )
    return strategy
