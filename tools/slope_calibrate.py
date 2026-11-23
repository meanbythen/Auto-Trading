"""斜率阈值标定报告。

用法：
    python tools/slope_calibrate.py
    python tools/slope_calibrate.py --symbols lh2701,lh2703,fu2701 --periods 15,5
    python tools/slope_calibrate.py --periods 15,D --split 0.75 --out data/斜率标定.xlsx

只读本地清洗库，不改配置、不下单。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.strategy.slope_calibration import calibrate_store, write_report  # noqa: E402


def _ints(text: str) -> list[int]:
    return [int(part) for part in text.replace("，", ",").split(",") if part.strip()]


def _strs(text: str) -> list[str]:
    out: list[str] = []
    for part in text.replace("，", ",").split(","):
        word = part.strip()
        if word:
            out.append(word.upper() if word.upper() == "D" else word)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="按品种×周期标定均线斜率阈值")
    parser.add_argument("--symbols", default="", help="逗号分隔的合约，留空=库里全部")
    parser.add_argument("--periods", default="15", help="逗号分隔的周期，如 15,5,D")
    parser.add_argument("--ema", type=int, default=120, help="方向均线，默认 120")
    parser.add_argument("--lookbacks", default="15,20,25,30", help="斜率回看根数")
    parser.add_argument("--horizon", type=int, default=20, help="未来趋势标签窗口")
    parser.add_argument("--split", type=float, default=0.7, help="前多少比例做标定")
    parser.add_argument("--out", default=str(ROOT / "data" / "斜率标定.xlsx"), help="输出 Excel")
    args = parser.parse_args()

    want_symbols = _strs(args.symbols) if args.symbols.strip() else None
    rows, summaries, meta = calibrate_store(
        ROOT,
        symbols=want_symbols,
        periods=_strs(args.periods),
        ema_period=args.ema,
        lookbacks=_ints(args.lookbacks),
        horizon=args.horizon,
        split=args.split,
    )
    if not summaries:
        print("没有可标定的数据：检查合约、周期和本地 K 线根数。")
        print(f"未纳入：{meta['未纳入']}")
        return 2
    out = write_report(Path(args.out), rows, summaries, meta)
    print(f"已写入 {out}")
    print(f"未纳入：{meta['未纳入']}")
    head = [
        "合约",
        "周期",
        "有效根数",
        "q80",
        "推荐回看",
        "建议阈值",
        "样本外覆盖",
        "样本外精确",
        "样本外召回",
        "样本外AUC",
        "t_AUC",
    ]
    print(" | ".join(head))
    for item in summaries:
        print(
            " | ".join(
                str(item.get(key, ""))
                for key in head
            )
        )
    usable = sum(1 for item in summaries if str(item.get("标定结论", "")).startswith(("可用", "偏弱")))
    print(f"结论：{usable}/{len(summaries)} 个品种×周期样本外有区分度；其余建议维持现阈值。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
