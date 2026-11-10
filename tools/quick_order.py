"""
快速下单脚本：直接驱动同花顺 GUI 下一笔单，不走策略。

用法：
    python tools/quick_order.py --symbol fu2701 --side buy_open --volume 1
    python tools/quick_order.py --symbol fu2701 --side sell_open --volume 2
    python tools/quick_order.py --symbol fu2701 --side close --volume 1

side: buy_open / sell_open / close
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_cfg() -> dict:
    import yaml

    cfg_path = ROOT / "config" / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    # 密钥覆盖
    secrets = ROOT / "config" / "secrets.local.yaml"
    if secrets.exists():
        s = yaml.safe_load(secrets.read_text(encoding="utf-8")) or {}
        cfg.setdefault("account", {})
        if pwd := s.get("password"):
            cfg["account"]["resolved_password"] = str(pwd)
        if uid := s.get("user_id"):
            cfg["account"]["user_id"] = str(uid)
    else:
        cfg.setdefault("account", {})
    return cfg


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--side", required=True, choices=["buy_open", "sell_open", "close"])
    parser.add_argument("--volume", type=int, default=1)
    parser.add_argument("--price", type=float, default=None,
                        help="限价单价格；不填则用对手价")
    args = parser.parse_args()

    cfg = load_cfg()
    cfg["dry_run"] = False  # 强制实盘（模拟账户也算实盘通道）

    from src.trader.base import OrderRequest
    from src.trader.gui_trader import GuiTrader

    trader = GuiTrader(cfg)
    req = OrderRequest(
        symbol=args.symbol,
        side=args.side,
        volume=args.volume,
        price=args.price,
        gui_price="limit" if args.price is not None else "opponent",
        remark="quick_order.py",
    )

    print(f"→ 下单: {args.symbol} {args.side} {args.volume}手 price={args.price or '对手价'}")
    result = trader.place_order(req)

    print(f"→ 结果: ok={result.ok} | {result.message}")
    if result.order_id:
        print(f"  order_id={result.order_id}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
