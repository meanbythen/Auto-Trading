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
from .float_hold import (
    FloatHoldStrategy,
    normalize_direction_key,
    normalize_macd_timeframe,
)
from .dual_ma import DualMaStrategy
from .macd_swing import MacdSwingStrategy
from .multi_factor import MultiFactorStrategy
from .sar_trend import SarTrendStrategy
from .supertrend import SuperTrendStrategy
from .swing_box import SwingBoxStrategy
from .swing_trend import SwingTrendStrategy
from .trend_follow import TrendFollowStrategy
from .types import StrategyAction, StrategyDecision


class StrategyLike(Protocol):
    """策略统一接口：主循环只依赖这些能力。"""

    symbols: list[str]
    timeframe: str

    def get_signaled_bar(self, symbol: str) -> str: ...

    def set_signaled_bar(self, symbol: str, bar_id: Any) -> None: ...

    def clear_signaled_bar(self, symbol: str, expected_bar_id: Any = None) -> bool: ...

    def get_last_entry_bar(self, symbol: str) -> str: ...

    def set_last_entry_bar(self, symbol: str, bar_id: Any) -> None: ...

    def get_last_entry_bars(self) -> dict[str, str]: ...

    def restore_last_entry_bars(self, bars: dict[str, Any] | None) -> None: ...

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
    # 9.40.0 删掉：单均线（被双均线覆盖）、早盘高低递进B
    "single_ma",
    "Single_ma",
    "ma20_trend",
    "ma20",
    "morning_structure_b",
    "morning_b",
    # 19.0.0 删掉早盘箱体突破 A
    "morning_box_a",
    "morning_a",
    # 23.0.0 删掉 Jesse Casper（分钟线没有足迹图，理论对不上）
    "jesse_casper",
    "casper",
    "jesse",
    # 24.0.0 删掉多因子自适应权重对照（加权打分不动）
    "multi_factor_adapt",
    # 25.0.0 删掉多因子混合（加权打分、投票不动）
    "multi_hybrid",
    "hybrid",
    # 26.0.0 删掉多因子投票（加权打分不动）
    "multi_vote",
    "vote",
    # 27.0.0 删掉日内实体·扩张（日内实体不动）
    "daily_body_expand",
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
    "morning_box_a",
    "jesse_casper",
    "multi_factor_adapt",
    "multi_hybrid",
    "multi_vote",
    "daily_body_expand",
}

# 现有策略小节的出厂缺省。sanitize 只补缺的键，不覆盖用户已写的值。
_DEFAULT_ENGINE_SECTIONS: dict[str, dict[str, Any]] = {
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
    "float_hold": {
        "eth_minutes": 60,
        "eth_minutes2": 0,
        "stop_atr_mult": 1.5,
        "be_atr_mult": 2.0,
        "skip_gap_atr_mult": 0.0,
        "max_box_atr_mult": 0.0,
        "max_chase_atr_mult": 0.0,
        "direction": "eth_trend",
        "macd_timeframe": "5",
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
        "dir_ema_fast": 5,
        "dir_ema_slow": 20,
        "trend_align_enabled": False,
        "trend_align_fast": 60,
        "trend_align_slow": 120,
        "trend_slope_lookback": 5,
        "trend_slope_min_deg": 0.0,
        "cluster_max_width": 10,
        "gap_align_atr_mult": 0.0,
        "ema_period": 0,
        "ema_buffer_ticks": 2,
        "day_cutoff": "14:45",
        "night_cutoff": "22:50",
        "filter_enabled": False,
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
        "slope_lookback": 20,
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
}

# 交叉测试名单里只留正式引擎名（不含别名）。
_CROSS_ENGINE_ORDER = [
    "daily_body",
    "float_hold",
    "trend_follow",
    "supertrend",
    "dual_ma",
    "ema_swing",
    "bollinger",
    "sar_trend",
    "multi_factor",
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


# 策略路线菜单：配置里 engine 填左边的名字
REGISTRY: dict[str, StrategyFactory] = {
    # 主推：开盘箱体收盘突破 + 极值/ATR 止损
    "daily_body": DailyBodyStrategy,
    # 等开盘噪音走完再定方向，突破沿止损，浮盈保本后拿到收盘
    "float_hold": FloatHoldStrategy,
    # 经典结构趋势
    "trend_follow": TrendFollowStrategy,
    "supertrend": SuperTrendStrategy,
    "st": SuperTrendStrategy,
    "dual_ma": DualMaStrategy,
    "ma_cross": DualMaStrategy,
    # 均线波段：界面名「均线波段」。macd_swing 是旧配置别名，不是已删的 macd。
    "ema_swing": MacdSwingStrategy,
    "macd_swing": MacdSwingStrategy,
    # 布林带趋势突破（不做轨道反转）
    "bollinger": BollingerBreakStrategy,
    "boll": BollingerBreakStrategy,
    # SAR 抛物转向波段（MA20 过滤，SAR 当移动止损）
    "sar_trend": SarTrendStrategy,
    "parabolic_sar": SarTrendStrategy,
    # 多因子 CTA 加权打分
    "multi_factor": MultiFactorStrategy,
    "cta": MultiFactorStrategy,
    # 大周期定方向 + 信号周期回调，支持有限次加仓与伸展减仓
    "swing_trend": SwingTrendStrategy,
    # 震荡箱子收盘突破后顺着拿趋势（信号/止损周期跟配置页）
    "swing_box": SwingBoxStrategy,
}


def _migrate_daily_body_atr_gates(body: dict[str, Any]) -> None:
    """
    把旧的跳数上限换成 ATR 倍数。

    已经写了倍数就只清掉旧键。旧跳数是 0 → 倍数 0（关掉）。
    旧跳数等于出厂值（50/100/20）→ 2.5 / 5.0 / 1.0。
    其它正跳数按「大约 20 跳 ≈ 1 倍 ATR」粗算，避免加载后闸门消失。
    """
    specs = (
        ("skip_gap_ticks", "skip_gap_atr_mult", 50, 2.5),
        ("max_box_ticks", "max_box_atr_mult", 100, 5.0),
        ("max_chase_ticks", "max_chase_atr_mult", 20, 1.0),
    )
    for old, new, factory_ticks, factory_mult in specs:
        raw_new = body.get(new)
        if raw_new not in (None, ""):
            try:
                body[new] = max(0.0, float(raw_new))
            except (TypeError, ValueError):
                body[new] = float(factory_mult)
            body.pop(old, None)
            continue
        if old not in body:
            continue
        try:
            ticks = int(0 if body.get(old) in (None, "") else body.get(old))
        except (TypeError, ValueError):
            ticks = int(factory_ticks)
        if ticks <= 0:
            body[new] = 0.0
        elif ticks == int(factory_ticks):
            body[new] = float(factory_mult)
        else:
            body[new] = max(0.0, round(float(ticks) / 20.0, 2))
        body.pop(old, None)


def format_atr_mult(value: Any, default: float = 0.0) -> str:
    """配置页显示 ATR 倍数：2.5 不写成 2.50，0 就是关掉。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    if number != number or number <= 0:
        return "0"
    text = f"{number:.4f}".rstrip("0").rstrip(".")
    return text or "0"


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
    # 跳空/箱宽/急破从「固定跳数」迁到 ATR 倍数，换品种时波动尺度一致。
    _migrate_daily_body_atr_gates(body)
    # 旧名 MACD波段 → 均线波段。配置节从 macd_swing 迁到 ema_swing。
    old_swing = st.get("macd_swing")
    ema_box = st.get("ema_swing")
    if isinstance(old_swing, dict):
        if not isinstance(ema_box, dict):
            ema_box = dict(old_swing)
            st["ema_swing"] = ema_box
        else:
            for key, value in old_swing.items():
                if key not in ema_box:
                    ema_box[key] = value
        st.pop("macd_swing", None)
    if isinstance(ema_box, dict):
        ema_box.pop("cluster_width", None)
        for dead in ("macd_fast", "macd_slow", "macd_signal", "macd_combo"):
            ema_box.pop(dead, None)
        if "dir_ema_fast" not in ema_box and ema_box.get("ema_fast") is not None:
            ema_box["dir_ema_fast"] = ema_box.get("ema_fast")
        if "dir_ema_slow" not in ema_box and ema_box.get("ema_slow") is not None:
            ema_box["dir_ema_slow"] = ema_box.get("ema_slow")
        # 旧配置只有止损均线：多空分界线、倾角先跟它，拆开后行为不变。
        if "side_ema" not in ema_box:
            ema_box["side_ema"] = ema_box.get("stop_ema", 120)
        if "slope_ema" not in ema_box:
            ema_box["slope_ema"] = ema_box.get("stop_ema", 120)

    hold = st.get("float_hold")
    if isinstance(hold, dict):
        _migrate_daily_body_atr_gates(hold)
        # 止损/保本已改成 ATR 宽度，旧的跳数和「几倍风险」不再读。
        hold.pop("stop_extra_ticks", None)
        hold.pop("be_r_mult", None)
        hold["direction"] = normalize_direction_key(hold.get("direction"))
        hold["macd_timeframe"] = normalize_macd_timeframe(hold.get("macd_timeframe"))

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

    # 旧配置若还把确认扩张写在日内实体小节里，清掉，不要改强单边上限。
    body.pop("filter_adx_expand", None)
    body.setdefault("filter_adx_upper", 30.0)

    # 多因子加权打分补一份行情闸门；缺的键才填，已经写过的数字不改。
    for section in ("multi_factor",):
        box = st.get(section)
        if isinstance(box, dict):
            ensure_cta_filter_box(box, section)

    # 引擎名别名收成正式名；已删的回退到日内实体。
    raw_engine = str(st.get("engine") or DEFAULT_ENGINE).strip().lower()
    engine = _ENGINE_ALIASES.get(raw_engine, raw_engine)
    if engine in _REMOVED_ENGINES or engine not in REGISTRY:
        engine = DEFAULT_ENGINE
    st["engine"] = engine

    # 交叉测试名单：丢掉已删名字。写过哪些就留哪些，空名单才填全部。
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
    if not cleaned_cross:
        cleaned_cross = list(_CROSS_ENGINE_ORDER)
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
