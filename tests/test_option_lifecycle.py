"""期权到期生命周期与策略注册。"""

from datetime import date, datetime

import pandas as pd

from src.account.option_lifecycle import (
    assert_tradable,
    days_to_expiry,
    force_close_reason,
    parse_expiry,
)
from src.strategy import create_strategy
from src.strategy.option_long import OptionLongStrategy
from src.strategy.types import StrategyAction


def _cfg(expiry: str = "2027-01-15") -> dict:
    return {
        "strategy": {
            "engine": "option_long",
            "symbols": ["rb2601C3500"],
            "timeframe": "5",
            "option_long": {"lookback": 20, "premium_stop_pct": 30},
        },
        "risk": {
            "contracts": {
                "rb2601C3500": {
                    "multiplier": 10,
                    "margin_rate_pct": 12,
                    "price_tick": 0.5,
                    "strike": 3500,
                    "expiry": expiry,
                }
            }
        },
        "trade": {"volume": 1},
    }


def test_parse_expiry_formats() -> None:
    """YYYY-MM-DD / YYYYMMDD / YYYY-MM / YYMM 都要认。"""
    assert parse_expiry("2027-01-15") == date(2027, 1, 15)
    assert parse_expiry("20270115") == date(2027, 1, 15)
    assert parse_expiry("2027-02") == date(2027, 2, 28)
    assert parse_expiry("2602") == date(2026, 2, 28)
    assert parse_expiry("") is None


def test_assert_tradable_by_expiry() -> None:
    """缺到期日拒开；最后交易日只平不开；之前正常。"""
    cfg = _cfg()
    assert assert_tradable(cfg, "rb2601C3500", date(2027, 1, 14))[0]
    ok, why = assert_tradable(cfg, "rb2601C3500", date(2027, 1, 15))
    assert not ok and "只平不开" in why
    missing = _cfg(expiry="")
    ok, why = assert_tradable(missing, "rb2601C3500", date(2027, 1, 10))
    assert not ok and "没有到期日" in why
    # 非期权不受影响
    assert assert_tradable(cfg, "rb2701", date(2027, 1, 15))[0]


def test_force_close_and_days() -> None:
    """到期日强平；剩余天数从最后交易日起算。"""
    cfg = _cfg()
    assert days_to_expiry(cfg, "rb2601C3500", date(2027, 1, 10)) == 5
    assert force_close_reason(cfg, "rb2601C3500", date(2027, 1, 15))
    assert force_close_reason(cfg, "rb2601C3500", date(2027, 1, 14)) is None


def test_option_engines_registered() -> None:
    """两个期权引擎都能从工厂创建。"""
    assert create_strategy(_cfg()).__class__ is OptionLongStrategy
    hedge_cfg = _cfg()
    hedge_cfg["strategy"]["engine"] = "option_hedge"
    assert create_strategy(hedge_cfg).__class__.__name__ == "OptionHedgeStrategy"


def _bars(n: int = 30) -> pd.DataFrame:
    idx = pd.date_range("2027-01-01 09:05", periods=n, freq="5min")
    close = [100 + i for i in range(n)]
    return pd.DataFrame(
        {
            "open": close,
            "high": [c + 0.5 for c in close],
            "low": [c - 0.5 for c in close],
            "close": close,
            "volume": [10] * n,
        },
        index=idx,
    )


def test_strategy_buys_breakout_then_expiry_close() -> None:
    """权利金突破开多；到期日 evaluate 直接给强平。"""
    strategy = OptionLongStrategy(_cfg())
    decision = strategy.evaluate("rb2601C3500", _bars())
    assert decision.action is StrategyAction.BUY_OPEN
    assert decision.entry_price == 129.0
    assert decision.stop_price < decision.entry_price

    strategy.on_filled(
        "rb2601C3500", decision.action, decision.entry_price, 1, decision.stop_price
    )
    closing = strategy.evaluate(
        "rb2601C3500", _bars(), now=datetime(2027, 1, 15, 10, 0)
    )
    assert closing.action is StrategyAction.CLOSE
    assert "强制平仓" in closing.reason
