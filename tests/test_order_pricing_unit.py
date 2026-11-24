"""CTP 定价与滑点闸门的单元测试（白盒，纯函数 + 轻量 Mixin）。"""

from __future__ import annotations

import pandas as pd
import pytest

from src.scheduler.order_pricing import (
    OrderPricingMixin,
    ctp_opponent_limit,
    entry_slip_blocked,
)
from src.strategy import StrategyAction, StrategyDecision


class _FakeMd:
    def __init__(self, row=None, wanted=None, latest=None):
        self._row = row
        self.wanted_symbols = wanted
        self.latest = latest

    def latest_for(self, symbol):
        return self._row


class _FakeGateway:
    def __init__(self, md=None, instrument=None):
        self.md = md
        self._instrument = instrument


class _FakeTrader:
    def __init__(self, gateway=None, md=None):
        if gateway is not None:
            self.gateway = gateway
        if md is not None:
            self.md = md


class _FakeKline:
    def __init__(self, latest=0.0, df=None, raise_latest=False):
        self._latest = latest
        self._df = df
        self._raise_latest = raise_latest

    def latest_price(self, symbol):
        if self._raise_latest:
            raise RuntimeError("quote down")
        return self._latest

    def fetch(self, symbol, period="5"):
        return self._df


class _Pricing(OrderPricingMixin):
    def __init__(self, cfg, *, trader=None, kline=None, strategy=None):
        self.cfg = cfg
        self.trader = trader
        self.kline = kline or _FakeKline()
        self.strategy = strategy
        self.events = []

    def _record_event(self, name, **kwargs):
        self.events.append((name, kwargs))


def _decision(action, *, symbol="bu2612", entry=100.0, stop=95.0, meta=None):
    return StrategyDecision(
        action=action,
        symbol=symbol,
        reason="unit",
        entry_price=entry,
        stop_price=stop,
        meta=meta or {},
    )


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (dict(is_buy=True, last=100, ask=100, tick=1, extra_ticks=2), 102.0),
        (dict(is_buy=True, last=100, tick=1, extra_ticks=2), 102.0),
        (dict(is_buy=True, last=100, ask=100, upper=101, tick=1, extra_ticks=2), 101.0),
        (dict(is_buy=True, last=100, ask=100, tick=0.5, extra_ticks=2), 101.0),
        (dict(is_buy=True, last=100, ask=100.2, tick=0.5, extra_ticks=0), 100.5),
        (dict(is_buy=False, last=100, bid=100, tick=1, extra_ticks=2), 98.0),
        (dict(is_buy=False, last=100, tick=1, extra_ticks=2), 98.0),
        (dict(is_buy=False, last=100, bid=100, lower=99, tick=1, extra_ticks=2), 99.0),
        (dict(is_buy=False, last=100, bid=100.2, tick=0.5, extra_ticks=0), 100.0),
    ],
)
def test_ctp_opponent_limit_follows_opponent_price(kwargs, expected):
    assert ctp_opponent_limit(**kwargs) == pytest.approx(expected)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(is_buy=True, last=0, ask=0),
        dict(is_buy=False, last=0, bid=0),
        dict(is_buy=True, last=-1, ask=-1),
        dict(is_buy=False, last=-1, bid=-1),
    ],
)
def test_ctp_opponent_limit_returns_none_without_price(kwargs):
    assert ctp_opponent_limit(**kwargs) is None


def test_ctp_opponent_limit_treats_bad_tick_and_negative_extra_safely():
    assert ctp_opponent_limit(is_buy=True, last=100, ask=100, tick=0, extra_ticks=2) == 102.0
    assert ctp_opponent_limit(is_buy=True, last=100, ask=100, tick=-1, extra_ticks=2) == 102.0
    assert ctp_opponent_limit(is_buy=True, last=100, ask=100, tick=1, extra_ticks=-3) == 100.0


@pytest.mark.parametrize(
    ("entry", "live", "atr", "is_long", "limit_atr", "limit_points", "expected"),
    [
        (100, 106, 10, True, 0.5, 0, (True, 6, 5)),
        (100, 105, 10, True, 0.5, 0, (False, 5, 5)),
        (100, 94, 10, False, 0.5, 0, (True, 6, 5)),
        (100, 106, 10, False, 0.5, 0, (False, -6, 5)),
        (100, 104, 10, True, 0.5, 3, (True, 4, 3)),
        (100, 104, 0, True, 0, 3, (True, 4, 3)),
        (100, 90, 10, True, 0.5, 0, (False, -10, 5)),
        (0, 100, 10, True, 0.5, 0, (False, 0, 0)),
        (100, 0, 10, True, 0.5, 0, (False, 0, 0)),
        (100, 106, 10, True, 0, 0, (False, 0, 0)),
    ],
)
def test_entry_slip_blocked_boundaries(
    entry, live, atr, is_long, limit_atr, limit_points, expected
):
    blocked, chase, limit = entry_slip_blocked(
        entry,
        live,
        atr,
        is_long=is_long,
        limit_atr=limit_atr,
        limit_points=limit_points,
    )
    assert blocked is expected[0]
    assert chase == pytest.approx(expected[1])
    assert limit == pytest.approx(expected[2])


def test_price_tick_prefers_contract_table_then_ctp_then_trade():
    pricing = _Pricing(
        {"risk": {"contracts": {"bu": {"price_tick": 0.5}}}, "trade": {"price_tick": 0.1}}
    )
    assert pricing._price_tick_for("bu2612") == 0.5

    pricing = _Pricing({"risk": {"contracts": {"bu": {}}}, "trade": {"ctp": {"price_tick": 0.2}}})
    assert pricing._price_tick_for("bu2612") == 0.2

    pricing = _Pricing({"trade": {"price_tick": 0.1}})
    assert pricing._price_tick_for("bu2612") == 0.1


@pytest.mark.parametrize("bad", ["abc", None, "", -1, 0])
def test_price_tick_falls_back_to_one_on_bad_values(bad):
    pricing = _Pricing({"risk": {"contracts": {"bu": {"price_tick": bad}}}})
    assert pricing._price_tick_for("bu2612") == 1.0


def test_protect_ticks_defaults_and_clamps():
    assert _Pricing({})._close_protect_ticks() == 2
    assert _Pricing({"trade": {"ctp_close_protect_ticks": 3}})._close_protect_ticks() == 3
    assert _Pricing({"trade": {"ctp_close_protect_ticks": "x"}})._close_protect_ticks() == 2
    assert _Pricing({"trade": {"ctp_close_protect_ticks": -2}})._close_protect_ticks() == 0

    assert _Pricing({})._open_protect_ticks() == 1
    assert _Pricing({"trade": {"ctp_open_protect_ticks": 2}})._open_protect_ticks() == 2
    assert _Pricing({"trade": {"ctp_open_protect_ticks": "x"}})._open_protect_ticks() == 1
    assert _Pricing({"trade": {"ctp_open_protect_ticks": -1}})._open_protect_ticks() == 0


def test_quote_snapshot_prefers_latest_for_and_exchange_limits():
    md = _FakeMd(row={"last": 100, "bid": 99, "ask": 101})
    trader = _FakeTrader(gateway=_FakeGateway(md=md, instrument={"upper": 110, "lower": 90}))
    pricing = _Pricing({}, trader=trader)

    quote = pricing._ctp_quote_snapshot("bu2612")

    assert quote == {"last": 100.0, "bid": 99.0, "ask": 101.0, "upper": 110.0, "lower": 90.0}


def test_quote_snapshot_uses_legacy_latest_only_without_wanted_symbols():
    md = _FakeMd(row=None, wanted=None, latest={"last": 88})
    pricing = _Pricing({}, trader=_FakeTrader(md=md))
    assert pricing._ctp_quote_snapshot("bu2612")["last"] == 88.0

    md = _FakeMd(row=None, wanted=["bu2612"], latest={"last": 88})
    kline = _FakeKline(latest=77)
    pricing = _Pricing({}, trader=_FakeTrader(md=md), kline=kline)
    assert pricing._ctp_quote_snapshot("bu2612")["last"] == 77.0


def test_quote_snapshot_falls_back_to_kline_close_and_then_zero():
    pricing = _Pricing(
        {},
        kline=_FakeKline(latest=0, df=pd.DataFrame({"close": [66.0]})),
    )
    assert pricing._ctp_quote_snapshot("bu2612")["last"] == 66.0

    pricing = _Pricing(
        {},
        kline=_FakeKline(latest=0, df=pd.DataFrame({"close": [66.0]}), raise_latest=True),
    )
    assert pricing._ctp_quote_snapshot("bu2612")["last"] == 66.0

    pricing = _Pricing({}, kline=_FakeKline(latest=0, df=None))
    assert pricing._ctp_quote_snapshot("bu2612")["last"] == 0.0


def test_quote_snapshot_uses_exchange_uppercase_limit_keys():
    md = _FakeMd(row={"last": 100, "bid": 0, "ask": 0, "upper": 0, "lower": 0})
    trader = _FakeTrader(
        gateway=_FakeGateway(md=md, instrument={"UpperLimitPrice": 120, "LowerLimitPrice": 80})
    )
    pricing = _Pricing({}, trader=trader)
    quote = pricing._ctp_quote_snapshot("bu2612")
    assert quote["upper"] == 120.0
    assert quote["lower"] == 80.0


def test_ctp_is_buy_covers_open_and_close_sides():
    pricing = _Pricing({})
    assert pricing._ctp_is_buy(_decision(StrategyAction.BUY_OPEN)) is True
    assert pricing._ctp_is_buy(_decision(StrategyAction.SELL_OPEN)) is False

    class _Pos:
        side = "short"

    pricing._position_for = lambda symbol: _Pos()
    assert pricing._ctp_is_buy(_decision(StrategyAction.CLOSE)) is True

    _Pos.side = "long"
    assert pricing._ctp_is_buy(_decision(StrategyAction.CLOSE)) is False


def test_ctp_limit_price_uses_open_protect_and_records_latest():
    pricing = _Pricing(
        {
            "risk": {"contracts": {"bu": {"price_tick": 0.5}}},
            "trade": {"ctp_open_protect_ticks": 2},
        }
    )
    pricing._ctp_quote_snapshot = lambda symbol: {
        "last": 100.0,
        "bid": 99.5,
        "ask": 100.0,
        "upper": 0.0,
        "lower": 0.0,
    }
    decision = _decision(StrategyAction.BUY_OPEN, entry=99.0)
    price = pricing._ctp_limit_price(decision)
    assert price == 101.0
    assert decision.meta["latest_price"] == 100.0
    assert decision.meta["theory_price"] == 99.0
    assert decision.meta["latest_price_source"] == "ctp_opponent"


def test_ctp_limit_price_close_adds_chase_ticks_and_caps_at_eight():
    pricing = _Pricing(
        {"risk": {"contracts": {"bu": {"price_tick": 1.0}}}, "trade": {"ctp_close_protect_ticks": 2}}
    )
    pricing._ctp_quote_snapshot = lambda symbol: {
        "last": 100.0,
        "bid": 100.0,
        "ask": 100.0,
        "upper": 0.0,
        "lower": 0.0,
    }
    pricing._close_chase_ticks = {"bu2612": 20}

    class _Pos:
        side = "short"

    pricing._position_for = lambda symbol: _Pos()
    decision = _decision(StrategyAction.CLOSE, stop=95.0)
    assert pricing._ctp_limit_price(decision) == 108.0
    assert decision.meta["theory_price"] == 95.0


def test_ctp_limit_price_returns_none_without_quote():
    pricing = _Pricing({})
    pricing._ctp_quote_snapshot = lambda symbol: {
        "last": 0.0,
        "bid": 0.0,
        "ask": 0.0,
        "upper": 0.0,
        "lower": 0.0,
    }
    assert pricing._ctp_limit_price(_decision(StrategyAction.BUY_OPEN)) is None


def test_open_slip_blocked_uses_atr_and_records_event():
    class _Leg:
        timeframe = "5"

        @staticmethod
        def _atr(bars):
            return 10.0

    pricing = _Pricing(
        {"trade": {"max_entry_slip_atr": 0.5, "max_entry_slip_points": 0}},
        kline=_FakeKline(latest=0, df=pd.DataFrame({"close": [100.0]})),
        strategy=object(),
    )
    pricing._strategy_for = lambda symbol: _Leg()
    decision = _decision(StrategyAction.BUY_OPEN, entry=100.0)
    assert pricing._ctp_open_slip_blocked(decision, 106.0) is True
    assert pricing.events[0][0] == "slip_block_open"


def test_open_slip_blocked_allows_when_atr_unavailable():
    pricing = _Pricing(
        {"trade": {"max_entry_slip_atr": 0.5}},
        kline=_FakeKline(latest=0, df=None),
        strategy=object(),
    )
    pricing._strategy_for = lambda symbol: object()
    decision = _decision(StrategyAction.BUY_OPEN, entry=100.0)
    assert pricing._ctp_open_slip_blocked(decision, 200.0) is False
    assert pricing.events == []


def test_ctp_opponent_limit_returns_none_when_sell_price_non_positive():
    """卖出价被 floor 夹到 0 或负数时直接返回 None。"""
    # bid=1, extra=5 → price = -4, max(-4, 0) = 0 → price<=0 → None
    assert ctp_opponent_limit(is_buy=False, last=1, bid=1, lower=0, tick=1, extra_ticks=5) is None


def test_open_slip_blocked_parses_bad_atr_and_points_as_default():
    """cfg 里给 'x' 字符串也能安全回退到默认值。"""
    pricing = _Pricing(
        {"trade": {"max_entry_slip_atr": "x", "max_entry_slip_points": "bad"}},
        kline=_FakeKline(latest=0, df=None),
        strategy=object(),
    )
    pricing._strategy_for = lambda symbol: object()
    # limit_atr 回退到 0.5，limit_points 回退到 0；at r=0 时 limit=0，闸门放行
    decision = _decision(StrategyAction.BUY_OPEN, entry=100.0)
    assert pricing._ctp_open_slip_blocked(decision, 200.0) is False
    assert pricing.events == []


def test_quote_snapshot_latest_for_with_wanted_symbols_prefers_kline_fallback():
    """md.latest_for 返回 None 且 wanted_symbols 非空时，跳过 legacy latest，退回 kline。"""
    md = _FakeMd(row=None, wanted=["bu2612"], latest={"last": 88})
    kline = _FakeKline(latest=66, df=None)
    pricing = _Pricing({}, trader=_FakeTrader(md=md), kline=kline)
    quote = pricing._ctp_quote_snapshot("bu2612")
    assert quote["last"] == 66.0


def test_quote_snapshot_kline_fetch_exception_is_swallowed(tmp_path) -> None:
    """kline.fetch 抛异常时 last 保留 0，不 crash。"""

    class _BadKline(_FakeKline):
        def fetch(self, symbol, period="5"):
            raise RuntimeError("quote backend down")

    pricing = _Pricing({}, kline=_BadKline(latest=0, df=None))
    assert pricing._ctp_quote_snapshot("bu2612")["last"] == 0.0
