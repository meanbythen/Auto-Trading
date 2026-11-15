"""
行情质量检查（给人看，不挡下单）。

对照清洗库和「接口原样库」：根数、丢掉的脏时间戳、交易时段缺口、阴阳线是否合理、是否过期。
只在你点按钮或命令行时跑，不跟实盘扫描绑在一起。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from src.data.bar_store import BarStore, RawBarStore, _market_price
from src.data.kline import bars_age_minutes
from src.data.periods import canonical_period, is_session_open_stamp, period_label, stale_after_minutes
from src.data.quote_check import normalize_source, source_label, storage_symbol
from src.data.tq_source import session_open_minutes


LATEST_NAME = "行情质量.xlsx"
# 并排对照这几家。csv / 演示数据不参加。同花顺图只在回测页拉过才有库存。
COMPARE_SOURCES = ("akshare", "eastmoney", "tq", "ths")
_STATUS_CN = {"ok": "能用", "watch": "该看一眼", "empty": "还没数据"}


@dataclass
class QualityReport:
    """一次检查的结果。status: empty=还没数据，ok=能用，watch=该看一眼。"""

    status: str
    symbol: str
    period: str
    source: str
    raw_n: int = 0
    clean_n: int = 0
    dropped_n: int = 0
    misaligned_n: int = 0
    open_stamp_n: int = 0
    long_decimal_n: int = 0
    ohlc_bad_n: int = 0
    gap_note: str = ""
    stale_minutes: float | None = None
    stale: bool = False
    raw_from: str = ""
    raw_to: str = ""
    clean_from: str = ""
    clean_to: str = ""
    samples: list[str] = field(default_factory=list)
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "symbol": self.symbol,
            "period": self.period,
            "source": self.source,
            "raw_n": self.raw_n,
            "clean_n": self.clean_n,
            "dropped_n": self.dropped_n,
            "misaligned_n": self.misaligned_n,
            "open_stamp_n": self.open_stamp_n,
            "long_decimal_n": self.long_decimal_n,
            "ohlc_bad_n": self.ohlc_bad_n,
            "gap_note": self.gap_note,
            "stale_minutes": self.stale_minutes,
            "stale": self.stale,
            "message": self.message,
        }


def _span(df: pd.DataFrame | None) -> tuple[str, str]:
    if df is None or len(df) == 0:
        return "", ""
    return str(df.index[0]), str(df.index[-1])


def _ohlc_bad(row: pd.Series) -> bool:
    try:
        o, h, low, c = float(row.open), float(row.high), float(row.low), float(row.close)
    except (TypeError, ValueError):
        return True
    top = max(o, c)
    bot = min(o, c)
    return h + 1e-9 < top or low - 1e-9 > bot


def inspect_quote(
    root: Path | str,
    symbol: str,
    period: str,
    source: str | None = None,
    *,
    max_stale_minutes: float = 90.0,
) -> QualityReport:
    """对照原样库和清洗库，不访问网络。"""
    base = Path(root)
    period = canonical_period(period)
    src = normalize_source(source)
    stored = storage_symbol(str(symbol).strip(), src)
    raw_store = RawBarStore(base / "data" / "klines" / "raw.sqlite")
    clean_store = BarStore(base / "data" / "klines" / "market.sqlite")
    raw = raw_store.load(stored, period, src)
    clean = clean_store.load(stored, period)
    report = QualityReport(
        status="empty",
        symbol=str(symbol).strip(),
        period=period,
        source=src,
    )
    report.raw_n = 0 if raw is None else len(raw)
    report.clean_n = 0 if clean is None else len(clean)
    report.raw_from, report.raw_to = _span(raw)
    report.clean_from, report.clean_to = _span(clean)

    if report.raw_n == 0 and report.clean_n == 0:
        report.message = (
            f"还没有 {symbol} 的{period_label(period)}库存。"
            "请先用这一家行情跑一次历史回测，或启动常驻拉一次K线。"
        )
        return report
    if report.raw_n == 0:
        report.status = "watch"
        report.message = (
            "清洗库里有K线，但还没有「接口原样」库。"
            "从这一版起，每次下载会另存一份。请再拉一次行情后再检查。"
        )
        if clean is not None:
            from src.backtest.engine import describe_bar_gaps

            report.gap_note = describe_bar_gaps(clean, period)
            report.stale_minutes = bars_age_minutes(clean)
            report.stale = bool(
                report.stale_minutes is not None
                and report.stale_minutes > stale_after_minutes(period, max_stale_minutes)
            )
        return report

    opens = session_open_minutes(symbol)
    step = int(period) if str(period).isdigit() else 0
    samples: list[str] = []
    for ts, row in raw.iterrows():
        stamp = pd.Timestamp(ts)
        bad_time = False
        if step > 0:
            if stamp.second or stamp.microsecond or stamp.nanosecond:
                report.misaligned_n += 1
                bad_time = True
            elif (stamp.hour * 60 + stamp.minute) % step != 0:
                report.misaligned_n += 1
                bad_time = True
            if is_session_open_stamp(stamp, opens):
                report.open_stamp_n += 1
                bad_time = True
        long_dec = False
        for col in ("open", "high", "low", "close"):
            if _market_price(row[col]) is None:
                long_dec = True
                break
        if long_dec:
            report.long_decimal_n += 1
        if _ohlc_bad(row):
            report.ohlc_bad_n += 1
        if (bad_time or long_dec) and len(samples) < 8:
            why = []
            if bad_time:
                why.append("时间对不齐")
            if long_dec:
                why.append("小数过长")
            samples.append(f"{stamp}  {'/'.join(why)}")
    report.samples = samples
    if clean is not None and len(clean) > 0:
        raw_set = set(pd.Timestamp(x) for x in raw.index)
        clean_set = set(pd.Timestamp(x) for x in clean.index)
        report.dropped_n = len(raw_set - clean_set)
        from src.backtest.engine import describe_bar_gaps

        report.gap_note = describe_bar_gaps(clean, period)
        report.stale_minutes = bars_age_minutes(clean)
        report.stale = bool(
            report.stale_minutes is not None
            and report.stale_minutes > stale_after_minutes(period, max_stale_minutes)
        )
    dirty = report.misaligned_n + report.open_stamp_n + report.long_decimal_n + report.ohlc_bad_n
    if report.stale or report.gap_note or (dirty > 0 and report.clean_n == 0):
        report.status = "watch"
    else:
        report.status = "ok"
    bits = [
        f"{source_label(src)} {symbol} {period_label(period)}：原样 {report.raw_n} 根，清洗后 {report.clean_n} 根"
    ]
    if report.dropped_n:
        bits.append(f"清洗丢掉 {report.dropped_n} 根")
    if report.misaligned_n:
        bits.append(f"时间对不齐 {report.misaligned_n} 根")
    if report.open_stamp_n:
        bits.append(f"开盘假戳 {report.open_stamp_n} 根")
    if report.long_decimal_n:
        bits.append(f"小数过长 {report.long_decimal_n} 根")
    if report.ohlc_bad_n:
        bits.append(f"高低价不合理 {report.ohlc_bad_n} 根")
    if report.gap_note:
        bits.append(report.gap_note)
    if report.stale:
        bits.append(f"清洗库最后一根距今约 {report.stale_minutes:.0f} 分钟，偏旧")
    if report.status == "ok" and dirty == 0 and not report.gap_note:
        bits.append("没有明显脏数据")
    report.message = "。".join(bits) + "。"
    return report


def _prep(df: pd.DataFrame | None) -> pd.DataFrame | None:
    """时间戳收成不带时区、去掉重复，方便三家按同一时刻对齐。"""
    if df is None or len(df) == 0:
        return None
    out = df.copy()
    out.index = pd.to_datetime(out.index)
    if getattr(out.index, "tz", None) is not None:
        out.index = out.index.tz_localize(None)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out


def _row_at(df: pd.DataFrame, ts: pd.Timestamp) -> pd.Series | None:
    if ts not in df.index:
        return None
    row = df.loc[ts]
    if isinstance(row, pd.DataFrame):
        return row.iloc[-1]
    return row


def _num(row: pd.Series, col: str) -> float | None:
    try:
        value = float(row[col])
    except (TypeError, ValueError, KeyError):
        return None
    if value != value:
        return None
    return value


def _pct(part: int | float, whole: int) -> float:
    if whole <= 0:
        return 0.0
    return 100.0 * float(part) / float(whole)


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    return float(pd.Series(values).quantile(q))


def _body_sign(row: pd.Series) -> int:
    o = _num(row, "open")
    c = _num(row, "close")
    if o is None or c is None:
        return 0
    if c > o + 1e-9:
        return 1
    if c < o - 1e-9:
        return -1
    return 0


def _is_night(ts: pd.Timestamp) -> bool:
    hour = int(pd.Timestamp(ts).hour)
    return hour >= 21 or hour < 9


def resolve_tick_mult(cfg: dict[str, Any] | None, symbol: str) -> tuple[float, float]:
    """
    每跳价格和合约乘数。用来把价差说成「几跳、1 手大约差几元」。

    优先品种表；没有就用交易配置里的全局每跳。算不出时按 1 跳、乘数 10。
    """
    tick = 1.0
    mult = 10.0
    if not cfg:
        return tick, mult
    from src.data.quote_check import product_code

    key = product_code(symbol)
    risk = cfg.get("risk") if isinstance(cfg.get("risk"), dict) else {}
    contracts = risk.get("contracts") or {}
    box = contracts.get(key) if isinstance(contracts.get(key), dict) else {}
    trade = cfg.get("trade") if isinstance(cfg.get("trade"), dict) else {}
    ctp = trade.get("ctp") if isinstance(trade.get("ctp"), dict) else {}
    bt = cfg.get("backtest") if isinstance(cfg.get("backtest"), dict) else {}
    raw_tick = None
    for candidate in (bt.get("price_tick"), (box or {}).get("price_tick"), ctp.get("price_tick"), trade.get("price_tick")):
        if candidate not in (None, ""):
            raw_tick = candidate
            break
    try:
        tick = float(raw_tick) if raw_tick is not None else 1.0
    except (TypeError, ValueError):
        tick = 1.0
    if tick <= 0:
        tick = 1.0
    try:
        mult = float((box or {}).get("multiplier") or 10)
    except (TypeError, ValueError):
        mult = 10.0
    if mult <= 0:
        mult = 10.0
    return tick, mult


def _detect_shift(
    left: pd.DataFrame,
    right: pd.DataFrame,
    step: int,
    tick: float,
) -> str:
    """
    看两家是同一根对同一根更像，还是整体错开一根（常见于左右标签）。

    只提示，不自动改库存、不改开仓。
    """
    if step <= 0 or len(left) < 20 or len(right) < 20:
        return ""
    scored: list[tuple[float, int, int, float]] = []
    for k in (0, 1, -1):
        shifted = right.copy()
        if k:
            shifted.index = shifted.index + pd.Timedelta(minutes=k * step)
        common = left.index.intersection(shifted.index)
        n = len(common)
        if n < 10:
            continue
        diff = (left.loc[common, "close"].astype(float) - shifted.loc[common, "close"].astype(float)).abs()
        mae = float(diff.mean()) / tick
        # 重叠根数优先，同样重叠时价差更小更好。
        scored.append((-float(n), mae, k, mae))
    if len(scored) < 2:
        return ""
    scored.sort()
    best_n, best_mae, best_k, _ = scored[0]
    zero = next((item for item in scored if item[2] == 0), None)
    if best_k == 0 or zero is None:
        return ""
    zero_n = -zero[0]
    best_overlap = -best_n
    # 错开一根后重叠明显更多，或价差明显更小，才开口。
    better_overlap = best_overlap >= zero_n * 1.15 and best_overlap >= zero_n + 20
    better_price = best_mae + 0.2 < zero[1] and best_overlap >= zero_n * 0.8
    if not (better_overlap or better_price):
        return ""
    direction = "往后" if best_k > 0 else "往前"
    return (
        f"若把后者时间整体{direction}挪 {abs(best_k) * step} 分钟，"
        f"重叠约 {best_overlap:.0f} 根、收盘平均差 {best_mae:.2f} 跳"
        f"（对同一时刻则重叠 {zero_n:.0f} 根、平均差 {zero[1]:.2f} 跳）。"
        "多半是左右标签不一致，不是真的涨跌不同。"
    )


def _compare_layer(
    frames: dict[str, pd.DataFrame | None],
    *,
    layer: str,
    tick: float,
    multiplier: float,
    period: str,
) -> dict[str, Any]:
    """同一层（原样或清洗）里，三家按时间戳并排比开高低收。"""
    ready = {src: _prep(df) for src, df in frames.items()}
    ready = {src: df for src, df in ready.items() if df is not None}
    names = [source_label(src) for src in COMPARE_SOURCES]
    counts = {source_label(src): (0 if src not in ready else len(ready[src])) for src in COMPARE_SOURCES}
    spans = {source_label(src): _span(ready.get(src)) for src in COMPARE_SOURCES}
    result: dict[str, Any] = {
        "layer": layer,
        "counts": counts,
        "spans": spans,
        "n_sources": len(ready),
        "overlap2": 0,
        "overlap3": 0,
        "overlap4": 0,
        "only": {source_label(src): 0 for src in COMPARE_SOURCES},
        "pairs": [],
        "examples": [],
        "recent": [],
        "impact": [],
        "shift_notes": [],
        "day_night": "",
        "message": "",
        "close_same_pct": 0.0,
        "close_ge1_pct": 0.0,
        "close_ge5_pct": 0.0,
        "close_p50_ticks": 0.0,
        "close_p95_ticks": 0.0,
        "close_max_ticks": 0.0,
        "open_ge1_pct": 0.0,
        "high_ge1_pct": 0.0,
        "low_ge1_pct": 0.0,
        "body_disagree_pct": 0.0,
        "yuan_max_1lot": 0.0,
        "yuan_p50_1lot": 0.0,
        "streak": 0,
        "bar_rows": [],
        "problem_rows": [],
        "pair_rows": [],
    }
    if len(ready) == 0:
        result["message"] = f"{layer}：各家本地都还没有。"
        return result
    if len(ready) == 1:
        only_name = source_label(next(iter(ready)))
        result["message"] = (
            f"{layer}：本地只有「{only_name}」。"
            "请到历史回测页换另外几家来源各跑一次（新浪/东财/天勤可常驻拉；同花顺图只在回测页读图），再点检查。"
        )
        return result

    sets = {src: set(pd.Timestamp(x) for x in df.index) for src, df in ready.items()}
    universe = set().union(*sets.values())
    for ts in universe:
        hit = [src for src, stamps in sets.items() if ts in stamps]
        if len(hit) >= 4:
            result["overlap4"] += 1
            result["overlap3"] += 1
        elif len(hit) == 3:
            result["overlap3"] += 1
        elif len(hit) == 2:
            result["overlap2"] += 1
        elif len(hit) == 1:
            result["only"][source_label(hit[0])] += 1

    # 至少两家同时有的时刻，才谈「同一根」。
    multi = sorted(ts for ts in universe if sum(ts in s for s in sets.values()) >= 2)
    close_spread: list[float] = []
    open_spread: list[float] = []
    high_spread: list[float] = []
    low_spread: list[float] = []
    body_bad = 0
    ge1 = ge5 = 0
    open_ge1 = high_ge1 = low_ge1 = 0
    example_rows: list[tuple[float, str]] = []
    streak = 0
    max_streak = 0
    day_ge1 = day_n = night_ge1 = night_n = 0

    def _spread_ticks(vals: list[float]) -> float:
        return (max(vals) - min(vals)) / tick

    for ts in multi:
        rows = {src: _row_at(ready[src], ts) for src in ready if ts in sets[src]}
        rows = {src: row for src, row in rows.items() if row is not None}
        if len(rows) < 2:
            continue
        closes = [_num(row, "close") for row in rows.values()]
        opens = [_num(row, "open") for row in rows.values()]
        highs = [_num(row, "high") for row in rows.values()]
        lows = [_num(row, "low") for row in rows.values()]
        closes_f = [x for x in closes if x is not None]
        opens_f = [x for x in opens if x is not None]
        highs_f = [x for x in highs if x is not None]
        lows_f = [x for x in lows if x is not None]
        c_ticks = _spread_ticks(closes_f) if len(closes_f) >= 2 else 0.0
        o_ticks = _spread_ticks(opens_f) if len(opens_f) >= 2 else 0.0
        h_ticks = _spread_ticks(highs_f) if len(highs_f) >= 2 else 0.0
        l_ticks = _spread_ticks(lows_f) if len(lows_f) >= 2 else 0.0
        close_spread.append(c_ticks)
        open_spread.append(o_ticks)
        high_spread.append(h_ticks)
        low_spread.append(l_ticks)
        if c_ticks >= 0.5:
            ge1 += 1
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
        if c_ticks >= 4.5:
            ge5 += 1
        if o_ticks >= 0.5:
            open_ge1 += 1
        if h_ticks >= 0.5:
            high_ge1 += 1
        if l_ticks >= 0.5:
            low_ge1 += 1
        signs = {_body_sign(row) for row in rows.values()}
        signs.discard(0)
        if len(signs) > 1:
            body_bad += 1
        if _is_night(ts):
            night_n += 1
            if c_ticks >= 0.5:
                night_ge1 += 1
        else:
            day_n += 1
            if c_ticks >= 0.5:
                day_ge1 += 1
        stamp = str(pd.Timestamp(ts))[:19]
        rec: dict[str, Any] = {"时间": stamp, "夜盘": "是" if _is_night(ts) else "否"}
        signs_map: dict[str, int] = {}
        for src in COMPARE_SOURCES:
            lab = source_label(src)
            row = rows.get(src)
            if row is None:
                rec[f"{lab}开"] = None
                rec[f"{lab}高"] = None
                rec[f"{lab}低"] = None
                rec[f"{lab}收"] = None
                rec[f"{lab}阴阳"] = ""
                continue
            rec[f"{lab}开"] = _num(row, "open")
            rec[f"{lab}高"] = _num(row, "high")
            rec[f"{lab}低"] = _num(row, "low")
            rec[f"{lab}收"] = _num(row, "close")
            sign = _body_sign(row)
            rec[f"{lab}阴阳"] = {1: "阳", -1: "阴", 0: "平"}.get(sign, "")
            signs_map[lab] = sign
        body_ok = len({s for s in signs_map.values() if s != 0}) <= 1
        rec["收盘最大差跳"] = round(c_ticks, 4)
        rec["开盘最大差跳"] = round(o_ticks, 4)
        rec["最高最大差跳"] = round(h_ticks, 4)
        rec["最低最大差跳"] = round(l_ticks, 4)
        rec["1手大约差元"] = round(c_ticks * tick * multiplier, 2)
        rec["阴阳一致"] = "是" if body_ok else "否"
        result["bar_rows"].append(rec)
        if c_ticks >= 0.5 or not body_ok:
            result["problem_rows"].append(rec)
        if c_ticks >= 0.5 and len(example_rows) < 40:
            bits = [stamp]
            for src in COMPARE_SOURCES:
                lab = source_label(src)
                row = rows.get(src)
                if row is None:
                    bits.append(f"{lab} —")
                    continue
                bits.append(
                    f"{lab} 开{_num(row,'open')} 高{_num(row,'high')} "
                    f"低{_num(row,'low')} 收{_num(row,'close')}"
                )
            bits.append(f"收盘最大差 {c_ticks:.1f} 跳")
            example_rows.append((c_ticks, "  |  ".join(bits)))

    n_multi = len(multi)
    same_n = n_multi - ge1
    result["overlap_multi"] = n_multi
    result["close_same_pct"] = _pct(same_n, n_multi)
    result["close_ge1_pct"] = _pct(ge1, n_multi)
    result["close_ge5_pct"] = _pct(ge5, n_multi)
    result["close_p50_ticks"] = _quantile(close_spread, 0.5)
    result["close_p95_ticks"] = _quantile(close_spread, 0.95)
    result["close_max_ticks"] = max(close_spread) if close_spread else 0.0
    result["open_ge1_pct"] = _pct(open_ge1, n_multi)
    result["high_ge1_pct"] = _pct(high_ge1, n_multi)
    result["low_ge1_pct"] = _pct(low_ge1, n_multi)
    result["body_disagree_pct"] = _pct(body_bad, n_multi)
    result["yuan_max_1lot"] = result["close_max_ticks"] * tick * multiplier
    result["yuan_p50_1lot"] = result["close_p50_ticks"] * tick * multiplier
    result["streak"] = max_streak
    example_rows.sort(key=lambda item: item[0], reverse=True)
    result["examples"] = [item[1] for item in example_rows[:8]]

    # 最近几根并排，方便对照同花顺图。
    recent_lines: list[str] = []
    for ts in list(reversed(multi))[:5]:
        ts = pd.Timestamp(ts)
        bits = [str(ts)[:19]]
        for src in COMPARE_SOURCES:
            lab = source_label(src)
            df = ready.get(src)
            row = _row_at(df, ts) if df is not None else None
            if row is None:
                bits.append(f"{lab} —")
            else:
                bits.append(f"{lab} 收{_num(row,'close')}")
        recent_lines.append("  |  ".join(bits))
    result["recent"] = list(reversed(recent_lines))

    step = int(period) if str(period).isdigit() else 0
    pair_stats: list[str] = []
    pair_rows: list[dict[str, Any]] = []
    for i, left_src in enumerate(COMPARE_SOURCES):
        for right_src in COMPARE_SOURCES[i + 1 :]:
            a = ready.get(left_src)
            b = ready.get(right_src)
            if a is None or b is None:
                continue
            common = a.index.intersection(b.index)
            n = len(common)
            pair_name = f"{source_label(left_src)} vs {source_label(right_src)}"
            if n == 0:
                pair_stats.append(f"{pair_name}：没有同一时刻的K线。")
                pair_rows.append(
                    {
                        "对照": pair_name,
                        "同刻根数": 0,
                        "收盘一致根数": 0,
                        "收盘一致%": None,
                        "平均差跳": None,
                        "95%不超过跳": None,
                        "最大差跳": None,
                        "时间错开提示": "",
                    }
                )
                note = _detect_shift(a, b, step, tick)
                if note:
                    result["shift_notes"].append(f"{pair_name}：{note}")
                    pair_rows[-1]["时间错开提示"] = note
                continue
            diff = (a.loc[common, "close"].astype(float) - b.loc[common, "close"].astype(float)).abs() / tick
            same = int((diff < 0.5).sum())
            pair_stats.append(
                f"{pair_name}：同刻 {n} 根，"
                f"收盘一致 {same} 根（{_pct(same, n):.1f}%），"
                f"平均差 {float(diff.mean()):.2f} 跳，"
                f"95%不超过 {float(diff.quantile(0.95)):.2f} 跳，"
                f"最大 {float(diff.max()):.2f} 跳。"
            )
            note = _detect_shift(a, b, step, tick)
            if note:
                result["shift_notes"].append(f"{pair_name}：{note}")
            pair_rows.append(
                {
                    "对照": pair_name,
                    "同刻根数": n,
                    "收盘一致根数": same,
                    "收盘一致%": round(_pct(same, n), 2),
                    "平均差跳": round(float(diff.mean()), 4),
                    "95%不超过跳": round(float(diff.quantile(0.95)), 4),
                    "最大差跳": round(float(diff.max()), 4),
                    "时间错开提示": note or "",
                }
            )
    result["pairs"] = pair_stats
    result["pair_rows"] = pair_rows

    if day_n + night_n >= 20:
        result["day_night"] = (
            f"日盘同时有的 {day_n} 根里收盘差≥1跳占 {_pct(day_ge1, day_n):.1f}%；"
            f"夜盘 {night_n} 根里占 {_pct(night_ge1, night_n):.1f}%。"
        )

    impact: list[str] = []
    if n_multi == 0:
        impact.append("三家时间戳对不上，还不能按「同一根」比价。先看下面有没有提示左右标签错开。")
    else:
        impact.append(
            f"同时有两家及以上的 {n_multi} 根里，收盘差不到 1 跳的占 {result['close_same_pct']:.1f}%；"
            f"≥1 跳占 {result['close_ge1_pct']:.1f}%；≥5 跳占 {result['close_ge5_pct']:.1f}%。"
        )
        impact.append(
            f"开盘≥1跳 {result['open_ge1_pct']:.1f}%；最高价 {result['high_ge1_pct']:.1f}%；"
            f"最低价 {result['low_ge1_pct']:.1f}%；阴阳方向不一致 {result['body_disagree_pct']:.1f}%。"
        )
        impact.append(
            f"收盘价差：一半不超过 {result['close_p50_ticks']:.2f} 跳，"
            f"95%不超过 {result['close_p95_ticks']:.2f} 跳，"
            f"最大 {result['close_max_ticks']:.2f} 跳。"
            f"按 1 手、乘数 {multiplier:g}、每跳 {tick:g} 估算，"
            f"最大一根大约差 {result['yuan_max_1lot']:.2f} 元"
            f"（中位约 {result['yuan_p50_1lot']:.2f} 元）。这不是真实盈亏，只表示「如果按不同源的价格成交会差多少」。"
        )
        if result["body_disagree_pct"] >= 1:
            impact.append("有阴阳方向不一致的K线：这根收盘确认时，不同源可能一个看涨一个看跌。")
        if result["close_ge1_pct"] >= 5:
            impact.append("收盘经常差 1 跳以上：开仓价、是否触发「收盘突破」可能跟着变。")
        if result["high_ge1_pct"] >= 5 or result["low_ge1_pct"] >= 5:
            impact.append("最高或最低经常不一致：箱体上下沿、SAR、触及止损可能跟着变。")
        if result["open_ge1_pct"] >= 5:
            impact.append("开盘经常不一致：下一根开盘成交的回测填价可能跟着变。")
        if max_streak >= 3:
            impact.append(f"最长连续 {max_streak} 根收盘对不齐，不像偶然跳一下。")
        if result["close_ge1_pct"] < 5 and result["body_disagree_pct"] < 1:
            impact.append("三家收盘大体一致，换源对开平仓信号的影响通常很小。仍以你配置页当前选的那一家为准。")
    result["impact"] = impact

    bits = [
        f"{layer}：有数据的 {len(ready)} 家；"
        f"至少三家同一时刻 {result['overlap3']} 根，两家同一时刻 {result['overlap2']} 根"
    ]
    for src in COMPARE_SOURCES:
        lab = source_label(src)
        bits.append(f"{lab} {counts[lab]} 根")
    if n_multi:
        bits.append(f"同刻收盘一致 {result['close_same_pct']:.1f}%")
    result["message"] = "；".join(bits) + "。"
    _ = names
    return result


@dataclass
class QuoteQualityBook:
    """一次检查：每家各自洗得干不干净，再加上三家并排对照。"""

    status: str
    symbol: str
    period: str
    tick: float
    multiplier: float
    per_source: list[QualityReport] = field(default_factory=list)
    clean_cmp: dict[str, Any] = field(default_factory=dict)
    raw_cmp: dict[str, Any] = field(default_factory=dict)
    message: str = ""


def inspect_quote_book(
    root: Path | str,
    symbol: str,
    period: str,
    *,
    cfg: dict[str, Any] | None = None,
    max_stale_minutes: float = 90.0,
) -> QuoteQualityBook:
    """
    读本地各家的原样库和清洗库，不访问网络。

    请你先在历史回测页换来源各拉一次。这里只对照已经存下的。
    """
    period = canonical_period(period)
    symbol = str(symbol).strip()
    tick, multiplier = resolve_tick_mult(cfg, symbol)
    per = [
        inspect_quote(root, symbol, period, src, max_stale_minutes=max_stale_minutes)
        for src in COMPARE_SOURCES
    ]
    raw_store = RawBarStore(Path(root) / "data" / "klines" / "raw.sqlite")
    clean_store = BarStore(Path(root) / "data" / "klines" / "market.sqlite")
    raw_frames: dict[str, pd.DataFrame | None] = {}
    clean_frames: dict[str, pd.DataFrame | None] = {}
    for src in COMPARE_SOURCES:
        stored = storage_symbol(symbol, src)
        raw_frames[src] = raw_store.load(stored, period, src)
        clean_frames[src] = clean_store.load(stored, period)
    clean_cmp = _compare_layer(
        clean_frames, layer="清洗后（开仓看这份）", tick=tick, multiplier=multiplier, period=period
    )
    raw_cmp = _compare_layer(
        raw_frames, layer="接口原样", tick=tick, multiplier=multiplier, period=period
    )
    n_clean = clean_cmp.get("n_sources") or 0
    if all(item.status == "empty" for item in per) and n_clean == 0:
        status = "empty"
        message = (
            f"还没有 {symbol} 的{period_label(period)}库存。"
            "请到历史回测页换新浪、东方财富、天勤各跑一次（或常驻拉一次）；"
            "同花顺图请在回测页选好后读图。"
        )
    elif n_clean < 2:
        status = "watch"
        message = clean_cmp.get("message") or "本地还凑不齐两家，不能并排比价。"
    else:
        messy = (
            clean_cmp.get("close_ge1_pct", 0) >= 5
            or clean_cmp.get("body_disagree_pct", 0) >= 1
            or clean_cmp.get("close_max_ticks", 0) >= 5
            or bool(clean_cmp.get("shift_notes"))
        )
        status = "watch" if messy or any(item.status == "watch" for item in per) else "ok"
        message = clean_cmp.get("message") or ""
        if status == "ok":
            message += "各家清洗后的收盘大体一致。"
        else:
            message += "请看报告里的价差和对交易的影响。"
    return QuoteQualityBook(
        status=status,
        symbol=symbol,
        period=period,
        tick=tick,
        multiplier=multiplier,
        per_source=per,
        clean_cmp=clean_cmp,
        raw_cmp=raw_cmp,
        message=message,
    )


def _format_layer(cmp: dict[str, Any]) -> list[str]:
    lines = [cmp.get("message") or "", ""]
    lines.append("各家根数与时间范围：")
    for src in COMPARE_SOURCES:
        lab = source_label(src)
        n = (cmp.get("counts") or {}).get(lab, 0)
        span = (cmp.get("spans") or {}).get(lab, ("", ""))
        only = (cmp.get("only") or {}).get(lab, 0)
        lines.append(f"  {lab}  {n} 根  {span[0]} ~ {span[1]}  （只有自己有、别人没有的 {only} 根）")
    lines.append(
        f"至少三家同一时刻 {cmp.get('overlap3', 0)} 根；四家同一时刻 {cmp.get('overlap4', 0)} 根；"
        f"恰好两家同一时刻 {cmp.get('overlap2', 0)} 根。"
    )
    if cmp.get("pairs"):
        lines.append("")
        lines.append("两两对照：")
        lines.extend(f"  {row}" for row in cmp["pairs"])
    if cmp.get("shift_notes"):
        lines.append("")
        lines.append("时间可能整体错开一根（左右标签）：")
        lines.extend(f"  {row}" for row in cmp["shift_notes"])
    if cmp.get("day_night"):
        lines.append(cmp["day_night"])
    if cmp.get("impact"):
        lines.append("")
        lines.append("对实际交易的影响（白话，不是下单）：")
        lines.extend(f"  · {row}" for row in cmp["impact"])
    if cmp.get("recent"):
        lines.append("")
        lines.append("最近几根同刻收盘并排：")
        lines.extend(f"  {row}" for row in cmp["recent"])
    if cmp.get("examples"):
        lines.append("")
        lines.append("收盘差得最多的几根（开高低收并排）：")
        lines.extend(f"  {row}" for row in cmp["examples"])
    return lines


def format_quality_text(report: QualityReport) -> str:
    """写成给人读的一整页。"""
    lines = [
        f"行情质量  {report.symbol}  {period_label(report.period)}  {source_label(report.source)}",
        f"检查时间  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"结论  {_STATUS_CN.get(report.status, report.status)}",
        "",
        report.message,
        "",
        f"原样库  {report.raw_n} 根  {report.raw_from} ~ {report.raw_to}",
        f"清洗库  {report.clean_n} 根  {report.clean_from} ~ {report.clean_to}",
        "原样库：data/klines/raw.sqlite（接口下来后立刻存，未对齐、未重拼）",
        "清洗库：data/klines/market.sqlite（开仓看这一份）",
    ]
    if report.samples:
        lines.append("")
        lines.append("原样库里被清洗规则盯上的例子：")
        lines.extend(f"  {row}" for row in report.samples)
    lines.append("")
    lines.append("这一页只检查，不下单，也不改开平仓。")
    return "\n".join(lines) + "\n"


def format_quality_book_text(book: QuoteQualityBook) -> str:
    """三家并排的一整页。先写清洗后（开仓看这份），再写各家脏不脏、原样对照。"""
    lines = [
        f"行情质量  {book.symbol}  {period_label(book.period)}  新浪 / 东方财富 / 天勤 / 同花顺图 并排",
        f"检查时间  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"结论  {_STATUS_CN.get(book.status, book.status)}",
        f"每跳 {book.tick:g}，乘数 {book.multiplier:g}（用来把价差说成跳数和大约几元）",
        "",
        book.message,
        "",
        "用法：历史回测页换来源各拉一次，再点「检查行情质量」。这里只读已经存下的，不拉网、不下单。",
        "同花顺图只能在回测页读图，不会进实盘扫描。开仓仍只看配置页当前选的那一家。",
        "",
        "======== 一、清洗后各家并排（开仓看这份） ========",
        "",
    ]
    lines.extend(_format_layer(book.clean_cmp))
    lines.append("")
    lines.append("======== 二、各家自己洗得干不干净 ========")
    for item in book.per_source:
        lines.append("")
        lines.append(f"【{source_label(item.source)}】{_STATUS_CN.get(item.status, item.status)}")
        lines.append(f"  {item.message}")
        lines.append(
            f"  原样 {item.raw_n} 根 {item.raw_from} ~ {item.raw_to}；"
            f"清洗 {item.clean_n} 根 {item.clean_from} ~ {item.clean_to}"
        )
        if item.samples:
            lines.extend(f"  例 {row}" for row in item.samples[:4])
    lines.append("")
    lines.append("======== 三、接口原样各家并排（未对齐、未重拼，时间更容易对不齐） ========")
    lines.append("")
    lines.extend(_format_layer(book.raw_cmp))
    lines.append("")
    lines.append("原样库：data/klines/raw.sqlite")
    lines.append("清洗库：data/klines/market.sqlite（东方财富带 @eastmoney，天勤带 @tq，同花顺图带 @ths）")
    lines.append("这一页只检查，不下单，也不改开平仓。")
    return "\n".join(lines) + "\n"


def _bar_columns() -> list[str]:
    """同刻明细表头：时间 + 四家开高低收 + 差几跳。"""
    cols = ["时间", "夜盘"]
    for src in COMPARE_SOURCES:
        lab = source_label(src)
        cols.extend([f"{lab}开", f"{lab}高", f"{lab}低", f"{lab}收", f"{lab}阴阳"])
    cols.extend(["收盘最大差跳", "开盘最大差跳", "最高最大差跳", "最低最大差跳", "1手大约差元", "阴阳一致"])
    return cols


def _autosize(ws: Any, *, min_width: int = 8, max_width: int = 28) -> None:
    from openpyxl.utils import get_column_letter

    for idx, col in enumerate(ws.columns, start=1):
        longest = 0
        for cell in col[:80]:
            value = "" if cell.value is None else str(cell.value)
            longest = max(longest, min(len(value), max_width))
        ws.column_dimensions[get_column_letter(idx)].width = max(min_width, longest + 2)


def _write_table(ws: Any, columns: list[str], rows: list[dict[str, Any]]) -> None:
    """一张表：第一行表头，后面每一行一条记录。空表也留下表头，方便你自己筛。"""
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.formatting.rule import FormulaRule
    from openpyxl.utils import get_column_letter

    head_fill = PatternFill("solid", fgColor="1F4E79")
    head_font = Font(color="FFFFFF", bold=True)
    ws.append(columns)
    for cell in ws[1]:
        cell.fill = head_fill
        cell.font = head_font
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    if not rows:
        empty = {col: None for col in columns}
        empty[columns[0]] = "（这一张还没有可并排的K线）"
        rows = [empty]
    for rec in rows:
        ws.append([rec.get(col) for col in columns])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{ws.max_row}"
    warn_col = None
    if "收盘最大差跳" in columns:
        warn_col = columns.index("收盘最大差跳") + 1
    if warn_col and ws.max_row >= 2:
        letter = get_column_letter(warn_col)
        # 收盘差到 1 跳就标浅黄，差到 5 跳标浅红，方便一眼看见问题根。
        yellow = FormulaRule(
            formula=[f"AND(ISNUMBER({letter}2),{letter}2>=0.5,{letter}2<5)"],
            fill=PatternFill("solid", fgColor="FFF2CC"),
        )
        red = FormulaRule(
            formula=[f"AND(ISNUMBER({letter}2),{letter}2>=5)"],
            fill=PatternFill("solid", fgColor="F8CBAD"),
        )
        rng = f"A2:{get_column_letter(len(columns))}{ws.max_row}"
        ws.conditional_formatting.add(rng, yellow)
        ws.conditional_formatting.add(rng, red)
    _autosize(ws)


def _write_kv(ws: Any, pairs: list[tuple[str, Any]]) -> None:
    from openpyxl.styles import Font, PatternFill, Alignment

    ws.append(["项目", "内容"])
    ws["A1"].fill = PatternFill("solid", fgColor="1F4E79")
    ws["B1"].fill = PatternFill("solid", fgColor="1F4E79")
    ws["A1"].font = Font(color="FFFFFF", bold=True)
    ws["B1"].font = Font(color="FFFFFF", bold=True)
    for key, value in pairs:
        ws.append([key, value])
        ws[f"B{ws.max_row}"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 88


def write_quality_excel(path: Path, report: QualityReport | QuoteQualityBook) -> None:
    """把质量检查写成 Excel：总览 + 能筛选排序的同刻明细。"""
    from openpyxl import Workbook

    wb = Workbook()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(report, QuoteQualityBook):
        help_ws = wb.active
        help_ws.title = "说明"
        _write_kv(
            help_ws,
            [
                ("这是什么", "各家本地K线并排对照。只检查，不下单，也不改开平仓。"),
                ("怎么看", "先看「总览」和「问题K线」。表头可以筛选、按「收盘最大差跳」从大到小排。"),
                ("颜色", "浅黄＝收盘差到 1 跳；浅红＝差到 5 跳以上。差特别大时，多半是某一家读错了，不是真涨了上百万。"),
                ("清洗同刻", "开仓看的那一份。同一时刻各家开高低收都在。"),
                ("原样同刻", "接口刚下来、还没清洗的。时间更容易对不齐。"),
                ("各家库存", "每一家自己洗掉了多少脏K线。"),
                ("合约", report.symbol),
                ("周期", period_label(report.period)),
                ("检查时间", now),
            ],
        )
        overview = wb.create_sheet("总览")
        cmp = report.clean_cmp or {}
        pairs: list[tuple[str, Any]] = [
            ("合约", report.symbol),
            ("周期", period_label(report.period)),
            ("结论", _STATUS_CN.get(report.status, report.status)),
            ("检查时间", now),
            ("每跳", report.tick),
            ("乘数", report.multiplier),
            ("一句话", report.message),
            ("有数据的家数", cmp.get("n_sources") or 0),
            ("至少三家同一时刻", cmp.get("overlap3") or 0),
            ("四家同一时刻", cmp.get("overlap4") or 0),
            ("恰好两家同一时刻", cmp.get("overlap2") or 0),
            ("同刻根数", cmp.get("overlap_multi") or 0),
            ("收盘一致%", round(float(cmp.get("close_same_pct") or 0), 2)),
            ("收盘差≥1跳%", round(float(cmp.get("close_ge1_pct") or 0), 2)),
            ("收盘差≥5跳%", round(float(cmp.get("close_ge5_pct") or 0), 2)),
            ("开盘差≥1跳%", round(float(cmp.get("open_ge1_pct") or 0), 2)),
            ("最高差≥1跳%", round(float(cmp.get("high_ge1_pct") or 0), 2)),
            ("最低差≥1跳%", round(float(cmp.get("low_ge1_pct") or 0), 2)),
            ("阴阳方向不一致%", round(float(cmp.get("body_disagree_pct") or 0), 2)),
            ("收盘差一半不超过跳", round(float(cmp.get("close_p50_ticks") or 0), 4)),
            ("收盘差95%不超过跳", round(float(cmp.get("close_p95_ticks") or 0), 4)),
            ("收盘最大差跳", round(float(cmp.get("close_max_ticks") or 0), 4)),
            ("最大一根大约差元(1手)", round(float(cmp.get("yuan_max_1lot") or 0), 2)),
            ("中位大约差元(1手)", round(float(cmp.get("yuan_p50_1lot") or 0), 2)),
            ("最长连续对不齐", cmp.get("streak") or 0),
            ("日夜盘", cmp.get("day_night") or ""),
        ]
        for idx, line in enumerate(cmp.get("impact") or [], start=1):
            pairs.append((f"对交易的影响{idx}", line))
        for idx, line in enumerate(cmp.get("shift_notes") or [], start=1):
            pairs.append((f"时间可能错开{idx}", line))
        _write_kv(overview, pairs)

        stock = wb.create_sheet("各家库存")
        stock_rows: list[dict[str, Any]] = []
        for item in report.per_source:
            stock_rows.append(
                {
                    "来源": source_label(item.source),
                    "结论": _STATUS_CN.get(item.status, item.status),
                    "原样根数": item.raw_n,
                    "原样从": item.raw_from,
                    "原样到": item.raw_to,
                    "清洗根数": item.clean_n,
                    "清洗从": item.clean_from,
                    "清洗到": item.clean_to,
                    "清洗丢掉": item.dropped_n,
                    "时间对不齐": item.misaligned_n,
                    "开盘假戳": item.open_stamp_n,
                    "小数过长": item.long_decimal_n,
                    "高低价不合理": item.ohlc_bad_n,
                    "缺口": item.gap_note,
                    "距今分钟": item.stale_minutes,
                    "是否偏旧": "是" if item.stale else "否",
                    "说明": item.message,
                }
            )
        _write_table(
            stock,
            [
                "来源", "结论", "原样根数", "原样从", "原样到", "清洗根数", "清洗从", "清洗到",
                "清洗丢掉", "时间对不齐", "开盘假戳", "小数过长", "高低价不合理",
                "缺口", "距今分钟", "是否偏旧", "说明",
            ],
            stock_rows,
        )

        problem = wb.create_sheet("问题K线")
        problems = list(cmp.get("problem_rows") or [])
        problems.sort(key=lambda row: float(row.get("收盘最大差跳") or 0), reverse=True)
        _write_table(problem, _bar_columns(), problems)

        clean_sheet = wb.create_sheet("清洗同刻")
        _write_table(clean_sheet, _bar_columns(), list(cmp.get("bar_rows") or []))

        pair_sheet = wb.create_sheet("两两对照")
        _write_table(
            pair_sheet,
            ["对照", "同刻根数", "收盘一致根数", "收盘一致%", "平均差跳", "95%不超过跳", "最大差跳", "时间错开提示"],
            list(cmp.get("pair_rows") or []),
        )

        raw_sheet = wb.create_sheet("原样同刻")
        _write_table(raw_sheet, _bar_columns(), list((report.raw_cmp or {}).get("bar_rows") or []))
    else:
        help_ws = wb.active
        help_ws.title = "总览"
        _write_kv(
            help_ws,
            [
                ("合约", report.symbol),
                ("周期", period_label(report.period)),
                ("来源", source_label(report.source)),
                ("结论", _STATUS_CN.get(report.status, report.status)),
                ("检查时间", now),
                ("一句话", report.message),
                ("原样根数", report.raw_n),
                ("原样从", report.raw_from),
                ("原样到", report.raw_to),
                ("清洗根数", report.clean_n),
                ("清洗从", report.clean_from),
                ("清洗到", report.clean_to),
                ("清洗丢掉", report.dropped_n),
                ("时间对不齐", report.misaligned_n),
                ("开盘假戳", report.open_stamp_n),
                ("小数过长", report.long_decimal_n),
                ("高低价不合理", report.ohlc_bad_n),
                ("缺口", report.gap_note),
            ],
        )
        sample_ws = wb.create_sheet("脏数据例子")
        _write_table(
            sample_ws,
            ["例子"],
            [{"例子": row} for row in (report.samples or [])],
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def write_quality_report(root: Path | str, report: QualityReport | QuoteQualityBook) -> Path:
    """带时间的一份不覆盖旧的；同时更新 data/行情质量.xlsx。"""
    base = Path(root)
    folder = base / "data"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    dated = folder / f"行情质量-{report.symbol}-{period_label(report.period)}-{stamp}.xlsx"
    write_quality_excel(dated, report)
    latest = folder / LATEST_NAME
    try:
        write_quality_excel(latest, report)
    except Exception:  # noqa: BLE001
        # 最新这一份正被 Excel 打开时，带时间的那份仍然能写上。
        pass
    return dated


def summarize_market_health(root: Path | str) -> pd.DataFrame:
    """按合约、周期、来源和小时汇总逐轮延迟；只读 market_health.jsonl。"""
    base = Path(root) / "data"
    records: list[dict[str, Any]] = []
    for name in ("market_health.jsonl.1", "market_health.jsonl"):
        path = base / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                records.append(row)
    columns = [
        "symbol",
        "period",
        "source",
        "时段",
        "样本数",
        "下载平均ms",
        "下载P95ms",
        "下载最大ms",
        "信号年龄平均分钟",
        "信号年龄最大分钟",
        "计算平均ms",
        "计算P95ms",
        "计算最大ms",
        "扫描间隔平均秒",
        "陈旧次数",
        "缓存回退次数",
        "断流次数",
        "异常分类",
    ]
    if not records:
        return pd.DataFrame(columns=columns)
    frame = pd.DataFrame(records)
    for name in ("received_at", "ts"):
        if name not in frame:
            frame[name] = pd.NaT
    for name in ("stale", "from_cache"):
        if name not in frame:
            frame[name] = False
    for name in ("status", "issue", "symbol", "period", "source"):
        if name not in frame:
            frame[name] = ""
    frame["issue"] = frame["issue"].fillna("")
    received = pd.to_datetime(frame["received_at"], errors="coerce", utc=True)
    fallback = pd.to_datetime(frame["ts"], errors="coerce", utc=True)
    frame["时段"] = (
        received.fillna(fallback)
        .dt.tz_convert("Asia/Shanghai")
        .dt.floor("h")
        .dt.strftime("%Y-%m-%d %H:00")
    )
    for name in ("download_ms", "signal_bar_age_minutes", "evaluation_ms", "scan_gap_sec"):
        if name not in frame:
            frame[name] = float("nan")
        frame[name] = pd.to_numeric(frame[name], errors="coerce")
    frame["_stale"] = frame["stale"].fillna(False).astype(bool)
    frame["_cache"] = frame["from_cache"].fillna(False).astype(bool)
    frame["_error"] = frame["status"].eq("error")
    grouped = (
        frame.groupby(["symbol", "period", "source", "时段"], dropna=False)
        .agg(
            样本数=("source", "size"),
            下载平均ms=("download_ms", "mean"),
            下载P95ms=("download_ms", lambda values: values.quantile(0.95)),
            下载最大ms=("download_ms", "max"),
            信号年龄平均分钟=("signal_bar_age_minutes", "mean"),
            信号年龄最大分钟=("signal_bar_age_minutes", "max"),
            计算平均ms=("evaluation_ms", "mean"),
            计算P95ms=("evaluation_ms", lambda values: values.quantile(0.95)),
            计算最大ms=("evaluation_ms", "max"),
            扫描间隔平均秒=("scan_gap_sec", "mean"),
            陈旧次数=("_stale", "sum"),
            缓存回退次数=("_cache", "sum"),
            断流次数=("_error", "sum"),
            异常分类=("issue", lambda values: "；".join(sorted({str(x) for x in values if x}))),
        )
        .reset_index()
    )
    numeric = grouped.select_dtypes(include="number").columns
    grouped[numeric] = grouped[numeric].round(3)
    return grouped[columns].sort_values(["时段", "symbol", "period", "source"]).reset_index(drop=True)


def write_market_health_summary(root: Path | str) -> Path:
    """生成 data/行情延迟摘要.xlsx；没有样本时也写出带表头的空表。"""
    table = summarize_market_health(root)
    path = Path(root) / "data" / "行情延迟摘要.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        table.to_excel(writer, sheet_name="按小时汇总", index=False)
        sheet = writer.sheets["按小时汇总"]
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查本地K线质量，不访问网络、不下单")
    parser.add_argument("--symbol", default="", help="合约，例如 fu2701")
    parser.add_argument("--period", default="5", help="周期，例如 5")
    parser.add_argument("--source", default="", help="只查这一家；空=各家并排")
    parser.add_argument("--market-health", action="store_true", help="只汇总实时行情延迟日志")
    args = parser.parse_args(argv)
    from src.utils import load_config
    from src.utils.paths import project_root

    root = project_root()
    if args.market_health:
        table = summarize_market_health(root)
        path = write_market_health_summary(root)
        print(f"行情延迟样本 {len(table)} 组，已保存 {path}")
        return 0 if len(table) else 1
    cfg = load_config()
    symbol = str(args.symbol or ((cfg.get("strategy") or {}).get("symbols") or ["fu2701"])[0])
    period = str(args.period or (cfg.get("strategy") or {}).get("timeframe") or "5")
    if str(args.source).strip():
        report: QualityReport | QuoteQualityBook = inspect_quote(root, symbol, period, args.source)
    else:
        report = inspect_quote_book(root, symbol, period, cfg=cfg)
    path = write_quality_report(root, report)
    print(report.message)
    print(f"已保存 {path}")
    return 0 if report.status != "empty" else 1


if __name__ == "__main__":
    raise SystemExit(main())
