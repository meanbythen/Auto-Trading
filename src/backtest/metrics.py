"""
回测绩效指标
功能：胜率、盈亏比、最大回撤、收益等，方便对比参数好坏。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from src.domain.pnl_stats import summarize_pnl
from src.domain.trade import TradeRecord


# 收益率只用这个比例估算每笔要交的保证金。不改变开仓手数，也不参与优选排名。
# 10% 和账户分析里「填 10 表示 10%」是同一个意思。
MARGIN_RATE = 0.10


def peak_margin(
    trades: list[TradeRecord],
    multiplier: float,
    margin_rate: float = MARGIN_RATE,
) -> float:
    """
    这次测试里，拿住一笔仓实际要交的保证金，取占用最多的那一笔。

    保证金 = 开仓价 × 合约乘数 × 手数 × 保证金比例。
    前后两笔不会同时拿着，所以不把每一笔保证金加在一起。
    """
    mult = float(multiplier or 0)
    rate = float(margin_rate or 0)
    if mult <= 0 or rate <= 0:
        return 0.0
    peak = 0.0
    for trade in trades:
        price = float(getattr(trade, "entry_price", 0) or 0)
        volume = int(getattr(trade, "volume", 0) or 0)
        if price <= 0 or volume <= 0:
            continue
        peak = max(peak, price * mult * volume * rate)
    return peak


@dataclass
class Performance:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    net_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    profit_factor: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    # 平均盈利 ÷ 平均亏损。没有亏损笔时为 0，界面会写成「算不出」。
    payoff: float = 0.0
    # 无单位：胜率×净盈亏比 − (1−胜率)。每亏 1 元风险期望换回多少。没有亏损笔时为 0。
    expectancy: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 4),
            "net_pnl": round(self.net_pnl, 2),
            "gross_profit": round(self.gross_profit, 2),
            "gross_loss": round(self.gross_loss, 2),
            "profit_factor": round(self.profit_factor, 4),
            "avg_win": round(self.avg_win, 2),
            "avg_loss": round(self.avg_loss, 2),
            "payoff": round(self.payoff, 4),
            "expectancy": round(self.expectancy, 4),
            "max_drawdown": round(self.max_drawdown, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "score": round(self.score, 4),
        }


def payoff_ratio(avg_win: float, avg_loss: float) -> float:
    """盈亏比 = 平均盈利 ÷ 平均亏损。没有亏损笔时算不出，返回 0。"""
    loss = float(avg_loss or 0)
    if loss <= 1e-9:
        return 0.0
    return float(avg_win or 0) / loss


def format_payoff_lines(
    avg_win: float,
    avg_loss: float,
    payoff: float | None = None,
) -> list[str]:
    """
    历史回测摘要里单独三行：平均盈利、平均亏损、盈亏比。

    盈亏比给凯利公式手填用，不要和「总盈利÷总亏损」的盈利因子混在一起。
    """
    win = float(avg_win or 0)
    loss = float(avg_loss or 0)
    ratio = payoff_ratio(win, loss) if payoff is None else float(payoff or 0)
    lines = [
        f"平均盈利  {win:.0f} 元",
        f"平均亏损  {loss:.0f} 元",
    ]
    if loss > 1e-9:
        lines.append(
            f"盈亏比    {ratio:.2f}（平均盈利÷平均亏损，可填凯利；不是盈利因子）"
        )
    else:
        lines.append("盈亏比    —（没有亏损笔，算不出；不能填凯利）")
    return lines


def unit_expectancy(
    win_rate: float,
    avg_win: float,
    avg_loss: float,
    cost: float = 0.0,
) -> float:
    """
    无单位收益期望：每亏 1 元风险，平均能换回多少。

    先按你给的参考算法扣成本：
    净盈利 = 平均盈利 − 成本，净亏损 = 平均亏损 + 成本，净盈亏比 R = 净盈利 ÷ 净亏损。
    再写成无单位：E = 胜率 × R − (1 − 胜率)。

    参考函数里带单位的 e（元/笔）等于 E × 净亏损。这里界面只展示 E。
    回测成交已经扣过手续费和滑点，调用时 cost 默认 0，不要再减一次。
    没有亏损（净亏损为 0）时算不出，返回 0，界面写成横杠。
    """
    p_win = float(win_rate or 0)
    net_win = float(avg_win or 0) - float(cost or 0)
    net_loss = float(avg_loss or 0) + float(cost or 0)
    if net_loss <= 1e-9:
        return 0.0
    ratio = net_win / net_loss
    return p_win * ratio - (1.0 - p_win)


def format_expectancy_line(
    win_rate: float,
    avg_win: float,
    avg_loss: float,
    expectancy: float | None = None,
    cost: float = 0.0,
) -> str:
    """历史回测摘要里单独一行收益期望（无单位）。"""
    net_loss = float(avg_loss or 0) + float(cost or 0)
    if net_loss <= 1e-9:
        return "收益期望  —（没有亏损笔，算不出）"
    value = (
        float(expectancy)
        if expectancy is not None
        else unit_expectancy(win_rate, avg_win, avg_loss, cost)
    )
    return (
        f"收益期望  {value:.2f}"
        "（每亏1元期望赚多少：胜率×盈亏比 − 败率；大于0才有正期望）"
    )


def calc_performance(
    trades: list[TradeRecord],
    equity: list[float],
    *,
    min_trades: int = 5,
) -> Performance:
    """根据成交列表与权益曲线算绩效，并给出综合得分。"""
    p = Performance(trades=len(trades))
    if not trades:
        return p

    pnls = [t.pnl for t in trades]
    summary = summarize_pnl(pnls, zero_as_loss=True)
    p.wins = summary.wins
    p.losses = summary.losses
    p.win_rate = summary.win_rate
    p.net_pnl = float(sum(pnls))
    p.gross_profit = summary.gross_profit
    p.gross_loss = summary.gross_loss
    if summary.profit_factor is None:
        p.profit_factor = 0.0
    elif math.isinf(summary.profit_factor):
        p.profit_factor = 99.0
    else:
        p.profit_factor = summary.profit_factor
    p.avg_win = p.gross_profit / p.wins if p.wins else 0.0
    p.avg_loss = p.gross_loss / p.losses if p.losses else 0.0
    p.payoff = payoff_ratio(p.avg_win, p.avg_loss)
    p.expectancy = unit_expectancy(p.win_rate, p.avg_win, p.avg_loss)

    # 最大回撤（按权益曲线）
    if equity:
        peak = equity[0]
        max_dd = 0.0
        max_dd_pct = 0.0
        for e in equity:
            peak = max(peak, e)
            dd = peak - e
            dd_pct = dd / peak if peak > 1e-9 else 0.0
            if dd > max_dd:
                max_dd = dd
                max_dd_pct = dd_pct
        p.max_drawdown = float(max_dd)
        p.max_drawdown_pct = float(max_dd_pct)

    # 综合得分不看胜率。胜率高只说明赚的笔数多，不说明实盘更能赚钱。
    # 看两件好事：盈利因子到 2 为止，再高也不加分；净赚能不能盖住最大回撤。
    # 回撤占本金越大，扣得越多。不满 30 笔的正分再打个折，避免三四笔全胜排到前面。
    if p.trades < min_trades:
        p.score = -999.0 + p.trades  # 样本太少直接垫底
    else:
        p.score = _money_score(p)

    return p


def _money_score(p: Performance) -> float:
    """
    把绩效收成一个用来排名的分数。胜率不参加。

    盈利因子：0 到 2 之间越高越好，2 和 99 一样，不再奖励特别好看的数字。
    净盈亏对回撤：亏钱是 0；净赚刚好等于回撤是一半；净赚达到回撤的两倍就满分。
    回撤比例：最多按权益的 50% 来扣。
    笔数：30 笔才给满正分，更少就按比例少给一点。
    """
    # 盈利因子到 2 就封顶。四笔全胜那种 99，不会比稳在 2 附近的更高。
    profit_factor_score = min(max(p.profit_factor, 0.0), 2.0) / 2.0
    if p.net_pnl <= 0:
        cover_score = 0.0
    elif p.max_drawdown <= 1e-9:
        cover_score = 1.0
    else:
        cover_score = min(p.net_pnl / p.max_drawdown, 2.0) / 2.0
    drawdown_penalty = min(max(p.max_drawdown_pct, 0.0), 0.5)
    raw = 0.40 * profit_factor_score + 0.45 * cover_score - 0.30 * drawdown_penalty
    if raw > 0 and p.trades < 30:
        # 5 笔大约打七五折，30 笔才是满分。亏钱的分数不再乘这个折扣，免得少亏的反而更吃亏。
        trust = 0.7 + 0.3 * (p.trades / 30.0)
        raw *= trust
    return raw
