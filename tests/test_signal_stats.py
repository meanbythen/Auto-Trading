"""信号账户统计：胜率、盈利因子、回撤、资金使用率和每日盈亏。不打开界面。"""

from datetime import date

from src.account.signal_book import append_trade, new_book
from src.account.signal_stats import analyze_book, format_factor, format_win_rate, span_bounds


def _trade(book, offset, direction, price, when, symbol="bu2612"):
    append_trade(
        book,
        symbol=symbol,
        offset=offset,
        direction=direction,
        price=price,
        volume=1,
        multiplier=10,
        bar_id=when,
        when=when,
    )


def test_full_span_equity_matches_the_balance() -> None:
    """只有一笔初始资金时，权益线的最后一个点就是这笔钱，不能再加一遍。"""
    book = new_book(20340, version="1.10.1", started_at="2026-10-22 20:56:57")
    report = analyze_book(book, start=None, end=date(2026, 10, 22))
    assert report.equity[-1][1] == 20340
    assert report.account.max_drawdown == 0


def test_span_week_includes_today() -> None:
    """近1周含今天，一共 7 天。"""
    start, end = span_bounds("近1周", date(2026, 10, 22))
    assert end == date(2026, 10, 22)
    assert start == date(2026, 10, 16)
    assert span_bounds("全部", date(2026, 10, 22)) == (None, date(2026, 10, 22))


def test_account_and_symbol_stats_follow_the_selected_span() -> None:
    """赚亏放在一起算盈利因子。时间段外面的平仓不进这段数字，但仍留在日历里。"""
    book = new_book(10000, version="1.9.1", started_at="2026-10-01 09:00:00")
    _trade(book, "开仓", "多", 100, "2026-10-08 09:00:00")
    _trade(book, "平仓", "多", 110, "2026-10-08 15:00:00")
    _trade(book, "开仓", "多", 100, "2026-10-20 09:00:00")
    _trade(book, "平仓", "多", 90, "2026-10-20 15:00:00")
    report = analyze_book(
        book,
        start=date(2026, 10, 16),
        end=date(2026, 10, 22),
        default_margin=1000,
    )
    assert report.account.closed == 1
    assert report.account.net_pnl == -100
    assert report.account.win_rate == 0
    assert format_win_rate(report.account.win_rate) == "0.00%"
    assert report.daily_pnl[date(2026, 10, 8)] == 100
    assert report.daily_pnl[date(2026, 10, 20)] == -100
    assert report.symbols[0].symbol == "bu2612"
    assert report.account.utilization is not None
    assert abs(report.account.utilization - (1000 / 10100)) < 0.001


def test_profit_factor_is_gains_over_losses() -> None:
    """盈利因子用总盈利除以总亏损。没有亏损时不假装算出一个倍数。"""
    book = new_book(10000, version="1.9.1", started_at="2026-10-01 09:00:00")
    _trade(book, "开仓", "多", 100, "2026-10-02 09:00:00", symbol="bu2612")
    _trade(book, "平仓", "多", 130, "2026-10-02 15:00:00", symbol="bu2612")
    _trade(book, "开仓", "空", 100, "2026-10-08 09:00:00", symbol="c2611")
    _trade(book, "平仓", "空", 110, "2026-10-08 15:00:00", symbol="c2611")
    # 平仓盈亏的变动时间必须跟成交同一天，按结束日筛权益线时才不会漏掉。
    pnl_times = [row["time"][:10] for row in book["changes"] if row.get("kind") == "pnl"]
    assert pnl_times == ["2026-10-02", "2026-10-08"]
    report = analyze_book(book, start=None, end=date(2026, 10, 22), default_margin=0)
    # 全部时段从 0 加起：本金 10000，只加一次，再加减平仓盈亏，不能变成 20000 出头。
    assert report.equity[-1][1] == 10200
    assert report.account.closed == 2
    assert report.account.win_rate == 0.5
    assert abs((report.account.profit_factor or 0) - 3) < 0.001
    assert format_factor(report.account.profit_factor) == "3.00"
    names = [item.symbol for item in report.symbols]
    assert names == ["bu2612", "c2611"]

    only_win = new_book(10000, version="1.9.1", started_at="2026-10-01 09:00:00")
    _trade(only_win, "开仓", "多", 100, "2026-10-02 09:00:00")
    _trade(only_win, "平仓", "多", 110, "2026-10-02 15:00:00")
    won = analyze_book(only_win, start=None, end=date(2026, 10, 22))
    assert format_factor(won.account.profit_factor) == "没有亏损"


def test_night_close_goes_to_next_trading_day() -> None:
    """周一 21:30 开的仓已经是周二这个交易日，盈亏也记在周二，不记周一。"""
    book = new_book(10000, version="1.9.1", started_at="2026-10-05 09:00:00")
    _trade(book, "开仓", "多", 100, "2026-10-06 21:30:00")
    _trade(book, "平仓", "多", 110, "2026-10-06 22:00:00", symbol="bu2612")
    assert analyze_book(book, start=date(2026, 10, 6), end=date(2026, 10, 6)).account.closed == 0
    report = analyze_book(book, start=date(2026, 10, 7), end=date(2026, 10, 7))
    assert report.account.closed == 1
    assert report.account.net_pnl == 100
    assert report.daily_pnl == {date(2026, 10, 7): 100}
