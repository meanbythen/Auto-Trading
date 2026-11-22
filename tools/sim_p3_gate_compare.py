"""
P3 精简闸门仿真对照。

功能：对三类闸门做 A/B 对照，不改实盘代码：
  1. ATR 减半：动态闸门关、全开、只留 ATR 减半三种口径；
  2. 固定手数：risk_pct 与 fixed（作为策略上限）两种算仓；
  3. 健康停开：按回测成交序列做逐笔影子重放，估算停开新仓的影响。

用法:
    python tools/sim_p3_gate_compare.py
    python tools/sim_p3_gate_compare.py --symbols bu2612,fu2701 --parts atr,fixed,health
    python tools/sim_p3_gate_compare.py --parts atr --json data/p3_atr.json

说明：健康停开依赖「实盘账本 vs 最近回测」，无法直接在同一段回测里开/关。
      这里用成交级影子重放做近似：按入场时点滚动统计已接受交易，达到偏差天数后
      跳过之后的开仓，并统计被跳过交易的盈亏。真实结论仍以仿真账号 A/B 为准。
"""

from __future__ import annotations

import argparse
import heapq
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.rollover import run_backtest_rollover  # noqa: E402
from src.ops.health import (  # noqa: E402
    LOOKBACK,
    MIN_TRADES,
    PF_RATIO,
    WATCH_DAYS_TO_BLOCK,
    WINRATE_DROP,
)
from src.utils.config_loader import load_config  # noqa: E402


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _skipped(result: Any) -> dict[str, int]:
    snapshot = _attr(result, "params_snapshot", {}) or {}
    raw = snapshot.get("skipped_opens") if isinstance(snapshot, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {str(key): int(_num(value)) for key, value in raw.items()}


def _aggregate(results: list[Any]) -> dict[str, Any]:
    trades = 0
    wins = 0
    net = 0.0
    drawdown = 0.0
    dd_pause = 0
    for result in results:
        perf = _attr(result, "performance")
        count = int(_num(_attr(perf, "trades", 0)))
        trades += count
        wins += int(round(_num(_attr(perf, "win_rate", 0.0)) * count))
        net += _num(_attr(perf, "net_pnl", 0.0))
        drawdown = max(drawdown, _num(_attr(perf, "max_drawdown", 0.0)))
        for reason, times in _skipped(result).items():
            if "回撤" in reason:
                dd_pause += times
    return {
        "trades": trades,
        "win_rate": (wins / trades) if trades else 0.0,
        "net_pnl": net,
        "max_drawdown": drawdown,
        "dd_pause_opens": dd_pause,
    }


def _legs(cfg: dict[str, Any], raw: str) -> list[dict[str, str]]:
    """按配置里的策略腿返回 symbol/engine/timeframe；--symbols 只给合约名。"""
    if raw.strip():
        return [
            {"symbol": item.strip(), "engine": "", "timeframe": "", "trail_timeframe": ""}
            for item in raw.split(",")
            if item.strip()
        ]
    st = cfg.get("strategy")
    st = st if isinstance(st, dict) else {}
    legs = st.get("legs")
    if isinstance(legs, list) and legs:
        out: list[dict[str, str]] = []
        for item in legs:
            if not isinstance(item, dict) or not str(item.get("symbol") or "").strip():
                continue
            out.append(
                {
                    "symbol": str(item.get("symbol")).strip(),
                    "engine": str(item.get("engine") or ""),
                    "timeframe": str(item.get("timeframe") or ""),
                    "trail_timeframe": str(item.get("trail_timeframe") or ""),
                }
            )
        return out
    base_engine = str(st.get("engine") or "")
    base_tf = str(st.get("timeframe") or "")
    symbols = st.get("symbols")
    if isinstance(symbols, list):
        return [
            {
                "symbol": str(item).strip(),
                "engine": base_engine,
                "timeframe": base_tf,
                "trail_timeframe": "",
            }
            for item in symbols
            if str(item).strip()
        ]
    return []


def _run_all(cfg: dict[str, Any], legs: list[dict[str, str]], capital: float) -> list[Any]:
    out: list[Any] = []
    for leg in legs:
        call: dict[str, Any] = {"symbol": leg["symbol"], "initial_capital": capital}
        if leg.get("engine"):
            call["engine"] = leg["engine"]
        if leg.get("timeframe"):
            call["timeframe"] = leg["timeframe"]
        if leg.get("trail_timeframe"):
            call["trail_timeframe"] = leg["trail_timeframe"]
        out.append(run_backtest_rollover(cfg, **call))
    return out


def _dynamic_cfg(
    cfg: dict[str, Any],
    *,
    enabled: bool,
    dd_off: bool = False,
    atr_off: bool = False,
) -> dict[str, Any]:
    run_cfg = deepcopy(cfg)
    dynamic = run_cfg.setdefault("risk", {}).setdefault("dynamic", {})
    dynamic["enabled"] = bool(enabled)
    if dd_off:
        dynamic["dd_half_pct"] = 100
        dynamic["dd_pause_pct"] = 100
    if atr_off:
        dynamic["atr_half_mult"] = 999
    return run_cfg


def _print_table(title: str, rows: list[dict[str, Any]]) -> None:
    print()
    print("=" * 96)
    print(title)
    print("=" * 96)
    print(
        f"{'场景':<20}{'成交':>7}{'胜率':>9}{'净盈亏':>14}"
        f"{'最大回撤':>13}{'回撤暂停':>10}"
    )
    print("-" * 96)
    for row in rows:
        print(
            f"{row['scenario']:<20}{row['trades']:>7}{row['win_rate']:>9.1%}"
            f"{row['net_pnl']:>14,.0f}{row['max_drawdown']:>13,.0f}"
            f"{row['dd_pause_opens']:>10}"
        )
    print("-" * 96)


def _wr_pf(pnls: list[float]) -> tuple[float, float | None]:
    if not pnls:
        return 0.0, None
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    win_rate = len(wins) / len(pnls)
    gross_loss = -sum(losses)
    profit_factor = (sum(wins) / gross_loss) if gross_loss > 0 else None
    return win_rate, profit_factor


def _health_replay(trades: list[Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """按入场时点做健康闸门影子重放（成交级近似，不重算策略信号）。"""
    runtime = cfg.get("runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    need = int(_num(runtime.get("health_block_days"), WATCH_DAYS_TO_BLOCK))
    if need <= 0:
        need = WATCH_DAYS_TO_BLOCK
    ordered = sorted(trades, key=lambda item: str(_attr(item, "entry_time", "") or ""))
    baseline_pnls = [_num(_attr(item, "pnl", 0.0)) for item in ordered]
    bt_wr, bt_pf = _wr_pf(baseline_pnls)

    live: list[float] = []
    pending: list[tuple[str, float]] = []
    accepted: list[Any] = []
    blocked: list[Any] = []
    watch_days = 0
    last_date = ""
    day_blocked = False
    blocked_dates: set[str] = set()

    for trade in ordered:
        entry = str(_attr(trade, "entry_time", "") or "")
        while pending and pending[0][0] <= entry:
            live.append(heapq.heappop(pending)[1])
        date = entry[:10]
        if date != last_date:
            last_date = date
            window = live[-LOOKBACK:]
            deviating = False
            if len(window) >= MIN_TRADES:
                live_wr, live_pf = _wr_pf(window)
                deviating = live_wr + 1e-9 < bt_wr - WINRATE_DROP
                if bt_pf is not None and bt_pf > 0 and live_pf is not None:
                    deviating = deviating or live_pf + 1e-9 < bt_pf * PF_RATIO
            watch_days = watch_days + 1 if deviating else 0
            day_blocked = watch_days >= need
        if day_blocked:
            blocked.append(trade)
            if date:
                blocked_dates.add(date)
            continue
        accepted.append(trade)
        heapq.heappush(
            pending,
            (str(_attr(trade, "exit_time", "") or ""), _num(_attr(trade, "pnl", 0.0))),
        )

    baseline_net = sum(baseline_pnls)
    accepted_net = sum(_num(_attr(item, "pnl", 0.0)) for item in accepted)
    blocked_net = sum(_num(_attr(item, "pnl", 0.0)) for item in blocked)
    return {
        "baseline_trades": len(ordered),
        "baseline_net_pnl": baseline_net,
        "baseline_win_rate": bt_wr,
        "baseline_profit_factor": bt_pf,
        "gated_trades": len(accepted),
        "gated_net_pnl": accepted_net,
        "blocked_trades": len(blocked),
        "blocked_dates": len(blocked_dates),
        "blocked_pnl": blocked_net,
        "net_delta": accepted_net - baseline_net,
        "lookback": LOOKBACK,
        "min_trades": MIN_TRADES,
        "block_days": need,
        "winrate_drop": WINRATE_DROP,
        "pf_ratio": PF_RATIO,
    }


def _print_health(info: dict[str, Any]) -> None:
    print()
    print("=" * 96)
    print("P3 健康停开影子重放（成交级近似）")
    print("=" * 96)
    pf = info["baseline_profit_factor"]
    pf_text = f"{pf:.2f}" if pf is not None else "—"
    print(
        f"基准：{info['baseline_trades']} 笔，净盈亏 {info['baseline_net_pnl']:,.0f} 元，"
        f"胜率 {info['baseline_win_rate']:.1%}，盈利因子 {pf_text}"
    )
    print(
        f"停开：{info['gated_trades']} 笔，净盈亏 {info['gated_net_pnl']:,.0f} 元；"
        f"被停开 {info['blocked_trades']} 笔（{info['blocked_dates']} 个交易日），"
        f"这些交易原本盈亏 {info['blocked_pnl']:+,.0f} 元"
    )
    print(f"净变化：{info['net_delta']:+,.0f} 元")
    print(
        f"参数：近 {info['lookback']} 笔、至少 {info['min_trades']} 笔才判；"
        f"连续偏离 {info['block_days']} 天停开；胜率低 {info['winrate_drop']:.0%} "
        f"或盈利因子不足 {info['pf_ratio']:.0%} 视为偏离"
    )
    print("注意：这是成交级影子重放，被停开的交易不会再产生后续信号；结论以仿真账号 A/B 为准。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P3 精简闸门仿真对照")
    parser.add_argument("--symbols", default="", help="逗号分隔的合约；默认读配置")
    parser.add_argument(
        "--parts",
        default="atr,fixed,health",
        help="要对照的部分，逗号分隔：atr,fixed,health",
    )
    parser.add_argument("--capital", type=float, default=0.0, help="初始资金；0=读配置")
    parser.add_argument(
        "--fixed-volume", type=int, default=0, help="fixed 场景的策略上限手数；0=读 trade.volume"
    )
    parser.add_argument(
        "--json",
        default=str(ROOT / "data" / "sim_p3_gate_compare.json"),
        help="结果 JSON 路径；空字符串不写文件",
    )
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = load_config()
    cfg["_project_root"] = str(ROOT)
    legs = _legs(cfg, args.symbols)
    if not legs:
        print("没有可回测的品种：请用 --symbols 指定，或在配置 strategy.legs / symbols 里填写。")
        return 2

    capital = args.capital if args.capital > 0 else _num(
        (cfg.get("risk") or {}).get("capital"), 100_000.0
    )
    parts = {item.strip().lower() for item in args.parts.split(",") if item.strip()}
    report: dict[str, Any] = {"capital": capital, "symbols": [item["symbol"] for item in legs]}
    baseline_results: list[Any] | None = None

    if "atr" in parts:
        scenarios = [
            ("动态闸门关", _dynamic_cfg(cfg, enabled=False)),
            ("动态闸门开", _dynamic_cfg(cfg, enabled=True)),
            ("仅ATR关", _dynamic_cfg(cfg, enabled=True, dd_off=True, atr_off=True)),
            ("仅ATR开", _dynamic_cfg(cfg, enabled=True, dd_off=True)),
        ]
        rows: list[dict[str, Any]] = []
        for label, run_cfg in scenarios:
            print(f"正在回测 ATR 场景：{label} …")
            results = _run_all(run_cfg, legs, capital)
            row = {"scenario": label, **_aggregate(results)}
            rows.append(row)
            if label == "动态闸门开":
                baseline_results = results
        _print_table(f"P3 ATR / 动态闸门对照（初始资金 {capital:,.0f} 元）", rows)
        report["atr"] = rows

    if "fixed" in parts:
        fixed_volume = args.fixed_volume
        if fixed_volume <= 0:
            fixed_volume = int(_num((cfg.get("trade") or {}).get("volume"), 1))
        risk_cfg = deepcopy(cfg)
        risk_cfg.setdefault("risk", {})["sizing_mode"] = "risk_pct"
        fixed_cfg = deepcopy(cfg)
        fixed_cfg.setdefault("risk", {})["sizing_mode"] = "fixed"
        fixed_cfg.setdefault("trade", {})["volume"] = max(1, fixed_volume)
        rows = []
        for label, run_cfg in (
            ("风险百分比", risk_cfg),
            (f"固定上限 {max(1, fixed_volume)} 手", fixed_cfg),
        ):
            print(f"正在回测算仓场景：{label} …")
            row = {"scenario": label, **_aggregate(_run_all(run_cfg, legs, capital))}
            rows.append(row)
        _print_table("P3 固定手数上限 vs 风险百分比", rows)
        report["fixed"] = rows

    if "health" in parts:
        if baseline_results is None:
            print("正在回测健康对照基准 …")
            baseline_results = _run_all(cfg, legs, capital)
        trades: list[Any] = []
        for result in baseline_results:
            raw = _attr(result, "trades", []) or []
            trades.extend(raw)
        if not trades:
            print("基准回测没有成交，跳过健康对照。")
        else:
            info = _health_replay(trades, cfg)
            _print_health(info)
            report["health"] = info

    if args.json:
        out = Path(args.json)
        if not out.is_absolute():
            out = ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
