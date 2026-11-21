"""单品种容量估算。

用法：
    python tools/check_capacity.py
    python tools/check_capacity.py --participation 0.005 --lookback 20

按关注品种逐个取最近 K 线成交量，给出「单笔最多几手、占多少资金和保证金」。
不接逐笔 tick，结论是保守近似，用来给多品种实盘定单品种上限。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.backtest.capacity import (
    DEFAULT_LOOKBACK,
    DEFAULT_PARTICIPATION,
    estimate_capacity,
    format_capacity_lines,
)
from src.data.kline import KlineProvider
from src.risk.universe_table import apply_universe_table_to_cfg, product_key
from src.utils import load_config

FALLBACK_MARGIN_RATE = 0.1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按成交量估算单品种容量")
    parser.add_argument("--participation", type=float, default=DEFAULT_PARTICIPATION, help="单笔参与率，默认 1%%")
    parser.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK, help="取最近多少根 K 线，默认 20")
    args = parser.parse_args(argv)

    cfg = load_config()
    apply_universe_table_to_cfg(cfg)
    st = cfg.get("strategy") or {}
    contracts = (cfg.get("risk") or {}).get("contracts") or {}
    provider = KlineProvider(cfg)
    period = str(st.get("timeframe") or "15")
    symbols = [str(item).strip() for item in (st.get("symbols") or []) if str(item).strip()]
    if not symbols:
        print("关注列表为空，先在配置里加合约。")
        return 1

    failed = 0
    for symbol in symbols:
        box = contracts.get(product_key(symbol)) or {}
        rate = float(box.get("margin_rate_pct") or 0) / 100.0
        try:
            frame = provider.fetch(symbol, period=period, keep_history=True)
        except Exception as exc:  # noqa: BLE001
            print(f"{symbol} 容量     取 K 线失败：{exc}")
            failed += 1
            continue
        estimate = estimate_capacity(
            frame,
            participation=args.participation,
            lookback=args.lookback,
            multiplier=float(box.get("multiplier") or 10.0),
            margin_rate=rate if rate > 0 else FALLBACK_MARGIN_RATE,
        )
        print("\n".join(format_capacity_lines(symbol, estimate)))
    return 1 if failed == len(symbols) else 0


if __name__ == "__main__":
    raise SystemExit(main())
