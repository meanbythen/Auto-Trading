"""
历史回测入口
用法:
    python tools/run_backtest.py
    python tools/run_backtest.py --symbol bu2612
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.engine import run_backtest, save_backtest_report
from src.utils.config_loader import load_config


def main() -> int:
    parser = argparse.ArgumentParser(description="历史回测")
    parser.add_argument("--symbol", default="", help="合约，如 bu2612；默认读配置")
    args = parser.parse_args()

    cfg = load_config()
    cfg["_project_root"] = str(ROOT)
    symbol = args.symbol.strip() or None

    print("=" * 52)
    print("历史回测开始…（用与实盘相同的策略逻辑）")
    print("=" * 52)
    result = run_backtest(cfg, symbol=symbol)
    p = result.performance
    print(f"品种: {result.symbol}  信号周期: {result.timeframe}m  止损周期: {result.trail_timeframe}m")
    print(f"K线根数: {result.bars}")
    print(f"成交笔数: {p.trades}  胜率: {p.win_rate:.1%}  净盈亏: {p.net_pnl:.0f} 元")
    print(f"盈利因子: {p.profit_factor:.2f}  最大回撤: {p.max_drawdown:.0f} 元 ({p.max_drawdown_pct:.1%})")
    print(f"综合得分: {p.score:.3f}")
    out = ROOT / "data" / "backtest_last.json"
    save_backtest_report(result, out)
    print(f"详细报告: {out}")
    if result.trades:
        print("\n最近最多5笔:")
        for t in result.trades[-5:]:
            print(
                f"  {t.side} {t.entry_time}@{t.entry_price:.0f} -> "
                f"{t.exit_time}@{t.exit_price:.0f}  pnl={t.pnl:+.0f}  | {t.reason_exit[:40]}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
