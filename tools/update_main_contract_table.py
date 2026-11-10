"""
生成 / 更新 config/主力合约表.xlsx（命令行入口）

功能：读 config/品种与合约.xlsx 里启用的品种，按交易所日行情算出主力合约时段，写主力合约表。
原理：生成逻辑在 src/backtest/main_table.py，界面按钮和本命令用的是同一套代码；
      本文件只做参数解析。表格不会自动更新，只有界面点「更新主力合约表」或手动运行本命令才更新。
      更新时「手动修正」页原样保留，回测按手动段覆盖自动段。
用法：
    python tools/update_main_contract_table.py
    python tools/update_main_contract_table.py --start 2026-01-01 --end 2026-10-30
    python tools/update_main_contract_table.py --products fu,rb --out config/主力合约表.xlsx
品种增删：每次运行都会重新读 config/品种与合约.xlsx；表里删掉某一品种（或把「启用」改成否），
      重新跑一次，这个品种就不会再出现在新表里。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.backtest.main_table import DEFAULT_OUT, UNIVERSE_FILE, update_table  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 / 更新 config/主力合约表.xlsx")
    parser.add_argument("--start", help="开始日期 YYYY-MM-DD，默认今年 1 月 1 日")
    parser.add_argument("--end", help="结束日期 YYYY-MM-DD，默认昨天")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"输出文件，默认 {DEFAULT_OUT}")
    parser.add_argument("--products", help="只更新这些品种，逗号分隔，例如 fu,rb,TA")
    parser.add_argument("--universe", default=UNIVERSE_FILE, help=f"品种配置表，默认 {UNIVERSE_FILE}")
    args = parser.parse_args(argv)

    summary = update_table(
        _ROOT,
        start=args.start,
        end=args.end,
        products=args.products.split(",") if args.products else None,
        out=args.out,
        universe=args.universe,
    )
    print(
        f"已生成主力合约表：{summary['path']} | {summary['products']} 个品种、"
        f"{summary['periods']} 段 | 区间 {summary['start']}~{summary['end']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
