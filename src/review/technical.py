"""
当日盘面与技术面（不看程序策略）
功能：只用开高低收、成交量、均线和摆动点，把一段盘说成人话。
原理：
  复盘先回答「今天价格自己怎么走」，再去问策略会不会做。
  这里的针形线、均线是通用看法，不等于某一套策略的开仓条件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from src.review.session import SessionWindow
from src.strategy.indicators import detect_pinbar
from src.strategy.levels import find_swing_levels


@dataclass
class TechnicalView:
    """一段盘（日盘或夜盘）的技术面结论。"""

    name: str
    has_bars: bool
    bar_count: int = 0
    # up / down / mixed：给最后综合结论用，界面不直接显示这个英文
    bias: str = "mixed"
    character: str = ""
    facts: list[str] = field(default_factory=list)
    tech_points: list[str] = field(default_factory=list)
    pin_count: int = 0


def _px(value: float) -> str:
    """价格尽量显示成整数；有小数才保留两位。"""
    if abs(value - round(value)) < 0.05:
        return f"{value:.0f}"
    return f"{value:.2f}"


def _naive_index(df: pd.DataFrame) -> pd.DataFrame:
    """把K线时间统一成不带时区的本地时间，方便和 09:00 比较。"""
    out = df.copy()
    idx = pd.to_datetime(out.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("Asia/Shanghai").tz_localize(None)
    out.index = idx
    return out.sort_index()


def _character(chg_pct: float | None, loc: float, amp: float) -> tuple[str, str]:
    """
    用涨跌幅和收盘在全日高低区间里的位置，给走势起一个名字。

    loc 接近 1 = 收在最高附近；接近 0 = 收在最低附近。
    返回 (人话, bias)。
    """
    chg = 0.0 if chg_pct is None else chg_pct
    if abs(chg) < 0.003 and amp < 0.008:
        return "窄幅震荡，多空都没走出方向。", "mixed"
    if chg >= 0.003 and loc >= 0.66:
        return "偏强：上涨并且收在这段区间的上沿。", "up"
    if chg >= 0.003 and loc <= 0.40:
        return "冲高回落：虽然比前收高，但涨幅没有守住，收在区间下半。", "down"
    if chg <= -0.003 and loc <= 0.34:
        return "偏弱：下跌并且收在这段区间的下沿。", "down"
    if chg <= -0.003 and loc >= 0.60:
        return "探底回升：比前收低，但尾段收回来一截，收在区间上半。", "up"
    return "来回震荡，方向不坚决。", "mixed"


def analyze_window(
    name: str,
    bars: pd.DataFrame | None,
    window: SessionWindow,
) -> TechnicalView:
    """
    分析某一个时段。

    参数:
        bars: 这一天之前也要带上，用来算「前收、均线、之前的量」。
              索引是时间，列至少要有 open/high/low/close，volume 可有可无。
        window: 日盘或夜盘的起止时间
    """
    view = TechnicalView(name=name, has_bars=False)
    if bars is None or len(bars) == 0:
        view.facts.append("没有K线，说不了这段怎么走。")
        return view

    df = _naive_index(bars)
    for col in ("open", "high", "low", "close"):
        if col not in df.columns:
            view.facts.append(f"K线缺 {col}，无法做技术面。")
            return view

    session = df[(df.index >= window.start) & (df.index <= window.end)]
    before = df[df.index < window.start]
    view.bar_count = int(len(session))
    if len(session) == 0:
        view.has_bars = False
        view.facts.append(
            "这段没有K线。常见原因：品种没有夜盘、当天休市，"
            "或者公开分钟线已经没有这么早的数据。"
        )
        return view

    view.has_bars = True
    o = float(session["open"].iloc[0])
    h = float(session["high"].max())
    lo = float(session["low"].min())
    c = float(session["close"].iloc[-1])
    prev = float(before["close"].iloc[-1]) if len(before) else None
    # 没有前收时，用本段开盘价当基准，这样单看当天也能说出涨跌
    base = prev if prev not in (None, 0) else o
    rng = max(h - lo, 1e-9)
    loc = (c - lo) / rng
    chg = c - base
    chg_pct = chg / base if base else 0.0
    amp = (h - lo) / (base or 1.0)
    character, bias = _character(chg_pct, loc, amp)
    view.character = character
    view.bias = bias

    compared = "前收" if prev is not None else "开盘"
    chg_txt = (
        f"较{compared} {_px(base)} {('+' if chg >= 0 else '')}{_px(chg)}"
        f"（{chg_pct:+.2%}）"
    )
    view.facts.append(
        f"{session.index[0]:%H:%M}–{session.index[-1]:%H:%M} 共 {len(session)} 根。"
        f"开 {_px(o)} 高 {_px(h)} 低 {_px(lo)} 收 {_px(c)}。{chg_txt}。"
    )
    view.facts.append(character)

    # 均线用「这段最后一根之前的全部历史」，避免只用当天几根就说趋势
    hist = df[df.index <= session.index[-1]]
    if len(hist) >= 20:
        ma = float(hist["close"].tail(20).mean())
        if c > ma * 1.002:
            view.tech_points.append(f"收盘 {_px(c)} 在近 20 根均线 {_px(ma)} 上方，短线偏多。")
        elif c < ma * 0.998:
            view.tech_points.append(f"收盘 {_px(c)} 在近 20 根均线 {_px(ma)} 下方，短线偏空。")
        else:
            view.tech_points.append(f"收盘贴着近 20 根均线 {_px(ma)}，短线方向不明显。")
    else:
        view.tech_points.append("前面的K线不够 20 根，均线只供参考、先不判断趋势。")

    # 摆动高低点：左右各 2 根确认，越近越重要
    if len(hist) >= 8:
        levels = find_swing_levels(hist, lookback=2, max_levels=8)
        supports = [lv for lv in levels if lv.kind == "support" and lv.price <= c * 1.002]
        resists = [lv for lv in levels if lv.kind == "resistance" and lv.price >= c * 0.998]
        sup = max(supports, key=lambda x: x.price) if supports else None
        res = min(resists, key=lambda x: x.price) if resists else None
        if sup or res:
            parts = []
            if sup:
                parts.append(f"下方摆动支撑约 {_px(sup.price)}")
            if res:
                parts.append(f"上方摆动压力约 {_px(res.price)}")
            view.tech_points.append("，".join(parts) + "。（这是高低点，不是策略专用的关键位过滤）")
        else:
            view.tech_points.append("收盘附近没有很清楚的摆动高低点。")

    prior = before.tail(40)
    if "volume" in session.columns and len(prior) >= 5 and "volume" in prior.columns:
        sess_vol = float(session["volume"].mean())
        prior_vol = float(prior["volume"].replace(0, pd.NA).mean())
        if prior_vol and prior_vol == prior_vol and prior_vol > 0:
            if sess_vol > prior_vol * 1.3:
                view.tech_points.append("这段平均成交量比之前明显更大，波动更值得看。")
            elif sess_vol < prior_vol * 0.7:
                view.tech_points.append("这段平均成交量比之前清淡，假突破会更多。")
            else:
                view.tech_points.append("这段成交量和之前差不多，没有异常放量或缩量。")

    pins: list[str] = []
    for ts, row in session.iterrows():
        pin = detect_pinbar(
            float(row["open"]),
            float(row["high"]),
            float(row["low"]),
            float(row["close"]),
        )
        if pin.side == "none":
            continue
        side = "长下影（偏多形态）" if pin.side == "bull" else "长上影（偏空形态）"
        when = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        pins.append(f"{when:%H:%M} {side}")
    view.pin_count = len(pins)
    if pins:
        shown = "；".join(pins[:5])
        more = f"（共 {len(pins)} 根，只列前 5）" if len(pins) > 5 else ""
        view.tech_points.append(
            f"盘面上出现针形线：{shown}{more}。"
            "这只说明K线长得像针，不代表程序策略会开仓。"
        )
    else:
        view.tech_points.append("这段没有标准针形线（影线够长、实体够小的那种）。")

    if len(prior) >= 10:
        med = float((prior["high"] - prior["low"]).median())
        wides: list[str] = []
        for ts, row in session.iterrows():
            bar_rng = float(row["high"]) - float(row["low"])
            if med > 0 and bar_rng >= med * 1.8:
                when = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
                direction = "阳线" if float(row["close"]) >= float(row["open"]) else "阴线"
                wides.append(f"{when:%H:%M}{direction}")
        if wides:
            view.tech_points.append(
                "振幅明显大于前面中位数的K线：" + "、".join(wides[:4]) + "。"
            )
    return view


def slice_session(bars: pd.DataFrame | None, window: SessionWindow) -> pd.DataFrame:
    """切出某一段的K线。策略重放要用完整历史，这个函数只给「有没有这段数据」做检查。"""
    if bars is None or len(bars) == 0:
        return pd.DataFrame()
    df = _naive_index(bars)
    return df[(df.index >= window.start) & (df.index <= window.end)]


def as_naive_datetime(ts: object) -> datetime:
    """K线索引转成普通 datetime，策略和日志都用这个比较。"""
    stamp = pd.Timestamp(ts)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert("Asia/Shanghai").tz_localize(None)
    return stamp.to_pydatetime()
