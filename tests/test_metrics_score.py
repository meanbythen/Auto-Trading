"""排名用的得分不看胜率。

盈利因子超过 2 不再加分。净赚盖得住回撤才有高分。
同样的盈亏结构，胜率不同，分数应该一样。
"""

from src.backtest.metrics import TradeRecord, calc_performance


def _trade(pnl: float, index: int) -> TradeRecord:
    """造一笔只有盈亏有用的成交。价格和手数不参与得分。"""
    return TradeRecord(
        symbol="bu",
        side="long",
        entry_time=f"2026-10-01 09:{index:02d}:00",
        exit_time=f"2026-10-01 10:{index:02d}:00",
        entry_price=100,
        exit_price=100,
        volume=1,
        pnl=pnl,
    )


def test_same_money_has_the_same_score_at_different_win_rates() -> None:
    """总赚、总亏、回撤一样时，50% 胜率和 75% 胜率分数相同。"""
    equity = [10000, 10800]
    half = calc_performance(
        [_trade(600, 1), _trade(400, 2), _trade(-150, 3), _trade(-50, 4)],
        equity,
        min_trades=1,
    )
    higher = calc_performance(
        [_trade(900, 1), _trade(50, 2), _trade(50, 3), _trade(-200, 4)],
        equity,
        min_trades=1,
    )
    assert half.win_rate == 0.5
    assert higher.win_rate == 0.75
    assert half.net_pnl == higher.net_pnl
    assert half.score == higher.score


def test_profit_factor_above_two_does_not_raise_the_score() -> None:
    """净赚一样、又没有回撤时，盈利因子 5 不比 2 更高。"""
    equity = [10000, 12000]
    modest = calc_performance(
        [_trade(4000, 1), _trade(-2000, 2)],
        equity,
        min_trades=1,
    )
    pretty = calc_performance(
        [_trade(2500, 1), _trade(-500, 2)],
        equity,
        min_trades=1,
    )
    assert modest.profit_factor == 2
    assert pretty.profit_factor == 5
    assert modest.score == pretty.score


def test_covering_the_drawdown_beats_a_high_win_rate() -> None:
    """经常小赚、一笔大回撤，不如胜率低但净赚盖得住回撤。"""
    fragile = calc_performance(
        [_trade(100, i) for i in range(8)] + [_trade(-700, 9)],
        [10000, 10800, 10000, 9300],
        min_trades=1,
    )
    sturdy = calc_performance(
        [_trade(400, 1), _trade(400, 2), _trade(-200, 3), _trade(-200, 4), _trade(400, 5)],
        [10000, 10400, 10800, 10600, 10400, 10800],
        min_trades=1,
    )
    assert fragile.win_rate > sturdy.win_rate
    assert sturdy.net_pnl > fragile.net_pnl
    assert sturdy.score > fragile.score


def test_fewer_trades_get_a_smaller_positive_score() -> None:
    """盈亏结构一样时，5 笔的正分低于 30 笔。"""
    few = calc_performance([_trade(100, i) for i in range(5)], [10000, 10500], min_trades=1)
    many = calc_performance([_trade(100, i) for i in range(30)], [10000, 13000], min_trades=1)
    assert few.score > 0
    assert many.score > few.score
