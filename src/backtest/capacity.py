"""容量估算：按成交量判断一个品种最多能下多少手。

功能：取近 N 根同周期 K 线的成交量，按「单笔不超过同期均量的 X%」估算最大手数，
      再换算成名义金额和保证金占用。
原理：不接逐笔 tick，用分钟/日线成交量做保守近似；成交量按「手」计。
      结论用于给多品种实盘定单品种上限，不参与撮合，也不改任何回测数值。
"""

from __future__ import annotations

from typing import Any

DEFAULT_PARTICIPATION = 0.01
DEFAULT_LOOKBACK = 20
MIN_BARS = 20


def _number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _rows(data: Any) -> list[dict[str, Any]]:
    """同时接受 DataFrame 和 list[dict]，方便测试不依赖 pandas。"""
    if data is None:
        return []
    if hasattr(data, "to_dict"):
        try:
            return [dict(item) for item in data.to_dict("records")]
        except (TypeError, ValueError):
            return []
    return [dict(item) for item in data]


def estimate_capacity(
    bars: Any,
    *,
    participation: float = DEFAULT_PARTICIPATION,
    lookback: int = DEFAULT_LOOKBACK,
    price: float | None = None,
    multiplier: float = 10.0,
    margin_rate: float = 0.1,
) -> dict[str, Any]:
    """按成交量估算单品种容量。

    返回 max_lots / avg_volume / notional / margin / bars_used / note。
    样本不足或成交量为 0 时 max_lots=0，并在 note 里写明原因，不硬凑数字。
    """
    rows = _rows(bars)
    volumes = [value for value in (_number(item.get("volume")) for item in rows) if value > 0]
    used = volumes[-max(1, int(lookback)) :]
    avg_volume = sum(used) / len(used) if used else 0.0
    last_close = _number(rows[-1].get("close")) if rows else 0.0
    used_price = _number(price) if price else last_close
    rate = max(0.0, min(1.0, _number(participation)))
    note = ""
    if len(used) < MIN_BARS:
        note = f"成交量样本只有 {len(used)} 根，不足 {MIN_BARS} 根，结果仅供参考"
    if avg_volume <= 0:
        note = "成交量缺失，无法估算容量"
    max_lots = int(avg_volume * rate) if avg_volume > 0 and rate > 0 else 0
    notional_per_lot = used_price * max(0.0, _number(multiplier))
    margin_per_lot = notional_per_lot * max(0.0, _number(margin_rate))
    return {
        "max_lots": max_lots,
        "avg_volume": avg_volume,
        "bars_used": len(used),
        "participation": rate,
        "lookback": int(lookback),
        "price": used_price,
        "notional": notional_per_lot * max_lots,
        "margin": margin_per_lot * max_lots,
        "note": note,
    }


def format_capacity_lines(symbol: str, estimate: dict[str, Any]) -> list[str]:
    """把单品种容量结论写成一行中文。"""
    lots = int(_number(estimate.get("max_lots")))
    if lots <= 0:
        return [f"{symbol} 容量     算不出（{estimate.get('note') or '成交量或参数缺失'}）"]
    return [
        f"{symbol} 容量     建议最多 {lots} 手 · 同期均量 {_number(estimate.get('avg_volume')):.0f} 手 · "
        f"单笔参与率 {_number(estimate.get('participation')):.0%} · "
        f"名义 {_number(estimate.get('notional')):,.0f} 元 · 保证金约 {_number(estimate.get('margin')):,.0f} 元"
        + (f"（{estimate['note']}）" if estimate.get("note") else "")
    ]
