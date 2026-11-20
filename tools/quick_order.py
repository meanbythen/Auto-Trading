"""
快速下单脚本：直接驱动同花顺 GUI 下一笔单，不走策略。

这个脚本绕过策略、风控和账本，默认不发单。只有同时加上 --live 并设置
环境变量 QUICK_ORDER_LIVE=1 才会真的报单；两者缺一都只做参数检查。

用法：
    python tools/quick_order.py --symbol fu2701 --side buy_open --volume 1 --live
    QUICK_ORDER_LIVE=1 python tools/quick_order.py --symbol fu2701 --side close --volume 1 --live

side: buy_open / sell_open / close
"""

from __future__ import annotations

import argparse
import os
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
    parser.add_argument("--live", action="store_true",
                        help="真的发到柜台；还必须同时设置环境变量 QUICK_ORDER_LIVE=1")
    args = parser.parse_args()

    if not args.live or os.environ.get("QUICK_ORDER_LIVE") != "1":
        print("没有真的发单。这个脚本绕过策略、风控和账本，要真的下单必须同时满足：")
        print("  1) 加 --live")
        print("  2) 设置环境变量 QUICK_ORDER_LIVE=1")
        print(f"本次参数：{args.symbol} {args.side} {args.volume} 手 price={args.price or '对手价'}")
        return 2

    cfg = load_cfg()
    cfg["dry_run"] = False  # 模拟账户也算实盘通道

    print("!!!! 实盘下单：本脚本绕过策略、风控和账本，请再核对合约、方向、手数。")

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
