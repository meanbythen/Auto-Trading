"""
策略优化入口
用法:
    python tools/run_optimize.py
    python tools/run_optimize.py --symbol bu2612 --apply
    python tools/run_optimize.py --mode 滚动前进 --holdout-pct 30 --walk-steps 3
加 --apply 会把通过检验的参数写进 config.yaml。检验段或其它品种大面积大亏时不会写入。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.optimizer import (
    apply_params_to_config,
    format_perf_block,
    optimize_strategy,
    save_optimization_report,
)
from src.utils.config_loader import load_config, save_config


def main() -> int:
    parser = argparse.ArgumentParser(description="策略优化")
    parser.add_argument("--symbol", default="", help="合约，默认读配置")
    parser.add_argument("--apply", action="store_true", help="把最优参数写入 config.yaml")
    parser.add_argument("--max-trials", type=int, default=80)
    parser.add_argument("--holdout-pct", type=float, default=30, help="检验比例%，默认 30")
    parser.add_argument("--mode", default="一次切断", help="一次切断 或 滚动前进")
    parser.add_argument("--walk-steps", type=int, default=3, help="滚动前进段数")
    args = parser.parse_args()

    cfg = load_config()
    cfg["_project_root"] = str(ROOT)
    symbol = args.symbol.strip() or None
    watch = list((cfg.get("strategy") or {}).get("symbols") or [])

    print("=" * 52)
    print("策略优化开始（网格搜索，可能需要几分钟）")
    print("=" * 52)
    result = optimize_strategy(
        cfg,
        symbol=symbol,
        max_trials=args.max_trials,
        holdout_pct=args.holdout_pct,
        mode=args.mode,
        walk_steps=args.walk_steps,
        cross_symbols=watch,
    )
    print("\n===== 挑出的参数 =====")
    print(f"品种: {result.symbol}")
    print(f"检验方式: {result.mode}，检验比例 {result.holdout_pct:.0f}%")
    print(f"训练段: {result.train_bars} 根，到 {result.train_end}")
    print(f"检验段: {result.holdout_bars} 根，从 {result.holdout_start} 起")
    print(f"训练得分: {result.best_score:.3f}")
    print(f"参数: {result.best_params}")
    print("\n".join(format_perf_block("训练成绩", result.train_performance or result.best_performance)))
    print("\n".join(format_perf_block("检验成绩", result.holdout_performance)))
    if result.cross_symbol_rows:
        print("\n===== 多品种验收 =====")
        for row in result.cross_symbol_rows:
            print(row)
    print(result.accept_reason)

    out = ROOT / "data" / "optimize_last.json"
    save_optimization_report(result, out)
    print(f"报告: {out}")

    if args.apply and result.accepted and result.best_params:
        new_cfg = apply_params_to_config(cfg, result.best_params, engine=result.engine)
        save_config(new_cfg)
        print("已把通过验收的参数写入 config.yaml，请重新加载后再启动。")
    elif args.apply:
        print("没有写入。检验没过，或没有挑出参数。配置没有改。")
    elif result.best_params and result.accepted:
        print("未写入配置。若确认要用，请加参数 --apply 再跑一次，或在界面点「把最优参数写入配置」。")
    elif result.best_params:
        print("未写入配置。这组没有通过检验，加 --apply 也不会写入。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
