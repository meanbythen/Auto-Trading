"""品种手续费计算。"""

from src.risk.commission import fee_close, fee_open, is_close_today


def test_open_and_close_today_fees() -> None:
    contracts = {
        "fu": {
            "name": "燃油",
            "margin": 10000,
            "multiplier": 10,
            "commission": 2.63,
            "commission_close_today": 0.0,
        }
    }
    assert fee_open(contracts, "fu2701", 1) == 2.63
    assert fee_close(contracts, "fu2701", 1, close_today=True) == 0.0
    assert fee_close(contracts, "fu2701", 1, close_today=False) == 2.63


def test_close_yesterday_can_differ_from_open() -> None:
    """生猪这类开仓、平昨费率不同时，平昨用平仓手续费。"""
    contracts = {"lh": {"commission": 30.0, "commission_close": 15.0, "commission_close_today": 0.0}}
    assert fee_open(contracts, "lh2701", 1) == 30.0
    assert fee_close(contracts, "lh2701", 1, close_today=False) == 15.0
    assert fee_close(contracts, "lh2701", 1, close_today=True) == 0.0


def test_blank_close_today_follows_per_lot() -> None:
    contracts = {"rb": {"commission": 1.5}}
    assert fee_close(contracts, "rb2701", 2, close_today=True) == 3.0


def test_same_trading_day_is_close_today() -> None:
    """夜盘算下一交易日：周二 21:36 开、周三 10:00 平，是平今。"""
    assert is_close_today("2026-10-27T21:36:35", "2026-10-27T22:55:00")
    assert is_close_today("2026-10-27T21:36:35", "2026-10-28T10:00:00")


def test_close_in_a_later_trading_day_is_close_yesterday() -> None:
    """周三日盘开、周三夜盘平，夜盘已属周四，算平昨。"""
    assert not is_close_today("2026-10-28T09:30:00", "2026-10-28T22:55:00")


def test_friday_night_and_monday_are_one_trading_day() -> None:
    """周五夜盘归下周一，周五夜盘开、周一 10:00 平，是平今。"""
    assert is_close_today("2026-10-30T21:36:35", "2026-11-02T10:00:00")
