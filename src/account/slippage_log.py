"""
实盘滑点记录与统计。

功能：每笔成交回报记账时，把「理论价 vs 实际成交价」的差追加到 data/slippage_log.csv。
     复盘页尾部显示汇总：各方向的滑点中位数、70 分位，并给出建议回测滑点。
原理：滑点统一折算成跳数，且永远是「对你不利为正」：
     买入（开多/平空）= (成交价 − 理论价) / 每跳；卖出（开空/平多）= (理论价 − 成交价) / 每跳。
     理论价：开仓=信号给的入场价（信号K线收盘价）；止损平仓=止损价；其他平仓没有理论价，留空。
     GUI 假定成交的滑点恒为 0，只留记录；真实滑点要看 CTP 成交回报。
     build_slippage_calibration：把实盘建议滑点与回测假设滑点对齐，产出带 diff/样本数/生效日期/calibrated 的结构化校准记录，只供展示与引用，不改写配置或下单。
"""

from __future__ import annotations

import csv
import json
import math
import os
import statistics
from bisect import bisect_right
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

FILE_NAME = "slippage_log.csv"
CALIBRATION_FILE = "slippage_calibration.json"

_FIELDS = [
    "ts",
    "symbol",
    "action",
    "volume",
    "theory_price",
    "fill_price",
    "slip_ticks",
    "tick",
    "executor",
    "dry_run",
    "account",
]


def _csv_path(root: Path | str) -> Path:
    return Path(root) / "data" / FILE_NAME


def _migrate_header(path: Path) -> None:
    """
    老文件只有 10 列、没有 account。读或追加前先补成 11 列。

    只补列，不改历史行语义；旧行的 account 留空，仍算「未分账」。
    迁移失败不影响后续读写，最多继续按旧列追加。
    """
    if not path.exists():
        return
    try:
        with path.open("r", newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            if not header:
                return
            if header == _FIELDS or "account" in header:
                return
            rows = list(reader)
    except OSError:
        return
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        with temp.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(_FIELDS)
            for row in rows:
                data = dict(zip(header, row))
                writer.writerow([data.get(name, "") for name in _FIELDS])
        os.replace(temp, path)
    except OSError:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass


def record_fill(
    root: Path | str,
    *,
    symbol: str,
    action: str,
    volume: int,
    theory_price: float | None,
    fill_price: float,
    tick: float,
    side_before: str = "",
    executor: str = "",
    dry_run: bool = True,
    assumed_fill: bool = False,
    account: str = "",
) -> float | None:
    """
    追加一条成交的滑点记录。返回滑点跳数（算不出时返回 None）。

    side_before：平仓时用——平多是卖出，平空是买入。开仓不用传。
    account：sim / live，用来分清仿真成交和真实成交；旧记录没有这一列，读的时候按「未分账」算。
    """
    fill = float(fill_price or 0)
    if fill <= 0:
        return None
    tick = float(tick or 0)
    if tick <= 0:
        tick = 1.0
    theory = float(theory_price or 0)
    action_key = str(action or "").strip().lower()
    slip: float | None = None
    if theory > 0:
        # GUI 只有点击成功的假定成交，参考价差不能冒充真实滑点。
        if assumed_fill:
            slip = 0.0
        # 买入方向成交价越高越亏；卖出方向越低越亏。不利方向记正数。
        elif action_key == "buy_open" or (action_key == "close" and side_before == "short"):
            slip = (fill - theory) / tick
        elif action_key == "sell_open" or (action_key == "close" and side_before == "long"):
            slip = (theory - fill) / tick
    path = _csv_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    _migrate_header(path)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if new:
            writer.writerow(_FIELDS)
        writer.writerow(
            [
                datetime.now().isoformat(timespec="seconds"),
                symbol,
                action,
                int(volume or 0),
                f"{theory:g}" if theory > 0 else "",
                f"{fill:g}",
                f"{slip:.2f}" if slip is not None else "",
                f"{tick:g}",
                executor,
                "1" if dry_run else "0",
                account,
            ]
        )
    return slip


def load_rows(root: Path | str) -> list[dict[str, str]]:
    """读出全部滑点记录。文件不存在时返回空；老表头先自动补 account 列。"""
    path = _csv_path(root)
    if not path.exists():
        return []
    _migrate_header(path)
    with path.open("r", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return [{name: row.get(name) or "" for name in _FIELDS} for row in rows]


def _percentile(values: list[float], pct: float) -> float:
    """线性插值分位数。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _stat_line(values: list[float]) -> tuple[int, float, float, float]:
    """(条数, 均值, 中位, 70分位)。"""
    if not values:
        return 0, 0.0, 0.0, 0.0
    return len(values), statistics.fmean(values), statistics.median(values), _percentile(values, 0.7)


def summarize(
    root: Path | str,
    *,
    real_only: bool = True,
    account: str | None = None,
) -> dict[str, Any]:
    """
    汇总滑点。默认只看真实成交（dry_run=0 且 executor=ctp 的行）；没有真实成交时退回全部记录。

    account 传 sim / live 时只统计这一本柜台的记录；旧记录没有 account 列，不会被算进任何一本。
    返回 dict：rows 用到的记录数、overall=(n,均值,中位,p70)、suggested 建议滑点跳数（p70 向上取到 0.5）。
    """
    rows = load_rows(root)
    if account is not None:
        rows = [row for row in rows if str(row.get("account") or "") == account]
    slips: list[float] = []
    real_slips: list[float] = []
    for row in rows:
        raw = str(row.get("slip_ticks") or "").strip()
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        slips.append(value)
        if str(row.get("dry_run") or "1") == "0" and str(row.get("executor") or "") == "ctp":
            real_slips.append(value)
    used = real_slips if (real_only and real_slips) else slips
    n, mean, median, p70 = _stat_line(used)
    # 建议回测滑点：70 分位向上取到 0.5 跳，留一点余量。
    suggested = math.ceil(max(p70, 0.0) * 2) / 2 if n else 0.0
    return {
        "rows": len(rows),
        "real_rows": len(real_slips),
        "n": n,
        "mean": mean,
        "median": median,
        "p70": p70,
        "suggested": suggested,
        "used_real": bool(real_slips),
    }


def product_of(symbol: str) -> str:
    """fu2701 → fu，SA2701 → sa。分品种看滑点时不必区分具体月份。"""
    text = str(symbol or "").strip()
    letters = "".join(ch for ch in text if ch.isalpha())
    return letters.lower() or text


def _slip_value(row: dict[str, Any]) -> float | None:
    try:
        return float(str(row.get("slip_ticks") or "").strip())
    except ValueError:
        return None


def _is_real(row: dict[str, Any]) -> bool:
    return str(row.get("dry_run") or "1") == "0" and str(row.get("executor") or "") == "ctp"


def _group_stats(rows: list[dict[str, Any]], key_of, *, min_n: int = 3) -> list[dict[str, Any]]:
    """按某个维度分组，优先真实成交；样本不足 min_n 的不进结论。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        value = _slip_value(row)
        if value is None:
            continue
        key = str(key_of(row) or "").strip()
        if not key:
            continue
        groups.setdefault(key, []).append(row)
    out: list[dict[str, Any]] = []
    for key, items in groups.items():
        real = [row for row in items if _is_real(row)]
        used = real or items
        values = [value for row in used if (value := _slip_value(row)) is not None]
        if len(values) < max(1, int(min_n)):
            continue
        n, _mean, median, p70 = _stat_line(values)
        out.append(
            {
                "key": key,
                "n": n,
                "median": median,
                "p70": p70,
                "suggested": math.ceil(max(p70, 0.0) * 2) / 2,
                "used_real": bool(real),
            }
        )
    out.sort(key=lambda item: (-int(item["n"]), str(item["key"])))
    return out


def summarize_by_side(root: Path | str) -> list[dict[str, Any]]:
    """按买卖方向给建议。买入和卖出的滑点来源不完全一样，不能直接平均。"""

    def key_of(row: dict[str, Any]) -> str:
        action = str(row.get("action") or "").upper()
        if action.startswith("BUY"):
            return "买"
        if action.startswith("SELL"):
            return "卖"
        return ""

    return _group_stats(load_rows(root), key_of)


def _session_from_table(cfg: dict[str, Any], stamp: str, symbol: str) -> str:
    """按品种与合约表的时段判断这笔成交属于日盘还是夜盘；判不出返回空串。"""
    if not stamp or not symbol:
        return ""
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if when.tzinfo is not None:
        when = when.astimezone().replace(tzinfo=None)
    try:
        from src.utils.session_clock import (
            in_clock_window,
            parse_hhmm,
            sessions_for_symbol,
            split_day_night_sessions,
        )

        day, night = split_day_night_sessions(sessions_for_symbol(cfg, symbol))
    except Exception:  # noqa: BLE001
        return ""
    if any(in_clock_window(parse_hhmm(start), parse_hhmm(end), when) for start, end in night):
        return "夜盘"
    if any(in_clock_window(parse_hhmm(start), parse_hhmm(end), when) for start, end in day):
        return "日盘"
    return "其他"


def _session_from_hour(stamp: str) -> str:
    """没有合约时段表时，按成交钟点粗分日盘、夜盘和其他。"""
    try:
        hour = int(stamp[11:13])
    except (TypeError, ValueError):
        return ""
    if 9 <= hour < 15:
        return "日盘"
    if hour >= 21 or hour < 3:
        return "夜盘"
    return "其他"


def summarize_by_session(
    root: Path | str,
    cfg: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    按日盘 / 夜盘给建议。夜盘流动性差时，滑点常比日盘大。

    给了 cfg 就按品种与合约表的时段判：生猪、鸡蛋这些没有夜盘的品种，
    22 点的记录不会被算进夜盘；没给 cfg 才退回按钟点粗分。
    """

    def key_of(row: dict[str, Any]) -> str:
        stamp = str(row.get("ts") or "")
        if cfg is not None:
            label = _session_from_table(cfg, stamp, str(row.get("symbol") or ""))
            if label:
                return label
        return _session_from_hour(stamp) if stamp else ""

    return _group_stats(load_rows(root), key_of)


def _naive_iso_datetime(value: Any) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is not None:
        when = when.astimezone().replace(tzinfo=None)
    return when


def _load_market_bars(
    root: Path | str,
    cfg: dict[str, Any] | None,
    symbol: str,
) -> Any | None:
    """只读本地清洗 K 线；优先配置的数据源，兼容未带来源后缀的旧库存。"""
    path = Path(root) / "data" / "klines" / "market.sqlite"
    if not path.exists():
        return None
    from src.data.bar_store import BarStore
    from src.data.periods import canonical_period
    from src.data.quote_check import storage_symbol

    data_cfg = (cfg or {}).get("data") or {}
    strategy_cfg = (cfg or {}).get("strategy") or {}
    source = data_cfg.get("source")
    period = canonical_period(str(strategy_cfg.get("timeframe") or "15"))
    store = BarStore(path)
    for stored in dict.fromkeys((storage_symbol(symbol, source), symbol)):
        bars = store.load(stored, period)
        if bars is not None and len(bars):
            return bars
    return None


def summarize_by_market_state(
    root: Path | str,
    cfg: dict[str, Any] | None = None,
    *,
    min_n: int = 3,
) -> list[dict[str, Any]]:
    """
    按「趋势/震荡 + 高波动/低波动」组合标签统计成交滑点。

    ADX 达到配置的 adx_trend 为趋势，否则为震荡；ATR 历史分位 >= 0.5 为高波动。
    K 线只读 data/klines/market.sqlite，周期取 strategy.timeframe（默认 15 分钟），
    按 symbol 缓存。K 线是右标签，成交取当时已收盘的最后一根，并逐根复用
    evaluate_market_state；无法定位、K 线不足或样本不足 min_n 的桶不进结论。
    """
    rows = load_rows(root)
    if not rows:
        return []
    try:
        from src.strategy.market_state import MarketFilterParams, evaluate_market_state

        # 这里只描述市场，不受策略过滤开关影响。
        params = replace(MarketFilterParams.from_cfg(cfg), enabled=True)
    except Exception:  # noqa: BLE001 - 缺少行情依赖时，成交质量其它部分仍应可读
        return []

    bars_cache: dict[str, Any | None] = {}
    times_cache: dict[str, list[datetime]] = {}
    state_cache: dict[tuple[str, int], str] = {}

    def label_for(symbol: str, stamp: str) -> str:
        when = _naive_iso_datetime(stamp)
        if when is None or not symbol:
            return ""
        if symbol not in bars_cache:
            try:
                bars = _load_market_bars(root, cfg, symbol)
                if bars is None or not len(bars) or not {"high", "low", "close"} <= set(bars.columns):
                    bars = None
                    times: list[datetime] = []
                else:
                    bars = bars.sort_index()
                    converted = [_naive_iso_datetime(value) for value in bars.index]
                    if any(value is None for value in converted):
                        bars = None
                        times = []
                    else:
                        times = [value for value in converted if value is not None]
            except Exception:  # noqa: BLE001 - 单个品种无行情不阻断其它品种
                bars = None
                times = []
            bars_cache[symbol] = bars
            times_cache[symbol] = times
        bars = bars_cache[symbol]
        if bars is None:
            return ""
        position = bisect_right(times_cache[symbol], when) - 1
        if position < 0:
            return ""
        cache_key = (symbol, position)
        if cache_key in state_cache:
            return state_cache[cache_key]

        label = ""
        window = bars.iloc[: position + 1]
        if len(window) >= params.adx_period + 5:
            try:
                state = evaluate_market_state(window, params)
                adx = float(state.adx)
                atr_rank = float(state.atr_rank)
                if math.isfinite(adx) and math.isfinite(atr_rank):
                    trend = "趋势" if adx >= params.adx_trend else "震荡"
                    volatility = "高波动" if atr_rank >= 0.5 else "低波动"
                    label = f"{trend}/{volatility}"
            except Exception:  # noqa: BLE001 - 异常行情只跳过这一桶
                pass
        state_cache[cache_key] = label
        return label

    tagged: list[dict[str, Any]] = []
    for row in rows:
        if _slip_value(row) is None:
            continue
        label = label_for(str(row.get("symbol") or ""), str(row.get("ts") or ""))
        if label:
            tagged.append({**row, "_market_state": label})
    return _group_stats(tagged, lambda row: row["_market_state"], min_n=min_n)


# 复盘里算「拒单 / 被拦」的事件。只统计没发出去或柜台退回的，不把正常撤单算进来。
_REJECT_EVENTS = {
    "order_fail": "柜台拒单",
    "order_no_price": "没有价格",
    "risk_block": "风控拦截",
    "symbol_frozen": "品种冻结",
    "strategy_health": "健康停开",
    "stale_block_open": "K线过期",
    "slip_block_open": "追价过大",
}


def reject_summary(root: Path | str, *, days: int = 30) -> dict[str, Any]:
    """
    近 N 天没发出去或被退回的单，按品种和原因分开数。

    数据来自 data/events.jsonl；没有文件时返回空统计，调用方要如实说「没有记录」。
    """
    path = Path(root) / "data" / "events.jsonl"
    by_session = {"日盘": 0, "夜盘": 0, "其他": 0}
    out: dict[str, Any] = {
        "total": 0,
        "by_symbol": [],
        "by_reason": [],
        "by_session": [{"key": key, "n": 0} for key in by_session],
    }
    if not path.exists():
        return out
    cutoff = datetime.now().timestamp() - max(1, int(days)) * 86400
    by_symbol: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        label = _REJECT_EVENTS.get(str(item.get("event") or ""))
        if label is None:
            continue
        when = _event_ts(item.get("ts"))
        if when is not None and when < cutoff:
            continue
        symbol = product_of(str(item.get("symbol") or "")) or "—"
        by_symbol[symbol] = by_symbol.get(symbol, 0) + 1
        by_reason[label] = by_reason.get(label, 0) + 1
        session = _session_from_hour(str(item.get("ts") or "")) or "其他"
        by_session[session] += 1
        out["total"] += 1
    out["by_symbol"] = sorted(
        ({"key": key, "n": n} for key, n in by_symbol.items()),
        key=lambda item: (-item["n"], item["key"]),
    )
    out["by_reason"] = sorted(
        ({"key": key, "n": n} for key, n in by_reason.items()),
        key=lambda item: (-item["n"], item["key"]),
    )
    out["by_session"] = [{"key": key, "n": n} for key, n in by_session.items()]
    return out


def _event_ts(value: Any) -> float | None:
    text = str(value or "")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def quality_text(root: Path | str, cfg: dict[str, Any] | None = None) -> str:
    """复盘尾部「成交质量」：方向、时段、市场状态、拒单。样本不足时直说。"""
    lines = ["—— 成交质量（滑点与拒单） ——"]
    side = summarize_by_side(root)
    if side:
        lines.append("按方向：" + "；".join(
            f"{item['key']} {item['n']}笔 → 建议 {item['suggested']:g} 跳" for item in side
        ))
    else:
        lines.append("按方向：真实样本不足 3 笔，先不改方向参数。")

    session = summarize_by_session(root, cfg)
    if session:
        lines.append("按时段：" + "；".join(
            f"{item['key']} {item['n']}笔 → 建议 {item['suggested']:g} 跳" for item in session
        ))
    else:
        lines.append("按时段：真实样本不足 3 笔，先不改夜盘参数。")

    market = summarize_by_market_state(root, cfg)
    if market:
        lines.append("按市场状态：" + "；".join(
            f"{item['key']} {item['n']}笔 → 建议 {item['suggested']:g} 跳" for item in market
        ))
    else:
        lines.append("按市场状态：市场状态样本不足或缺少行情，先不下结论。")

    rejects = reject_summary(root)
    if not rejects["total"]:
        lines.append("拒单：近 30 天没有记录（或还没点过「启动常驻」）。")
    else:
        reasons = "、".join(f"{item['key']} {item['n']} 次" for item in rejects["by_reason"])
        symbols = "、".join(f"{item['key']} {item['n']} 次" for item in rejects["by_symbol"][:5])
        sessions = "、".join(f"{item['key']} {item['n']} 次" for item in rejects["by_session"])
        lines.append(f"拒单：近 30 天 {rejects['total']} 次（{reasons}）")
        lines.append(f"  分品种  {symbols}")
        lines.append(f"  分时段  {sessions}")
    return "\n".join(lines)


def summarize_by_symbol(
    root: Path | str,
    *,
    real_only: bool = True,
    min_n: int = 3,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """
    按品种汇总滑点，给出各自建议跳数。

    每个品种优先只用真实成交；该品种没有真实成交时才退回全部记录。
    样本不足 min_n 的品种不参与建议，避免一笔成交就改参数。
    """
    rows = load_rows(root)
    groups: dict[str, list[float]] = {}
    real: dict[str, list[float]] = {}
    for row in rows:
        raw = str(row.get("slip_ticks") or "").strip()
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        key = product_of(str(row.get("symbol") or ""))
        if not key:
            continue
        groups.setdefault(key, []).append(value)
        if str(row.get("dry_run") or "1") == "0" and str(row.get("executor") or "") == "ctp":
            real.setdefault(key, []).append(value)
    out: list[dict[str, Any]] = []
    for key, values in groups.items():
        real_values = real.get(key) or []
        used = real_values if (real_only and real_values) else values
        n, _mean, median, p70 = _stat_line(used)
        if n < min_n:
            continue
        out.append(
            {
                "symbol": key,
                "n": n,
                "median": median,
                "p70": p70,
                "suggested": math.ceil(max(p70, 0.0) * 2) / 2,
                "used_real": bool(real_values),
            }
        )
    out.sort(key=lambda item: (-int(item["n"]), str(item["symbol"])))
    return out[:limit]


def summarize_text(root: Path | str) -> str:
    """复盘页尾部展示的滑点校准段。"""
    info = summarize(root)
    lines = ["—— 实盘滑点校准 ——"]
    if not info["rows"]:
        lines.append("还没有成交滑点记录。真实成交（CTP）后会自动记到 data/slippage_log.csv。")
        return "\n".join(lines)
    scope = "真实成交" if info["used_real"] else "全部记录（含GUI假定成交，滑点恒为0，仅供参考）"
    lines.append(
        f"样本 {info['n']} 笔（{scope}，共 {info['rows']} 行记录）："
        f"均值 {info['mean']:+.2f} 跳，中位 {info['median']:+.2f} 跳，70分位 {info['p70']:+.2f} 跳"
    )
    if info["n"]:
        lines.append(
            f"建议回测滑点：{info['suggested']:g} 跳（按 70 分位向上取 0.5）。"
            "正数=成交价比理论价差，0 附近=基本按理论价成交。"
        )
    if not info["used_real"]:
        lines.append("目前没有 CTP 真实成交样本；GUI 假定成交得不出真实滑点。")
    per_symbol = summarize_by_symbol(root)
    if len(per_symbol) >= 2:
        lines.append("分品种建议（各品种 70 分位向上取 0.5 跳，满 3 笔才列）：")
        for item in per_symbol:
            suffix = "" if item["used_real"] else "（含假定成交）"
            lines.append(
                f"  {item['symbol']}  {item['n']}笔  70分位 {item['p70']:+.2f}  → "
                f"{item['suggested']:g} 跳{suffix}"
            )
    return "\n".join(lines)


def build_slippage_calibration(
    root: Path | str,
    *,
    cfg: dict[str, Any] | None = None,
    backtest_assumed: float | None = None,
    account: str | None = "live",
) -> dict[str, Any]:
    """
    生成滑点校准记录：实盘建议滑点 vs 回测假设滑点的差异，带样本数与生效日期。

    只产出结构化记录，供复盘/评审/回测引用；不会自动改写配置或下单行为。
    - 实盘值取自 summarize(root, account=account)（median/p70/suggested/n/real_rows/used_real）。
      account 默认 "live"，只统计真实实盘成交，避免仿真(sim)混入导致校准失真；显式传 None 才统计全部账户。
    - backtest_assumed 缺省时从 cfg["backtest"]["slippage_ticks"] 读；仍读不到记 0.0 并在 reason 注明。
    - diff = live_suggested - backtest_assumed，正数=实盘比回测假设更不利。
    - calibrated=True 仅当有 CTP 真实成交且样本 n>=3；否则 False，reason 写明原因。
    任何异常都兜底返回 0.0/0/""/False，不抛异常。
    """
    reasons: list[str] = []
    try:
        info = summarize(root, account=account)
    except Exception:  # noqa: BLE001 - 校准记录只读展示，失败也要返回兜底结构
        info = {}
    median = float(info.get("median") or 0.0)
    p70 = float(info.get("p70") or 0.0)
    suggested = float(info.get("suggested") or 0.0)
    n = int(info.get("n") or 0)
    real_rows = int(info.get("real_rows") or 0)
    used_real = bool(info.get("used_real"))
    rows = int(info.get("rows") or 0)

    assumed = backtest_assumed
    if assumed is None:
        try:
            assumed = (cfg or {}).get("backtest", {}).get("slippage_ticks")
        except Exception:  # noqa: BLE001
            assumed = None
    if assumed is None or assumed == "":
        assumed_val = 0.0
        reasons.append("未提供回测假设滑点")
    else:
        try:
            assumed_val = float(assumed)
        except (TypeError, ValueError):
            assumed_val = 0.0
            reasons.append("回测假设滑点无效")

    diff = suggested - assumed_val

    calibrated = used_real and n >= 3
    if not rows:
        reasons.append("无成交滑点记录")
    elif not used_real:
        reasons.append("无 CTP 真实成交，仅有 GUI 假定成交")
    elif n < 3:
        reasons.append("真实成交样本不足 3 笔")
    else:
        reasons.append("已基于真实成交样本校准")

    return {
        "live_median": median,
        "live_p70": p70,
        "live_suggested": suggested,
        "backtest_assumed": assumed_val,
        "diff": diff,
        "sample_n": n,
        "real_rows": real_rows,
        "used_real": used_real,
        "effective_date": datetime.now().strftime("%Y-%m-%d"),
        "calibrated": calibrated,
        "reason": "；".join(reasons),
    }


def save_slippage_calibration(
    root: Path | str,
    *,
    cfg: dict[str, Any] | None = None,
    backtest_assumed: float | None = None,
    account: str | None = "live",
) -> dict[str, Any]:
    """生成并保存校准记录到 data/slippage_calibration.json，返回写入的内容。默认只统计 live 实盘。"""
    record = build_slippage_calibration(
        root,
        cfg=cfg,
        backtest_assumed=backtest_assumed,
        account=account,
    )
    record["generated_at"] = datetime.now().isoformat(timespec="seconds")
    path = Path(root) / "data" / CALIBRATION_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return record


def load_slippage_calibration(root: Path | str) -> dict[str, Any]:
    """读取校准记录；文件不存在、不是 JSON 对象或解析失败时返回空 dict。"""
    path = Path(root) / "data" / CALIBRATION_FILE
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return record if isinstance(record, dict) else {}


def resolve_backtest_slippage(
    root: Path | str,
    cfg: dict[str, Any] | None = None,
    *,
    use_calibration: bool | None = None,
) -> dict[str, Any]:
    """决定回测滑点；只有开关开启且已有达标校准记录时才采用实盘建议值。"""
    assumed = 1.0
    backtest: dict[str, Any] = {}
    try:
        raw_backtest = (cfg or {}).get("backtest") or {}
        if isinstance(raw_backtest, dict):
            backtest = raw_backtest
        raw_assumed = backtest.get("slippage_ticks")
        if raw_assumed not in (None, ""):
            candidate = float(raw_assumed)
            if math.isfinite(candidate):
                assumed = candidate
    except Exception:  # noqa: BLE001 - 配置无效时使用统一默认值
        assumed = 1.0

    fallback: dict[str, Any] = {
        "value": assumed,
        "source": "配置默认",
        "calibrated": False,
        "reason": "",
        "diff": 0.0,
        "effective_date": "",
        "sample_n": 0,
        "live_suggested": 0.0,
        "backtest_assumed": assumed,
    }
    result = dict(fallback)
    try:
        from src.strategy.types import config_flag

        if use_calibration is None:
            enabled = config_flag(backtest.get("use_slippage_calibration"), False)
        else:
            enabled = config_flag(use_calibration, False)
        record = load_slippage_calibration(root)
        if record:
            live_suggested = float(record.get("live_suggested") or 0.0)
            if not math.isfinite(live_suggested):
                live_suggested = 0.0
            result.update(
                {
                    "diff": live_suggested - assumed,
                    "effective_date": str(record.get("effective_date") or ""),
                    "sample_n": int(record.get("sample_n") or 0),
                    "live_suggested": live_suggested,
                }
            )

        if not enabled:
            result["reason"] = "校准开关未开启，使用配置默认"
        elif not record:
            result["reason"] = "无校准记录，使用配置默认"
        elif not record.get("calibrated"):
            detail = str(record.get("reason") or "").strip()
            suffix = f"；{detail}" if detail else ""
            result["reason"] = f"校准记录未达标（calibrated=False{suffix}），使用配置默认"
        else:
            result.update(
                {
                    "value": result["live_suggested"],
                    "source": "实盘校准",
                    "calibrated": True,
                    "reason": str(record.get("reason") or "已使用实盘校准"),
                }
            )
    except Exception:  # noqa: BLE001 - 回测滑点解析不允许因校准文件异常中断
        result = dict(fallback)
        result["reason"] = "校准读取或解析失败，使用配置默认"
    return result
