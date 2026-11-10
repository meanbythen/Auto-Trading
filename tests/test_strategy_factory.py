"""策略 / 执行通道注册表测试。"""

from src.strategy.daily_body import DailyBodyStrategy
from src.strategy.factory import DEFAULT_ENGINE, create_strategy, list_engines
from src.trader.factory import create_trader, list_executors
from src.trader.sim_trader import SimTrader


def _cfg(engine: str = "daily_body", executor: str = "sim") -> dict:
    return {
        "_project_root": ".",
        "strategy": {
            "engine": engine,
            "timeframe": "15",
            "symbols": ["bu"],
            "flatten_times": ["14:55", "22:55"],
            "daily_body": {
                "eth_minutes": 30,
            },
        },
        "trade": {"executor": executor, "volume": 1, "symbol": "bu"},
        "schedule": {"trading_sessions": [["09:00", "11:30"]]},
        "dry_run": True,
    }


def test_list_engines_contains_core() -> None:
    names = list_engines()
    assert "daily_body" in names
    assert "liquidity_exhaustion" not in names
    assert "morning_box_a" in names
    assert "trend_follow" in names
    assert "supertrend" in names
    assert "st" in names
    assert "dual_ma" in names
    assert "ma_cross" in names
    # 已删除的引擎不应再出现在可选名单里
    assert "morning_gap_c" not in names
    assert "morning_c" not in names
    assert "sar_flip" not in names
    assert "sar" not in names
    assert "mean_reversion" not in names
    assert "macd_divergence" not in names
    assert "liquidity_signs" not in names
    assert "fvg" not in names
    assert "custom" not in names
    assert "reversal_pivot" not in names
    assert "pinbar_sar" not in names
    assert "price_action" not in names
    assert "ema_pin_momentum" not in names
    assert "intraday_breakout" not in names
    assert "jesse_casper" in names
    assert "casper" in names


def test_removed_liquidity_exhaustion_falls_back() -> None:
    """流动性耗尽已删除：旧配置写这个名字时回退到日内实体。"""
    s = create_strategy(_cfg("liquidity_exhaustion"))
    assert isinstance(s, DailyBodyStrategy)


def test_create_trend_follow() -> None:
    """趋势跟随是单独的一套，不能掉回日内实体。"""
    from src.strategy.trend_follow import TrendFollowStrategy

    strategy = create_strategy(_cfg("trend_follow"))
    assert isinstance(strategy, TrendFollowStrategy)


def test_create_dual_ma() -> None:
    """双均线趋势是单独的一套。"""
    from src.strategy.dual_ma import DualMaStrategy

    strategy = create_strategy(_cfg("dual_ma"))
    assert isinstance(strategy, DualMaStrategy)
    assert isinstance(create_strategy(_cfg("ma_cross")), DualMaStrategy)


def test_create_daily_body() -> None:
    """日内实体是默认主策略。"""
    strategy = create_strategy(_cfg("daily_body"))
    assert type(strategy) is DailyBodyStrategy


def test_removed_mean_reversion_falls_back() -> None:
    """均值回归已删除：旧配置写这个名字时回退到日内实体。"""
    s = create_strategy(_cfg("mean_reversion"))
    assert isinstance(s, DailyBodyStrategy)


def test_create_morning_engines() -> None:
    """早盘箱体 A 保留；缺口 C / 旧 SAR 翻转策略已删除。"""
    from src.strategy.morning_edge import MorningBoxAStrategy

    expected = {
        "morning_box_a": MorningBoxAStrategy,
        "morning_a": MorningBoxAStrategy,
    }
    for name, cls in expected.items():
        strategy = create_strategy(_cfg(name))
        assert type(strategy) is cls

    # 「sar」现在是新加的 SAR 波段（sar_trend）的别名，不再是已删除的 sar_flip。
    for gone in ("morning_gap_c", "morning_c", "sar_flip"):
        assert isinstance(create_strategy(_cfg(gone)), DailyBodyStrategy)


def test_create_new_trend_engines() -> None:
    """新加的三套趋势策略能按正式名和别名创建。"""
    from src.strategy.bollinger_break import BollingerBreakStrategy
    from src.strategy.multi_factor import (
        MultiFactorStrategy,
        MultiHybridStrategy,
        MultiVoteStrategy,
    )
    from src.strategy.multi_factor_adapt import MultiFactorAdaptStrategy
    from src.strategy.sar_trend import SarTrendStrategy

    expected = {
        "bollinger": BollingerBreakStrategy,
        "boll": BollingerBreakStrategy,
        "sar_trend": SarTrendStrategy,
        "parabolic_sar": SarTrendStrategy,
        "multi_factor": MultiFactorStrategy,
        "cta": MultiFactorStrategy,
        "multi_factor_adapt": MultiFactorAdaptStrategy,
        "multi_vote": MultiVoteStrategy,
        "vote": MultiVoteStrategy,
        "multi_hybrid": MultiHybridStrategy,
        "hybrid": MultiHybridStrategy,
    }
    for name, cls in expected.items():
        assert type(create_strategy(_cfg(name))) is cls


def test_removed_engine_falls_back_to_daily_body() -> None:
    """已删除的引擎名应明确回退到日内实体，而不是静默变成别的策略。"""
    s = create_strategy(_cfg("macd_divergence"))
    assert isinstance(s, DailyBodyStrategy)
    assert DEFAULT_ENGINE == "daily_body"


def test_unknown_engine_falls_back() -> None:
    s = create_strategy(_cfg("not_exist_xyz"))
    assert isinstance(s, DailyBodyStrategy)
    assert DEFAULT_ENGINE == "daily_body"


def test_custom_placeholder_falls_back() -> None:
    s = create_strategy(_cfg("custom"))
    assert isinstance(s, DailyBodyStrategy)


def test_executor_registry() -> None:
    assert "sim" in list_executors()
    assert "gui" in list_executors()
    assert "ctp" in list_executors()
    t = create_trader(_cfg(executor="sim"))
    assert isinstance(t, SimTrader)


def test_all_strategies_read_the_shared_stop_mode() -> None:
    """止损触发方式是 strategy 根上的公共设置，每套策略都照它判断。"""
    engines = (
        "daily_body",
        "trend_follow",
        "morning_box_a",
        "supertrend",
        "dual_ma",
        "swing_trend",
        "jesse_casper",
    )
    for engine in engines:
        cfg = _cfg(engine)
        cfg["strategy"]["stop_mode"] = "close"
        strategy = create_strategy(cfg)
        assert getattr(strategy, "stop_mode") == "close", engine
        assert getattr(strategy, "stop_on_touch") is False, engine


def test_sanitize_strategy_config_drops_dead_and_fills_defaults() -> None:
    """旧小节删掉；ETH 迁到日内实体；现有策略缺的键补上。"""
    from src.strategy.factory import sanitize_strategy_config

    cfg = {
        "strategy": {
            "engine": "liquidity_exhaustion",
            "cross_engines": ["liquidity_exhaustion", "daily_body", "sar_flip"],
            "sar_af_start": 0.01,
            "wick_body_ratio": 2.0,
            "liquidity_exhaustion": {"eth_minutes": 25},
            "mean_reversion": {"lookback": 20},
            "daily_body": {"initial_atr_mult": 1.0, "stop_mode": "close"},
        }
    }
    sanitize_strategy_config(cfg)
    st = cfg["strategy"]
    assert st["engine"] == "daily_body"
    assert "liquidity_exhaustion" not in st
    assert "mean_reversion" not in st
    assert "sar_af_start" not in st
    assert "wick_body_ratio" not in st
    assert st["daily_body"]["eth_minutes"] == 25
    # 止损触发方式已挪到 strategy 根（全策略共用），旧键迁上来并删掉。
    assert st["stop_mode"] == "close"
    assert "stop_mode" not in st["daily_body"]
    # 止损改为箱沿极值后，这两个旧参数不再保留。
    assert "initial_atr_mult" not in st["daily_body"]
    assert "reverse_atr_mult" not in st["daily_body"]
    assert "supertrend" in st and "multiplier" in st["supertrend"]
    assert "morning_box_a" in st
    assert "liquidity_exhaustion" not in st["cross_engines"]
    assert "sar_flip" not in st["cross_engines"]
    assert "daily_body" in st["cross_engines"]
    assert "trend_follow" in st["cross_engines"]


def test_sanitize_fills_default_shared_stop_mode() -> None:
    """旧配置没写过止损触发方式时，补默认的触及即止损。"""
    from src.strategy.factory import sanitize_strategy_config

    cfg = {"strategy": {"engine": "daily_body", "daily_body": {}}}
    sanitize_strategy_config(cfg)
    assert cfg["strategy"]["stop_mode"] == "touch"
