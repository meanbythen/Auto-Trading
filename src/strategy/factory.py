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

from .base import StrategyBase
from .cta_filter import ensure_cta_filter_box
from .market_state import (
    TREND_FILTER_SECTIONS,
    MarketFilterMode,
    ensure_trend_filter_defaults,
)
from .bollinger_break import BollingerBreakStrategy
from .daily_body import DailyBodyStrategy
from .kelly_break import KellyBreakStrategy
from .liq_stop import LiquidityStopStrategy
from .dual_ma import DualMaStrategy
from .macd_swing import MacdSwingStrategy
from .multi_factor import MultiFactorStrategy
from .params import DEFAULT_ENGINE, ENGINE_SECTION_DEFAULTS, StrategyParams
from .rule_dsl import RuleConfigError, RuleStrategy, rule_engine_stem
from .sar_trend import SarTrendStrategy
from .supertrend import SuperTrendStrategy
from .dual_donchian import DualDonchianStrategy
from .dual_fractal import DualFractalStrategy
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

    def note_flat_wait_next_bar(self, symbol: str, bar_id: Any = "") -> None: ...

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

# 主策略由 params 定义（DEFAULT_ENGINE），工厂继续导出同名常量，旧导入路径不变。

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
    # 47.0.0 删掉日内浮盈飘单（和日内实体、双均线、均线波段重复，实测难开仓且难盈利）
    "float_hold",
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

# 已删策略留在配置里的整节。除这份补充名单外，其余都来自 _REMOVED_ENGINES，不再各写一份。
_EXTRA_OBSOLETE_SECTIONS = {"rotation_scan"}
_OBSOLETE_STRATEGY_SECTIONS = set(_REMOVED_ENGINES) | _EXTRA_OBSOLETE_SECTIONS

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

# 对外公开的别名表：界面、回测、复盘、审计统一从这里取，不再各写一份。
ENGINE_ALIASES: dict[str, str] = dict(_ENGINE_ALIASES)

# 已删除的引擎名：配置里读到这些要明确提示并回退，不能静默当成别的策略。
REMOVED_ENGINES: set[str] = set(_REMOVED_ENGINES)


# 策略中文名（配置页 / 参数表 / 导出用）。别名不单列，统一走 ENGINE_ALIASES 换算。
ENGINE_LABELS: dict[str, str] = {
    "daily_body": "日内实体（主推）",
    "trend_follow": "趋势跟随",
    "supertrend": "SuperTrend趋势",
    "dual_ma": "双均线趋势",
    "ema_swing": "均线波段",
    "bollinger": "布林带突破",
    "liq_stop": "以损定开",
    "kelly_break": "凯利突破",
    "sar_trend": "SAR波段趋势",
    "multi_factor": "多因子CTA（加权打分）",
    "swing_trend": "长线趋势加减仓",
    "swing_box": "长线箱体突破",
    "dual_donchian": "双周期通道突破",
    "dual_fractal": "双周期回调分型",
    "rule_dsl": "自定义规则（DSL）",
}

# 选股交叉测试名单里的短名：同一策略不重复标「主推」。
ENGINE_SHORT_LABELS: dict[str, str] = {**ENGINE_LABELS, "daily_body": "日内实体"}

# 交叉测试可选内置策略的显示顺序。真正名单由 cross_engine_choices() 现算。
CROSS_TEST_ENGINE_ORDER: list[str] = [
    "daily_body",
    "trend_follow",
    "supertrend",
    "dual_ma",
    "ema_swing",
    "bollinger",
    "sar_trend",
    "multi_factor",
    "swing_trend",
    "swing_box",
    "dual_donchian",
    "dual_fractal",
]

# 配置页能选，但交叉测试不测：参数格子太大，一轮组合会爆。
CROSS_TEST_SKIP_ENGINES: frozenset[str] = frozenset({"liq_stop", "kelly_break"})

# 每个引擎盘前审计时该套哪种公共行情过滤。None = 不接公共过滤器。
# 和引擎名、中文名放在同一处，新增策略只改这个文件。
ENGINE_AUDIT_MODES: dict[str, MarketFilterMode | None] = {
    "daily_body": "range",
    "trend_follow": "trend",
    "supertrend": "trend",
    "st": "trend",          # 别名
    "dual_ma": "trend",
    "ma_cross": "trend",    # 别名
    "ema_swing": None,  # 只用均线斜率，不用公共 ADX 过滤
    "macd_swing": None,
    "bollinger": "trend",
    "boll": "trend",        # 别名
    "liq_stop": None,  # ADX/DI 过滤写在策略自己里，不接公共趋势过滤
    "kelly_break": None,  # 波动率阈值写在策略自己里，不接公共趋势过滤
    "sar_trend": "trend",
    "parabolic_sar": "trend",  # 别名
    "multi_factor": "trend",
    "cta": "trend",         # 别名
    "swing_trend": "trend",
    "swing_box": None,  # 箱子和 ADX 写在策略里
    "dual_donchian": None,  # ADX 写在大周期自己里
    "dual_fractal": None,
    "rule_dsl": None,  # 过滤条件写在用户规则文件里
}


# 策略路线菜单：配置里 engine 填左边的名字
REGISTRY: dict[str, StrategyFactory] = {
    # 主推：开盘箱体收盘突破 + 极值/ATR 止损
    "daily_body": DailyBodyStrategy,
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
    # 以损定开：公共止损区被击穿并回流后再顺势进场
    "liq_stop": LiquidityStopStrategy,
    # 凯利突破：极简高低点突破 + ATR 止损 + 固定盈亏比止盈
    "kelly_break": KellyBreakStrategy,
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
    # 大周期通道+ADX 定方向，小周期唐奇安突破（周期可改）
    "dual_donchian": DualDonchianStrategy,
    # 大周期均线或通道定方向，小周期强分型+DIF 背离（周期可改）
    "dual_fractal": DualFractalStrategy,
    # 用户自己写的指标条件组合（rules/*.yaml），不写代码就能加策略
    "rule_dsl": RuleStrategy,
}

# 只含正式引擎名（不含别名），顺序与注册表一致。审计/复盘按这个顺序列策略。
PRIMARY_ENGINES: list[str] = [name for name in REGISTRY if name not in _ENGINE_ALIASES]


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

    # 止损触发方式已从日内实体挪到 strategy 根，全策略共用；旧配置写在 daily_body 里，迁上来。
    legacy_stop_mode = body.pop("stop_mode", None)
    if legacy_stop_mode is not None and "stop_mode" not in st:
        st["stop_mode"] = legacy_stop_mode
    # 全策略共用参数（止损/开仓触发方式、通用止损）的出厂值只在 StrategyParams 里定义一次。
    StrategyParams().apply_defaults(st)
    # 公共行情过滤已拆进各策略。旧配置里的 market_filter 只用来给趋势策略补缺省。
    mf = st.get("market_filter") if isinstance(st.get("market_filter"), dict) else {}
    for section in TREND_FILTER_SECTIONS:
        box = st.get(section)
        if not isinstance(box, dict):
            box = {}
            st[section] = box
        ensure_trend_filter_defaults(box, mf)

    for key in list(_OBSOLETE_STRATEGY_KEYS):
        st.pop(key, None)
    for key in list(_OBSOLETE_STRATEGY_SECTIONS):
        st.pop(key, None)

    for section, defaults in ENGINE_SECTION_DEFAULTS.items():
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
    # rule_dsl:<文件名> 是配置页新增的自定义规则，原样保留，文件名由规则目录决定。
    raw_engine = str(st.get("engine") or DEFAULT_ENGINE).strip().lower()
    engine = raw_engine if rule_engine_stem(raw_engine) else _ENGINE_ALIASES.get(raw_engine, raw_engine)
    # 插件引擎也算合法；已删除和认不出的仍回退默认，避免保存后行为漂移。
    if engine in _REMOVED_ENGINES or not normalize_engine(engine):
        engine = DEFAULT_ENGINE
    st["engine"] = engine

    # 交叉测试勾选：丢掉已删名字和现在测不了的名字。写过哪些就留哪些，空名单才填内置全部。
    # 不把没勾过的新策略自动勾上，避免一轮组数突然变多。界面仍会列出新策略，让人自己勾。
    allowed = {code for code, _label in cross_engine_choices()}
    raw_cross = st.get("cross_engines")
    cleaned_cross: list[str] = []
    seen: set[str] = set()
    if isinstance(raw_cross, list):
        for item in raw_cross:
            name = str(item).strip().lower()
            name = _ENGINE_ALIASES.get(name, name)
            if name in _REMOVED_ENGINES or name not in allowed:
                continue
            if name in seen:
                continue
            seen.add(name)
            cleaned_cross.append(name)
    if not cleaned_cross:
        cleaned_cross = [code for code, _label in cross_engine_choices() if not rule_engine_stem(code)]
    st["cross_engines"] = cleaned_cross
    return cfg


def _plugins() -> Any:
    """延迟取插件清单模块。

    插件目录要读 REGISTRY，方向只能是 plugins → factory；这里用 importlib
    而不是 import 语句，保持单向依赖，不引入模块环。
    """
    import importlib

    return importlib.import_module(f"{__package__}.plugins")


def _plugin_registered(key: str) -> bool:
    """插件目录里是否有这个引擎；加载失败一律当作没有，不影响内置策略。"""
    if not key:
        return False
    try:
        return key in _plugins().discover_specs()
    except Exception as exc:  # noqa: BLE001
        logger.warning("策略插件清单加载失败，按内置注册表处理: {}", exc)
        return False


def normalize_engine(name: Any) -> str:
    """把别名/大小写收成正式引擎名；认不出返回空串。

    rule_dsl:<文件名> 是配置页里新建的自定义规则，只要前缀对就原样返回，
    文件在不在由 create_strategy 加载时报错（缺文件会走 _BlockedStrategy）。
    插件目录里的引擎也算正式名，保存配置时不会被重置。
    """
    key = str(name or "").strip().lower()
    if rule_engine_stem(key):
        return key
    key = _ENGINE_ALIASES.get(key, key)
    if key in REGISTRY:
        return key
    return key if _plugin_registered(key) else ""


def param_section(name: Any) -> str:
    """策略别名 → 参数小节名。未登记的名字原样返回，不额外造一节。"""
    key = str(name or "").strip().lower()
    return _ENGINE_ALIASES.get(key, key)


def engine_label(name: Any, *, short: bool = False) -> str:
    """引擎中文名。short=True 用交叉测试短名。认不出时原样返回，方便界面显示非法配置。"""
    table = ENGINE_SHORT_LABELS if short else ENGINE_LABELS
    raw = str(name or "").strip().lower()
    key = _ENGINE_ALIASES.get(raw, raw)
    label = table.get(key)
    if label:
        return label
    # 插件策略的中文名不在静态表里，从插件清单补。
    try:
        spec = _plugins().spec_for(key)
    except Exception:  # noqa: BLE001
        spec = None
    if spec is not None:
        return spec.short_label if short else spec.label
    return str(name or "")


def engine_label_map(*, short: bool = False) -> dict[str, str]:
    """中文名表：正式名和别名都指向同一个中文名。界面、复盘、审计共用这一份。"""
    table = ENGINE_SHORT_LABELS if short else ENGINE_LABELS
    labels = dict(table)
    for alias, target in _ENGINE_ALIASES.items():
        if target in table:
            labels[alias] = table[target]
    try:
        for code, spec in _plugins().discover_specs().items():
            if spec.source == "plugin":
                labels[code] = spec.short_label if short else spec.label
    except Exception:  # noqa: BLE001
        pass
    return labels


def cross_engine_choices() -> list[tuple[str, str]]:
    """交叉测试当前可选策略（代号, 中文名）。

    每次现算，不拍死一份名单：注册表里还在的正式引擎会列出，已经删掉的立刻没了。
    策略编辑器里的自定义规则也会跟上。以损定开、凯利突破不参加交叉测试。
    """
    from .rule_dsl import list_rule_files, rule_engine_code

    skip = set(CROSS_TEST_SKIP_ENGINES) | {"rule_dsl"} | set(_ENGINE_ALIASES) | set(_REMOVED_ENGINES)
    seen: set[str] = set()
    out: list[tuple[str, str]] = []

    def _add(code: str, label: str | None = None) -> None:
        # 同一套策略只出现一次；别名、已删、明确跳过的都不进名单。
        if not code or code in seen or code in skip:
            return
        seen.add(code)
        out.append((code, label or ENGINE_SHORT_LABELS.get(code, code)))

    for code in CROSS_TEST_ENGINE_ORDER:
        if code in REGISTRY:
            _add(code)
    for code in PRIMARY_ENGINES:
        _add(code)
    for info in list_rule_files():
        stem = str(info.get("stem") or "").strip()
        if not stem:
            continue
        title = str(info.get("name") or stem).strip() or stem
        _add(rule_engine_code(stem), f"自定义：{title}")
    return out


def list_engines() -> list[str]:
    """返回可供配置的主引擎名（不含纯别名重复时仍全部列出，便于对照）。"""
    return sorted(REGISTRY.keys())


class _BlockedStrategy(StrategyBase):
    """
    规则文件不合法时的占位策略：只发 HOLD，并把原因写进告警。

    宁可一整天不开仓，也不能静默换成别的策略——用户以为在跑自己的规则，
    实际却在跑双均线，这种错误比不交易严重得多。
    """

    def __init__(self, cfg: dict[str, Any], reason: str) -> None:
        super().__init__()
        st = cfg.get("strategy") or {}
        trade = cfg.get("trade") or {}
        self.reason = str(reason)
        self.timeframe = str(st.get("timeframe", "15"))
        self.trail_timeframe = self.timeframe
        self.symbols: list[str] = list(st.get("symbols") or [trade.get("symbol", "bu2612")])
        self.flatten_at_close = bool(st.get("flatten_at_close", True))
        self.stop_mode = "close"
        self.stop_on_touch = False
        self.entry_mode = "close"

    def is_flatten_time(self, now: Any = None) -> bool:
        return False

    def evaluate(self, symbol: str, bars: Any, now: Any = None, trail_bars: Any = None) -> StrategyDecision:
        _ = (bars, now, trail_bars)
        return StrategyDecision(
            StrategyAction.HOLD,
            symbol,
            f"自定义规则不可用，已停止开仓：{self.reason}",
        )

    def on_filled(
        self,
        symbol: str,
        action: StrategyAction,
        price: float,
        volume: int,
        initial_stop: float | None = None,
    ) -> None:
        _ = (symbol, action, price, volume, initial_stop)


def _create_plugin_strategy(
    name: str, cfg: dict[str, Any]
) -> StrategyLike | None:
    """插件目录里找同名工厂；加载或参数检查失败都返回 None，让调用方回退。"""
    try:
        plugins = _plugins()
        factory = plugins.plugin_factory(name, cfg)
        if factory is None:
            return None
        strategy = factory(cfg)
        section = param_section(name)
        raw_params = (cfg.get("strategy") or {}).get(section)
        for message in plugins.validate_params(name, raw_params if isinstance(raw_params, dict) else {}):
            logger.warning("策略参数类型不匹配: {}", message)
        return strategy  # type: ignore[no-any-return]
    except Exception as exc:  # noqa: BLE001
        logger.warning("策略插件 {} 加载失败，回退到 {}: {}", name, DEFAULT_ENGINE, exc)
        return None


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

    stem = rule_engine_stem(name)
    factory = REGISTRY.get("rule_dsl" if stem else name)
    plugin_strategy: StrategyLike | None = None
    if factory is None and not stem:
        plugin_strategy = _create_plugin_strategy(name, cfg)
    if factory is None and plugin_strategy is None:
        logger.warning(
            "未知 strategy.engine={}，可选: {}；回退到 {}",
            name,
            ", ".join(list_engines()),
            DEFAULT_ENGINE,
        )
        name = DEFAULT_ENGINE
        factory = REGISTRY[DEFAULT_ENGINE]
        stem = None

    if plugin_strategy is not None:
        strategy = plugin_strategy
    else:
        try:
            strategy = RuleStrategy(cfg, rule_path=f"rules/{stem}.yaml") if stem else factory(cfg)
        except RuleConfigError as exc:
            # 规则写错时只停这一套策略，不换别的策略：用户以为在跑自己的规则，
            # 实际跑成双均线比一天不交易更危险。
            message = f"自定义规则加载失败，已停止开仓：{exc}"
            logger.error("{}", message)
            try:
                from src.ops.alerts import emit_alert

                emit_alert("rule_dsl", message, level="error")
            except Exception as alert_exc:  # noqa: BLE001
                logger.warning("规则错误告警写入失败: {}", alert_exc)
            strategy = _BlockedStrategy(cfg, str(exc))
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


def strategy_identity(cfg: dict[str, Any]) -> dict[str, str]:
    """回测/实验留痕用：策略代号、中文名、版本。插件清单不可用时版本留空。"""
    identity: dict[str, str] = _plugins().strategy_identity(cfg)
    return identity
