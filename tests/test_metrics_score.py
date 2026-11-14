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


def test_payoff_is_average_win_over_average_loss() -> None:
    """盈亏比是平均盈利除以平均亏损，不是总盈利除以总亏损。"""
    p = calc_performance(
        [_trade(200, 1), _trade(100, 2), _trade(-50, 3)],
        [10000, 10150],
        min_trades=1,
    )
    # 两笔赢平均 150，一笔亏 50，盈亏比 3；盈利因子是 300/50=6。
    assert p.avg_win == 150
    assert p.avg_loss == 50
    assert p.payoff == 3
    assert p.profit_factor == 6
    # 胜率 2/3、盈亏比 3 → 无单位期望 2 − 1/3 = 5/3。
    assert abs(p.expectancy - 5 / 3) < 1e-9
    from src.backtest.metrics import format_expectancy_line, format_payoff_lines

    text = "\n".join(format_payoff_lines(p.avg_win, p.avg_loss, p.payoff))
    assert "盈亏比    3.00" in text
    assert "不是盈利因子" in text
    expect_line = format_expectancy_line(p.win_rate, p.avg_win, p.avg_loss, p.expectancy)
    assert "收益期望  1.67" in expect_line
    assert "每亏1元" in expect_line
    d = p.to_dict()
    assert d["payoff"] == 3
    assert abs(d["expectancy"] - round(5 / 3, 4)) < 1e-9


def test_expectancy_is_unitless_r_multiple() -> None:
    """无单位期望：胜率×盈亏比 − 败率。全亏是 −1。带成本时和参考函数的 e/净亏损一致。"""
    from src.backtest.metrics import format_expectancy_line, unit_expectancy

    p = calc_performance(
        [_trade(-40, 1), _trade(-60, 2)],
        [10000, 9900],
        min_trades=1,
    )
    assert p.expectancy == -1
    assert "没有亏损笔" in format_expectancy_line(0, 0, 0)
    # 胜率 0.4、盈利 120、亏损 80、成本 15 → 净盈 105、净亏 95，带单位 e=-15，无单位 E=-15/95。
    yuan = 0.4 * (120 - 15) - 0.6 * (80 + 15)
    assert abs(yuan + 15) < 1e-9
    value = unit_expectancy(0.4, 120, 80, 15)
    assert abs(value - yuan / 95) < 1e-9


def test_payoff_missing_when_no_losing_trades() -> None:
    """全赢没有亏损笔，盈亏比算不出。"""
    p = calc_performance([_trade(100, 1), _trade(80, 2)], [10000, 10180], min_trades=1)
    assert p.payoff == 0
    from src.backtest.metrics import format_payoff_lines

    text = "\n".join(format_payoff_lines(p.avg_win, p.avg_loss, p.payoff))
    assert "算不出" in text


def test_fewer_trades_get_a_smaller_positive_score() -> None:
    """盈亏结构一样时，5 笔的正分低于 30 笔。"""
    few = calc_performance([_trade(100, i) for i in range(5)], [10000, 10500], min_trades=1)
    many = calc_performance([_trade(100, i) for i in range(30)], [10000, 13000], min_trades=1)
    assert few.score > 0
    assert many.score > few.score
