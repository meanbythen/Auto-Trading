"""
当日信号质量
功能：对策略重放出来的每一笔，算 MAE / MFE / R 倍 / 利润捕获率，回答「这笔信号好不好」。
原理：
  MAE = 持仓期间对你最不利的价差（浮亏峰值）；
  MFE = 持仓期间对你最有利的价差（浮盈峰值）；
  R = 最终点数盈亏 ÷ 开仓时止损距离；
  捕获率 = 最终有利点 ÷ MFE（有多少顺风被你拿住）。
  这些是复盘里判断策略缺陷的硬数字，不依赖基本面故事。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from src.review.technical import as_naive_datetime


@dataclass
class SignalTrade:
    """重放出来的一笔完整（或未平）信号，带质量指标。"""

    side: str  # long / short
    entry_time: datetime
    exit_time: datetime | None
    entry_price: float
    exit_price: float | None
    stop_price: float | None
    reason_entry: str = ""
    reason_exit: str = ""
    still_open: bool = False
    mae: float = 0.0
    mfe: float = 0.0
    pnl_points: float = 0.0
    risk_points: float = 0.0
    r_multiple: float | None = None
    capture_ratio: float | None = None
    # 开仓时的 ADX，交给评估分趋势/震荡组
    adx_at_entry: float | None = None


@dataclass
class QualityReport:
    """一天全部信号的质量汇总，以及写进报告的句子。"""

    trades: list[SignalTrade] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    closed_count: int = 0
    open_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    avg_r: float | None = None
    avg_capture: float | None = None
    avg_mae: float | None = None
    avg_mfe: float | None = None
    net_points: float = 0.0
    expectancy_r: float | None = None


def enrich_round_trips(
    opens: list,
    closes: list,
    bars: pd.DataFrame | None,
    *,
    window_end: datetime | None = None,
) -> list[SignalTrade]:
    """
    把开仓/平仓事件配成一笔笔信号，并在持仓区间里扫高低点算 MAE/MFE。

    参数:
        opens / closes: replay 里的 ReplayEvent 列表（需带 when / stop）
        bars: 信号周期K线，索引是时间
        window_end: 窗口结束时刻；还有仓没平时，用这段最后一根K线当临时出场
    """
    if not opens:
        return []

    remaining_closes = list(closes)
    out: list[SignalTrade] = []
    for op in opens:
        when = getattr(op, "when", None) or _parse_time(op.time)
        if when is None:
            continue
        side = "long" if "多" in str(op.action) else "short"
        stop = getattr(op, "stop", None)
        match = None
        for i, cl in enumerate(remaining_closes):
            cl_when = getattr(cl, "when", None) or _parse_time(cl.time)
            if cl_when is not None and cl_when >= when:
                match = remaining_closes.pop(i)
                break
        if match is not None:
            exit_when = getattr(match, "when", None) or _parse_time(match.time)
            trade = SignalTrade(
                side=side,
                entry_time=when,
                exit_time=exit_when,
                entry_price=float(op.price),
                exit_price=float(match.price),
                stop_price=float(stop) if stop not in (None, 0, 0.0) else None,
                reason_entry=str(op.reason or ""),
                reason_exit=str(match.reason or ""),
                still_open=False,
                adx_at_entry=getattr(op, "adx", None),
            )
        else:
            # 窗口结束还拿着：用最后可见价估算，标明未平
            exit_when = window_end
            exit_price = float(op.price)
            if bars is not None and len(bars) > 0:
                slice_bars = _bars_from(bars, when, window_end)
                if len(slice_bars) > 0:
                    exit_price = float(slice_bars["close"].iloc[-1])
                    exit_when = as_naive_datetime(slice_bars.index[-1])
            trade = SignalTrade(
                side=side,
                entry_time=when,
                exit_time=exit_when,
                entry_price=float(op.price),
                exit_price=exit_price,
                stop_price=float(stop) if stop not in (None, 0, 0.0) else None,
                reason_entry=str(op.reason or ""),
                reason_exit="窗口结束仍持仓（复盘按最后一根估算）",
                still_open=True,
                adx_at_entry=getattr(op, "adx", None),
            )
        _fill_excursions(trade, bars)
        out.append(trade)
    return out


def build_quality_report(trades: list[SignalTrade]) -> QualityReport:
    """把多笔信号收成一段人话 + 汇总数字。"""
    report = QualityReport(trades=list(trades))
    if not trades:
        report.lines.append("这段没有可分析的信号（策略观望，或只看盘面）。")
        report.lines.append("没有开仓时，质量指标先空着；重点去看第四节的观望原因和第八节有没有「有行情无信号」。")
        return report

    closed = [t for t in trades if not t.still_open]
    opened = [t for t in trades if t.still_open]
    report.closed_count = len(closed)
    report.open_count = len(opened)
    scored = closed if closed else trades

    wins = [t for t in scored if t.pnl_points > 0]
    losses = [t for t in scored if t.pnl_points <= 0]
    report.win_count = len(wins)
    report.loss_count = len(losses)
    report.net_points = float(sum(t.pnl_points for t in scored))
    rs = [t.r_multiple for t in scored if t.r_multiple is not None]
    caps = [t.capture_ratio for t in scored if t.capture_ratio is not None]
    report.avg_r = (sum(rs) / len(rs)) if rs else None
    report.avg_capture = (sum(caps) / len(caps)) if caps else None
    report.avg_mae = sum(t.mae for t in scored) / len(scored) if scored else None
    report.avg_mfe = sum(t.mfe for t in scored) / len(scored) if scored else None
    report.expectancy_r = (sum(rs) / len(rs)) if rs else None

    report.lines.append(
        "下面按「真实当天行情」重放每一笔信号，用 MAE（最差浮亏）、MFE（最好浮盈）、"
        "R 倍（盈亏÷止损距离）、捕获率（最终有利÷MFE）衡量信号质量。"
        "不算手数和保证金，只看价格路径是否支持这套规则。"
    )
    if report.closed_count:
        wr = report.win_count / report.closed_count
        report.lines.append(
            f"已平仓 {report.closed_count} 笔：赢 {report.win_count} / 亏 {report.loss_count}，"
            f"胜率 {wr:.0%}，合计点数 {report.net_points:+.1f}。"
        )
    if report.open_count:
        report.lines.append(f"窗口结束仍持仓 {report.open_count} 笔（用最后一根K估算，不算正式平仓）。")
    if report.avg_r is not None:
        report.lines.append(f"平均 R 倍 {report.avg_r:+.2f}（期望值约等于这个数字）。")
    if report.avg_capture is not None:
        report.lines.append(f"平均利润捕获率 {report.avg_capture:.0%}（越高说明顺风拿得越稳，越低说明利润回吐多）。")
    if report.avg_mae is not None and report.avg_mfe is not None:
        report.lines.append(
            f"平均 MAE {report.avg_mae:.1f} 点，平均 MFE {report.avg_mfe:.1f} 点。"
            "若 MAE 经常接近止损距离，说明止损偏紧或进场位置差。"
        )

    report.lines.append("分笔明细：")
    for i, t in enumerate(trades, 1):
        report.lines.append(f"  · {_trade_line(i, t)}")
    return report


def _fill_excursions(trade: SignalTrade, bars: pd.DataFrame | None) -> None:
    """扫持仓区间高低点，填 MAE/MFE/R/捕获率。"""
    entry = float(trade.entry_price)
    exit_px = float(trade.exit_price if trade.exit_price is not None else entry)
    if trade.side == "long":
        trade.pnl_points = exit_px - entry
    else:
        trade.pnl_points = entry - exit_px

    if trade.stop_price is not None:
        trade.risk_points = abs(entry - float(trade.stop_price))
        if trade.risk_points > 1e-9:
            trade.r_multiple = trade.pnl_points / trade.risk_points

    mae = 0.0
    mfe = 0.0
    if bars is not None and len(bars) > 0 and trade.exit_time is not None:
        slice_bars = _bars_between(bars, trade.entry_time, trade.exit_time)
        for _, row in slice_bars.iterrows():
            high = float(row["high"])
            low = float(row["low"])
            if trade.side == "long":
                mae = max(mae, entry - low)
                mfe = max(mfe, high - entry)
            else:
                mae = max(mae, high - entry)
                mfe = max(mfe, entry - low)
    trade.mae = float(mae)
    trade.mfe = float(mfe)
    if mfe > 1e-9:
        favorable = trade.pnl_points if trade.pnl_points > 0 else 0.0
        # 亏损单捕获率为 0；盈利单看拿住了多少顺风
        trade.capture_ratio = max(0.0, min(favorable / mfe, 1.5))


def _bars_between(bars: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame:
    out = bars[(bars.index >= start) & (bars.index <= end)]
    return out


def _bars_from(bars: pd.DataFrame, start: datetime, end: datetime | None) -> pd.DataFrame:
    if end is None:
        return bars[bars.index >= start]
    return bars[(bars.index >= start) & (bars.index <= end)]


def _parse_time(text: str) -> datetime | None:
    """ReplayEvent.time 形如 10-25 09:15，缺少年份时返回 None，交给 when 字段。"""
    return None


def _trade_line(index: int, t: SignalTrade) -> str:
    side = "多" if t.side == "long" else "空"
    entry_t = t.entry_time.strftime("%H:%M")
    exit_t = t.exit_time.strftime("%H:%M") if t.exit_time else "--"
    status = "未平" if t.still_open else "已平"
    r_text = f"R {t.r_multiple:+.2f}" if t.r_multiple is not None else "R 未知（无止损价）"
    cap = f"捕获 {t.capture_ratio:.0%}" if t.capture_ratio is not None else "捕获 —"
    return (
        f"第{index}笔 {side} {entry_t}→{exit_t}（{status}） "
        f"入 {_px(t.entry_price)} 出 {_px(t.exit_price or t.entry_price)} "
        f"盈亏 {t.pnl_points:+.1f}点 | MAE {t.mae:.1f} MFE {t.mfe:.1f} | {r_text} | {cap}"
    )


def _px(value: float) -> str:
    if abs(value - round(value)) < 0.05:
        return f"{value:.0f}"
    return f"{value:.2f}"
