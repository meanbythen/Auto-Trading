"""策略 / 执行通道注册表测试。"""

from src.strategy.base import StrategyBase
from src.strategy.daily_body import DailyBodyStrategy
from src.strategy.swing_box import SwingBoxStrategy
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


def test_engine_metadata_comes_from_one_registry() -> None:
    """引擎名、中文名、审计模式、复盘下拉都从策略工厂这一份注册表来。"""
    from src.review.replay import ENGINE_BY_LABEL, LABEL_BY_ENGINE
    from src.strategy.factory import (
        CROSS_TEST_ENGINE_ORDER,
        ENGINE_AUDIT_MODES,
        ENGINE_ALIASES,
        ENGINE_LABELS,
        PRIMARY_ENGINES,
        engine_label_map,
    )
    from src.strategy.session_audit import ENGINE_NAMES

    labels = engine_label_map()
    for code in PRIMARY_ENGINES:
        assert code in ENGINE_LABELS, f"{code} 缺少中文名"
        assert code in ENGINE_AUDIT_MODES, f"{code} 缺少审计模式"
    for alias, target in ENGINE_ALIASES.items():
        assert alias in labels and alias in ENGINE_NAMES, f"{alias} 缺少中文名"
        assert labels[alias] == ENGINE_LABELS[target]
    assert ENGINE_NAMES == labels

    # 复盘下拉和工厂用同一份名单，中文名可以反查回引擎。
    for code in CROSS_TEST_ENGINE_ORDER:
        assert ENGINE_BY_LABEL[ENGINE_LABELS[code]] == code
        assert code in LABEL_BY_ENGINE
    assert "只看盘面（不套策略）" in ENGINE_BY_LABEL


def test_list_engines_contains_core() -> None:
    names = list_engines()
    assert "daily_body" in names
    assert "float_hold" not in names
    assert "daily_body_expand" not in names
    assert "swing_box" in names
    assert "dual_donchian" in names
    assert "dual_fractal" in names
    assert "liquidity_exhaustion" not in names
    assert "morning_box_a" not in names
    assert "trend_follow" in names
    assert "supertrend" in names
    assert "st" in names
    assert "dual_ma" in names
    assert "ma_cross" in names
    assert "ema_swing" in names
    assert "macd_swing" in names
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
    assert "jesse_casper" not in names
    assert "casper" not in names


def test_removed_liquidity_exhaustion_falls_back() -> None:
    """流动性耗尽已删除：旧配置写这个名字时回退到日内实体。"""
    s = create_strategy(_cfg("liquidity_exhaustion"))
    assert isinstance(s, DailyBodyStrategy)


def test_rule_dsl_engine_code_loads_its_own_file() -> None:
    """配置页里每条自定义规则都是独立引擎：rule_dsl:<文件名> 按代号找 rules/<文件名>.yaml。"""
    from src.strategy.rule_dsl import RuleStrategy

    cfg = _cfg("rule_dsl:turtle_adx")
    # 就算旧配置里还留着别的 rule_dsl.file，也应该按引擎代号加载。
    cfg["strategy"]["rule_dsl"] = {"file": "rules/dual_ma_equiv.yaml"}
    strategy = create_strategy(cfg)
    assert isinstance(strategy, RuleStrategy)
    assert strategy.rule.name == "海龟唐奇安+ADX"


def test_rule_dsl_engine_code_survives_sanitize() -> None:
    """保存配置时不能把自定义规则名当成未知策略改回日内实体。"""
    from src.strategy.factory import normalize_engine, sanitize_strategy_config

    assert normalize_engine("rule_dsl:turtle_adx") == "rule_dsl:turtle_adx"
    assert normalize_engine("rule_dsl:") == ""
    cfg = _cfg("rule_dsl:Turtle_ADX")
    sanitize_strategy_config(cfg)
    assert cfg["strategy"]["engine"] == "rule_dsl:turtle_adx"


def test_removed_jesse_casper_falls_back() -> None:
    """Jesse Casper 已删除：旧配置写这个名字时回退到日内实体。"""
    for name in ("jesse_casper", "casper", "jesse"):
        s = create_strategy(_cfg(name))
        assert isinstance(s, DailyBodyStrategy), name


def test_removed_multi_factor_adapt_falls_back() -> None:
    """多因子自适应权重已删除：旧配置写这个名字时回退到日内实体。加权打分不受影响。"""
    s = create_strategy(_cfg("multi_factor_adapt"))
    assert isinstance(s, DailyBodyStrategy)


def test_removed_multi_hybrid_falls_back() -> None:
    """多因子混合已删除：旧配置写这个名字时回退到日内实体。加权打分不受影响。"""
    for name in ("multi_hybrid", "hybrid"):
        s = create_strategy(_cfg(name))
        assert isinstance(s, DailyBodyStrategy), name


def test_removed_multi_vote_falls_back() -> None:
    """多因子投票已删除：旧配置写这个名字时回退到日内实体。加权打分不受影响。"""
    for name in ("multi_vote", "vote"):
        s = create_strategy(_cfg(name))
        assert isinstance(s, DailyBodyStrategy), name


def test_removed_daily_body_expand_falls_back() -> None:
    """日内实体·扩张已删除：旧配置写这个名字时回退到日内实体。日内实体本身不受影响。"""
    s = create_strategy(_cfg("daily_body_expand"))
    assert isinstance(s, DailyBodyStrategy)
    assert type(s) is DailyBodyStrategy


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


def test_create_ema_swing() -> None:
    """均线波段是单独的一套，不能掉回日内实体，也不能当成已删的 macd。旧名 macd_swing 仍能打开。"""
    from src.strategy.ema_swing import EmaSwingStrategy

    strategy = create_strategy(_cfg("ema_swing"))
    assert isinstance(strategy, EmaSwingStrategy)
    assert isinstance(create_strategy(_cfg("macd_swing")), EmaSwingStrategy)
    assert not strategy.flatten_at_close
    assert strategy.fast_ma == 60
    assert strategy.slow_ma == 120
    assert strategy.sig_c == 30
    assert strategy.stop_ema == 120
    assert strategy.side_ema == 120
    assert strategy.slope_ema == 120
    assert not hasattr(strategy, "macd_fast")
    assert not hasattr(strategy, "cluster_width")
    assert strategy.slope_min_deg == 3.0
    assert strategy.slope_lookback == 20
    assert strategy.no_open_before_close_min == 30
    assert strategy.slope_exit is False


def test_create_daily_body() -> None:
    """日内实体是默认主策略。"""
    strategy = create_strategy(_cfg("daily_body"))
    assert type(strategy) is DailyBodyStrategy


def test_removed_float_hold_falls_back() -> None:
    """日内浮盈飘单已删除：旧配置写这个名字时回退到日内实体。"""
    s = create_strategy(_cfg("float_hold"))
    assert isinstance(s, DailyBodyStrategy)


def test_cross_engine_choices_follows_live_registry() -> None:
    """交叉测试名单跟当前注册表走：已删的没有，新内置会排在后面，跳过的两套不测。"""
    from src.strategy.factory import (
        CROSS_TEST_ENGINE_ORDER,
        CROSS_TEST_SKIP_ENGINES,
        PRIMARY_ENGINES,
        REGISTRY,
        cross_engine_choices,
    )
    from src.strategy.rule_dsl import RULE_ENGINE_PREFIX

    items = cross_engine_choices()
    codes = [code for code, _label in items]
    assert codes == list(dict.fromkeys(codes))
    assert "float_hold" not in codes
    assert "liq_stop" not in codes
    assert "kelly_break" not in codes
    assert "daily_body" in codes
    builtins = [code for code in codes if not str(code).startswith(RULE_ENGINE_PREFIX)]
    expected = [code for code in CROSS_TEST_ENGINE_ORDER if code in REGISTRY]
    extra = [
        code
        for code in PRIMARY_ENGINES
        if code not in expected
        and code not in CROSS_TEST_SKIP_ENGINES
        and code != "rule_dsl"
    ]
    assert builtins == expected + extra
    for code in CROSS_TEST_SKIP_ENGINES:
        assert code not in codes


def test_create_swing_box() -> None:
    """长线箱体突破是单独一套，周期跟公共信号/止损周期，默认不收盘清仓。"""
    strategy = create_strategy(_cfg("swing_box"))
    assert isinstance(strategy, SwingBoxStrategy)
    assert strategy.timeframe == "15"
    assert strategy.trail_timeframe == "15"
    assert strategy.flatten_at_close is False


def test_removed_mean_reversion_falls_back() -> None:
    """均值回归已删除：旧配置写这个名字时回退到日内实体。"""
    s = create_strategy(_cfg("mean_reversion"))
    assert isinstance(s, DailyBodyStrategy)


def test_create_morning_engines() -> None:
    """早盘箱体 A / 缺口 C / 旧 SAR 翻转策略已删除，旧配置回退到日内实体。"""
    for gone in (
        "morning_box_a",
        "morning_a",
        "morning_gap_c",
        "morning_c",
        "sar_flip",
    ):
        assert isinstance(create_strategy(_cfg(gone)), DailyBodyStrategy)


def test_create_new_trend_engines() -> None:
    """新加的三套趋势策略能按正式名和别名创建。"""
    from src.strategy.bollinger_break import BollingerBreakStrategy
    from src.strategy.multi_factor import (
        MultiFactorStrategy,
    )
    from src.strategy.sar_trend import SarTrendStrategy

    expected = {
        "bollinger": BollingerBreakStrategy,
        "boll": BollingerBreakStrategy,
        "sar_trend": SarTrendStrategy,
        "parabolic_sar": SarTrendStrategy,
        "multi_factor": MultiFactorStrategy,
        "cta": MultiFactorStrategy,
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
        "supertrend",
        "dual_ma",
        "ema_swing",
        "swing_trend",
        "swing_box",
    )
    for engine in engines:
        cfg = _cfg(engine)
        cfg["strategy"]["stop_mode"] = "close"
        strategy = create_strategy(cfg)
        assert getattr(strategy, "stop_mode") == "close", engine
        assert getattr(strategy, "stop_on_touch") is False, engine


def test_all_strategies_share_the_public_bar_state_contract() -> None:
    """所有引擎都有同一套信号去重和开仓 K 线公开接口。"""
    engines = (
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
    )
    for engine in engines:
        strategy = create_strategy(_cfg(engine))
        assert isinstance(strategy, StrategyBase), engine
        assert strategy.get_signaled_bar("bu") == "", engine
        assert strategy.get_last_entry_bar("bu") == "", engine
        strategy.note_flat_wait_next_bar("bu", "bar-flat")
        assert strategy.get_signaled_bar("bu") == "bar-flat", engine
        assert strategy.get_last_entry_bar("bu") == "bar-flat", engine


def test_public_bar_state_methods_cover_release_snapshot_and_restore() -> None:
    """实盘失败重试、状态保存恢复和回测记录都只走公开方法。"""
    strategy = create_strategy(_cfg("trend_follow"))
    strategy.set_signaled_bar("bu", "bar-1")
    assert strategy.get_signaled_bar("bu") == "bar-1"
    assert strategy.clear_signaled_bar("bu", expected_bar_id="bar-0") is False
    assert strategy.get_signaled_bar("bu") == "bar-1"
    assert strategy.clear_signaled_bar("bu", expected_bar_id="bar-1") is True
    assert strategy.get_signaled_bar("bu") == ""

    strategy.set_last_entry_bar("bu", "bar-1")
    snapshot = strategy.get_last_entry_bars()
    snapshot["bu"] = "changed-elsewhere"
    assert strategy.get_last_entry_bar("bu") == "bar-1"
    strategy.restore_last_entry_bars({"bu": "bar-2", "cu": "bar-3"})
    assert strategy.get_last_entry_bar("bu") == "bar-2"
    assert strategy.get_last_entry_bar("cu") == "bar-3"


def test_sanitize_strategy_config_drops_dead_and_fills_defaults() -> None:
    """旧小节删掉；ETH 迁到日内实体；现有策略缺的键补上。"""
    from src.strategy.factory import sanitize_strategy_config

    cfg = {
        "strategy": {
            "engine": "liquidity_exhaustion",
            "cross_engines": [
                "liquidity_exhaustion",
                "daily_body",
                "sar_flip",
                "morning_box_a",
            ],
            "sar_af_start": 0.01,
            "wick_body_ratio": 2.0,
            "liquidity_exhaustion": {"eth_minutes": 25},
            "mean_reversion": {"lookback": 20},
            "jesse_casper": {"lookback_liq": 20},
            "multi_factor_adapt": {"lookback_trades": 20},
            "multi_hybrid": {"min_position_ratio": 0.5},
            "daily_body": {"initial_atr_mult": 1.0, "stop_mode": "close"},
        }
    }
    sanitize_strategy_config(cfg)
    st = cfg["strategy"]
    assert st["engine"] == "daily_body"
    assert "liquidity_exhaustion" not in st
    assert "mean_reversion" not in st
    assert "jesse_casper" not in st
    assert "multi_factor_adapt" not in st
    assert "multi_hybrid" not in st
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
    assert "float_hold" not in st
    assert "morning_box_a" not in st
    assert "liquidity_exhaustion" not in st["cross_engines"]
    assert "morning_box_a" not in st["cross_engines"]
    assert "sar_flip" not in st["cross_engines"]
    assert "daily_body" in st["cross_engines"]
    # 写过哪些策略就留哪些，不再自动把没勾的补回来。
    assert "daily_body_expand" not in st["cross_engines"]
    assert "daily_body_expand" not in st
    assert "trend_follow" not in st["cross_engines"]
    assert st["daily_body"].get("filter_adx_upper") == 30.0
    assert "filter_adx_expand" not in st["daily_body"]
    assert st["swing_box"]["lookback"] == 40


def test_sanitize_fills_default_shared_stop_mode() -> None:
    """旧配置没写过止损触发方式时，补默认的触及即止损。"""
    from src.strategy.factory import sanitize_strategy_config

    cfg = {"strategy": {"engine": "daily_body", "daily_body": {}}}
    sanitize_strategy_config(cfg)
    assert cfg["strategy"]["stop_mode"] == "touch"
    assert cfg["strategy"]["daily_body"]["skip_gap_atr_mult"] == 2.5
    assert cfg["strategy"]["daily_body"]["max_box_atr_mult"] == 5.0
    assert cfg["strategy"]["daily_body"]["max_chase_atr_mult"] == 1.0


def test_sanitize_migrates_tick_gates_to_atr_mult() -> None:
    """旧跳数：0 关掉，出厂 50/100/20 换成 2.5/5/1，其它按大约 20 跳=1 倍。"""
    from src.strategy.factory import sanitize_strategy_config

    cfg = {
        "strategy": {
            "engine": "daily_body",
            "daily_body": {
                "skip_gap_ticks": 50,
                "max_box_ticks": 0,
                "max_chase_ticks": 40,
            },
        }
    }
    sanitize_strategy_config(cfg)
    body = cfg["strategy"]["daily_body"]
    assert body["skip_gap_atr_mult"] == 2.5
    assert body["max_box_atr_mult"] == 0.0
    assert body["max_chase_atr_mult"] == 2.0
    assert "skip_gap_ticks" not in body
    assert "max_box_ticks" not in body
    assert "max_chase_ticks" not in body
