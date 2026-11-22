"""
P2 组合风险热度仿真对照。

功能：同一段行情、同一组策略腿，分别在「关闭组合风控」和不同
      `risk.portfolio.max_heat_pct` 下跑组合回测，比较成交、拒开、盈亏、回撤和热度峰值。

用法:
    python tools/sim_p2_heat_compare.py
    python tools/sim_p2_heat_compare.py --symbols bu2612,fu2701 --heat-pcts 2,4,6
    python tools/sim_p2_heat_compare.py --heat-pcts 2,4,6,8 --json data/p2_heat.json

默认品种取配置里的 strategy.legs；没有 legs 时取 strategy.symbols。
"""

from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.portfolio import run_portfolio_backtest  # noqa: E402
from src.utils.config_loader import load_config  # noqa: E402


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _symbols(cfg: dict[str, Any], raw: str) -> list[str]:
    if raw.strip():
        return [item.strip() for item in raw.split(",") if item.strip()]
    st = cfg.get("strategy")
    st = st if isinstance(st, dict) else {}
    legs = st.get("legs")
    if isinstance(legs, list) and legs:
        return [str(item.get("symbol") or "").strip() for item in legs if isinstance(item, dict)]
    symbols = st.get("symbols")
    if isinstance(symbols, list):
        return [str(item).strip() for item in symbols if str(item).strip()]
    return []


def _summary(result: dict[str, Any], capital: float) -> dict[str, Any]:
    perf = result.get("performance")
    shared = result.get("shared_capital")
    shared = shared if isinstance(shared, dict) else {}
    net = _num(getattr(perf, "net_pnl", 0.0))
    rejected = shared.get("rejected") or []
    reasons: dict[str, int] = {}
    for item in rejected:
        reason = str((item or {}).get("reason") or "未说明")
        reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "trades": int(_num(getattr(perf, "trades", 0))),
        "win_rate": _num(getattr(perf, "win_rate", 0.0)),
        "net_pnl": net,
        "return_pct": (net / capital) if capital > 0 else 0.0,
        "max_drawdown": _num(getattr(perf, "max_drawdown", 0.0)),
        "max_drawdown_pct": _num(getattr(perf, "max_drawdown_pct", 0.0)),
        "accepted": int(_num(shared.get("trades_accepted"))),
        "rejected": int(_num(shared.get("trades_rejected"))),
        "heat_budget": _num(shared.get("heat_budget")),
        "heat_peak": _num(shared.get("heat_peak")),
        "heat_peak_pct": _num(shared.get("heat_peak_pct")),
        "reject_reasons": reasons,
    }


def _print_table(rows: list[dict[str, Any]], capital: float) -> None:
    print()
    print("=" * 104)
    print(f"P2 组合风险热度对照（初始资金 {capital:,.0f} 元）")
    print("=" * 104)
    print(
        f"{'场景':<18}{'成交':>7}{'拒开':>7}{'净盈亏':>14}"
        f"{'收益率':>10}{'最大回撤':>13}{'热度峰值':>14}"
    )
    print("-" * 104)
    for row in rows:
        print(
            f"{row['scenario']:<18}{row['trades']:>7}{row['rejected']:>7}"
            f"{row['net_pnl']:>14,.0f}{row['return_pct']:>10.2%}"
            f"{row['max_drawdown']:>13,.0f}{row['heat_peak']:>14,.0f}"
        )
    print("-" * 104)
    for row in rows:
        if not row["reject_reasons"]:
            continue
        detail = "；".join(f"{k}×{v}" for k, v in sorted(row["reject_reasons"].items()))
        print(f"  {row['scenario']} 拒开原因：{detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P2 组合风险热度仿真对照")
    parser.add_argument("--symbols", default="", help="逗号分隔的合约；默认读配置")
    parser.add_argument(
        "--heat-pcts",
        default="1,2,3,5",
        help="要对照的热度上限（%%权益），逗号分隔；默认 1,2,3,5",
    )
    parser.add_argument("--capital", type=float, default=0.0, help="初始资金；0=读配置")
    parser.add_argument(
        "--json",
        default=str(ROOT / "data" / "sim_p2_heat_compare.json"),
        help="结果 JSON 路径；空字符串不写文件",
    )
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = load_config()
    cfg["_project_root"] = str(ROOT)
    symbols = _symbols(cfg, args.symbols)
    if not symbols:
        print("没有可回测的品种：请用 --symbols 指定，或在配置 strategy.legs / symbols 里填写。")
        return 2

    capital = args.capital if args.capital > 0 else _num(
        (cfg.get("risk") or {}).get("capital"), 100_000.0
    )
    heat_pcts = [_num(item) for item in args.heat_pcts.split(",") if item.strip()]
    scenarios: list[tuple[str, float | None]] = [("关闭组合风控", None)]
    scenarios.extend((f"热度上限 {pct:g}%", pct) for pct in heat_pcts if pct > 0)

    print(f"品种：{', '.join(symbols)}")
    rows: list[dict[str, Any]] = []
    for label, pct in scenarios:
        run_cfg = deepcopy(cfg)
        risk = run_cfg.setdefault("risk", {})
        portfolio = risk.setdefault("portfolio", {})
        if pct is None:
            portfolio["mode"] = "off"
        else:
            portfolio["mode"] = "enforce"
            portfolio["max_heat_pct"] = float(pct)
        print(f"正在回测：{label} …")
        try:
            result = run_portfolio_backtest(
                run_cfg, symbols, initial_capital=capital
            )
        except Exception as exc:  # noqa: BLE001 - 对照脚本要跑完所有场景
            print(f"  失败：{exc}")
            rows.append(
                {
                    "scenario": label,
                    "trades": 0,
                    "net_pnl": 0.0,
                    "return_pct": 0.0,
                    "max_drawdown": 0.0,
                    "rejected": 0,
                    "heat_peak": 0.0,
                    "reject_reasons": {"回测失败": 1},
                }
            )
            continue
        row = {"scenario": label, **_summary(result, capital)}
        rows.append(row)
        print(
            f"  成交 {row['trades']} 笔，拒开 {row['rejected']} 笔，"
            f"净盈亏 {row['net_pnl']:,.0f} 元，热度峰值 {row['heat_peak']:,.0f} 元"
        )

    _print_table(rows, capital)

    if rows:
        base = rows[0]
        print("\n相对「关闭组合风控」的差异：")
        for row in rows[1:]:
            print(
                f"  {row['scenario']}：净盈亏 {row['net_pnl'] - base['net_pnl']:+,.0f} 元，"
                f"成交 {row['trades'] - base['trades']:+d} 笔，"
                f"拒开 {row['rejected']} 笔"
            )

    if args.json:
        out = Path(args.json)
        if not out.is_absolute():
            out = ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "capital": capital,
                    "symbols": symbols,
                    "heat_pcts": heat_pcts,
                    "rows": rows,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n结果已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
