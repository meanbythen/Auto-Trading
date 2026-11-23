"""斜率闸门 A/B 对照。

`ema_swing` 比较不同的 `slope_min_deg` 和斜率 t 值下限 `slope_min_t`；
其他策略比较可选的 `slope_gate` 开/关，以及不同 `min_deg` / `min_t` 组合。
按时间折判断候选是否够格替换默认值。只跑回测，不写配置、不下单。

用法:
    python tools/slope_gate_compare.py
    python tools/slope_gate_compare.py --min-t 0,2.1
    python tools/slope_gate_compare.py --thresholds 0,2.5,3 --min-t 0,2.1
    python tools/slope_gate_compare.py --engine trend_follow --thresholds 2.5,3,4,5 --min-t 0,2.1
    python tools/slope_gate_compare.py --symbols lh2701,fu2701 --out data/斜率闸门对照.json
    python tools/slope_gate_compare.py --thresholds 6 --adaptive 0,1 --adaptive-daily 1 --folds 3
"""

from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.backtest.rollover import run_backtest_rollover  # noqa: E402
from src.backtest.slope_gate import (  # noqa: E402
    ThresholdStat,
    Verdict,
    judge,
    neighbor_map,
    threshold_stat,
)
from src.risk.universe_table import product_key  # noqa: E402
from src.utils.config_loader import load_config  # noqa: E402

GATE_ENGINES = {"trend_follow", "supertrend"}


@dataclass(frozen=True)
class Run:
    """一次对照：deg=None 表示关闭闸门；min_t 是斜率 t 值下限。

    adaptive=True 时用自适应阈值替换固定角度；adaptive_daily 只在自适应时生效。
    """

    deg: float | None
    min_t: float = 0.0
    adaptive: bool = False
    adaptive_daily: bool = False

    @property
    def label(self) -> str:
        if self.deg is None:
            return "关"
        text = f"{self.deg:g}°"
        if self.min_t > 0:
            text = f"{text}+t{self.min_t:g}"
        if self.adaptive:
            text = f"{text}+自适应" + ("+日冻结" if self.adaptive_daily else "")
        return text


def _floats(text: str) -> list[float]:
    return [float(part) for part in text.replace("，", ",").split(",") if part.strip()]


def _strs(text: str) -> list[str]:
    return [part.strip() for part in text.replace("，", ",").split(",") if part.strip()]


def _bools(text: str) -> list[bool]:
    truthy = {"1", "true", "yes", "on", "开"}
    return [
        part.strip().lower() in truthy
        for part in text.replace("，", ",").split(",")
        if part.strip()
    ]


def _adaptive_combos(states: list[bool], daily: list[bool], *, force_fixed: bool) -> list[tuple[bool, bool]]:
    """自适应关时忽略日冻结；force_fixed 保证固定阈值基线始终在对照里。"""
    combos: set[tuple[bool, bool]] = set()
    for adaptive in states:
        if not adaptive:
            combos.add((False, False))
            continue
        for flag in daily:
            combos.add((True, flag))
    if force_fixed:
        combos.add((False, False))
    return sorted(combos)


def _sort_key(run: Run) -> tuple[float, bool, bool, float]:
    return (run.min_t, run.adaptive, run.adaptive_daily, run.deg if run.deg is not None else -1.0)


def _describe(run: Run) -> str:
    text = f"slope_min_deg={run.deg:g}，slope_min_t={run.min_t:g}"
    if run.adaptive:
        text += f"，自适应=开，日线冻结={'开' if run.adaptive_daily else '关'}"
    else:
        text += "，自适应=关"
    return text


def _one_product(
    cfg: dict[str, Any],
    symbol: str,
    engine: str,
    run: Run,
) -> dict[str, Any]:
    """只改引擎、斜率参数和品种，其余与实盘配置一致。deg=None 表示关闭闸门。"""
    local = deepcopy(cfg)
    local["_project_root"] = str(ROOT)
    strategy = local.setdefault("strategy", {})
    strategy["engine"] = engine
    strategy["symbols"] = [symbol]
    strategy["legs"] = []
    local.setdefault("trade", {})["symbol"] = symbol
    box = strategy.setdefault(engine, {})
    if engine == "ema_swing":
        if run.deg is not None:
            box["slope_min_deg"] = float(run.deg)
            box["slope_min_t"] = float(run.min_t)
        box["slope_adaptive"] = bool(run.adaptive)
        box["slope_adaptive_daily"] = bool(run.adaptive_daily) if run.adaptive else False
    else:
        gate = dict(box.get("slope_gate") or {})
        gate.update(
            {
                "enabled": run.deg is not None,
                "ema_period": 120,
                "lookback": 20,
                "method": "reg",
                "min_t": float(run.min_t),
                "price_side": False,
                "adaptive": bool(run.adaptive),
                "adaptive_daily": bool(run.adaptive_daily) if run.adaptive else False,
            }
        )
        if run.deg is not None:
            gate["min_deg"] = float(run.deg)
        box["slope_gate"] = gate
    return local


def _build_runs(args: argparse.Namespace, cfg: dict[str, Any]) -> tuple[list[Run], Run | None]:
    """返回要跑的对照组合，以及 ema_swing 的基线（其他引擎为 None，基线是关闭）。"""
    min_ts = sorted({round(float(value), 2) for value in _floats(args.min_t)}) if args.min_t.strip() else [0.0]
    adaptives = _bools(args.adaptive) if args.adaptive.strip() else [False]
    daily = _bools(args.adaptive_daily) if args.adaptive_daily.strip() else [True]
    if args.engine == "ema_swing":
        box = cfg["strategy"].get("ema_swing", {}) or {}
        base_deg = round(float(box.get("slope_min_deg", 3.0) or 3.0), 1)
        base_t = round(float(box.get("slope_min_t", 0.0) or 0.0), 2)
        # 基线固定用配置里的角度/t 值，不带自适应，便于和候选直接对照。
        baseline = Run(base_deg, base_t)
        min_ts = sorted(set(min_ts) | {base_t})
        degrees = sorted({round(float(value), 1) for value in _floats(args.thresholds or "2.5,3,4,5")} | {base_deg})
        combos = _adaptive_combos(adaptives, daily, force_fixed=True)
        runs = sorted(
            {Run(degree, t, adaptive, flag) for t in min_ts for degree in degrees for adaptive, flag in combos},
            key=_sort_key,
        )
        return runs, baseline
    degrees = sorted({round(float(value), 1) for value in _floats(args.thresholds or "2.5,3,4,5")})
    combos = _adaptive_combos(adaptives, daily, force_fixed=False)
    runs = [Run(None)]
    runs.extend(
        sorted(
            {Run(degree, t, adaptive, flag) for t in min_ts for degree in degrees for adaptive, flag in combos},
            key=_sort_key,
        )
    )
    return runs, None


def _judge_all(
    stats: dict[Run, ThresholdStat],
    baseline: Run | None,
    *,
    min_trades: int,
    calmar_ratio: float,
) -> tuple[Run | None, dict[Run, Verdict]]:
    """按 min_t 分组做邻域判定；ema_swing 的基线是当前配置，其他引擎是关闭。"""
    if baseline is not None:
        if baseline not in stats:
            return None, {}
        base_stat = stats[baseline]
        base_key: Run | None = baseline
    else:
        closed = stats.get(Run(None))
        if closed is None:
            return None, {}
        base_stat = closed
        base_key = Run(None)

    groups: dict[tuple[float, bool, bool], dict[float, ThresholdStat]] = {}
    for run, stat in stats.items():
        if run.deg is None:
            continue
        groups.setdefault((run.min_t, run.adaptive, run.adaptive_daily), {})[run.deg] = stat

    neighbors: dict[Run, dict[float, float]] = {}
    for (min_t, adaptive, adaptive_daily), group in groups.items():
        near = neighbor_map(group)
        for degree in group:
            neighbors[Run(degree, min_t, adaptive, adaptive_daily)] = near.get(degree, {})

    verdicts: dict[Run, Verdict] = {}
    for run, stat in stats.items():
        if run == base_key:
            verdicts[run] = Verdict(True, ("基线",))
        else:
            verdicts[run] = judge(
                stat,
                base_stat,
                neighbors.get(run, {}),
                min_trades=min_trades,
                calmar_ratio=calmar_ratio,
            )
    return base_key, verdicts


def main() -> int:
    parser = argparse.ArgumentParser(description="斜率闸门 A/B：真实回测对照不同阈值")
    parser.add_argument("--engine", default="ema_swing", help="ema_swing / trend_follow / supertrend")
    parser.add_argument("--symbols", default="", help="逗号分隔的合约，留空=配置里的 symbols")
    parser.add_argument("--thresholds", default="", help="逗号分隔的斜率角度阈值；留空用引擎默认")
    parser.add_argument(
        "--min-t",
        default="",
        help="逗号分隔的斜率 t 值下限，如 0,2.1；留空=只看角度。ema_swing 写 slope_min_t，其他引擎写 slope_gate.min_t",
    )
    parser.add_argument(
        "--adaptive",
        default="",
        help="逗号分隔的自适应开关，如 0,1；留空=只用固定阈值。ema_swing 写 slope_adaptive，其他引擎写 slope_gate.adaptive",
    )
    parser.add_argument(
        "--adaptive-daily",
        default="",
        help="自适应时是否按日线开盘冻结，如 1/0；留空=开。仅在 --adaptive 含 1 时生效",
    )
    parser.add_argument("--folds", type=int, default=3, help="时间折数，默认 3")
    parser.add_argument("--min-trades", type=int, default=30, help="验收最少成交笔数")
    parser.add_argument("--calmar-ratio", type=float, default=0.7, help="样本外 Calmar 至少是样本内的几倍")
    parser.add_argument("--out", default="", help="输出 JSON；留空按引擎命名")
    args = parser.parse_args()

    cfg = load_config()
    engine = args.engine.strip()
    if engine != "ema_swing" and engine not in GATE_ENGINES:
        print(f"不支持的引擎：{engine}（可用：ema_swing / trend_follow / supertrend）")
        return 2

    raw_symbols = _strs(args.symbols) if args.symbols.strip() else list(cfg["strategy"].get("symbols") or [])
    symbols: list[str] = []
    seen_products: set[str] = set()
    for symbol in raw_symbols:
        product = product_key(symbol)
        if product in seen_products:
            continue
        seen_products.add(product)
        symbols.append(symbol)
    if not symbols:
        print("没有可对照的品种：配置 strategy.symbols 为空。")
        return 2

    runs, baseline = _build_runs(args, cfg)

    print(f"引擎：{engine}  品种：{', '.join(symbols)}（按品种去重）")
    print(f"对照：{', '.join(run.label for run in runs)}  折数：{args.folds}")
    print("=" * 78)

    series_by_run: dict[Run, list[tuple[list[str], list[float]]]] = {run: [] for run in runs}
    trades_by_run: dict[Run, int] = {run: 0 for run in runs}
    failures: list[str] = []
    for run in runs:
        for symbol in symbols:
            local = _one_product(cfg, symbol, engine, run)
            try:
                result = run_backtest_rollover(
                    local,
                    symbol=symbol,
                    volume=1,
                    engine=engine,
                    source="slope_gate",
                )
            except Exception as error:  # noqa: BLE001
                failures.append(f"{symbol} {run.label}: {error}")
                continue
            if not result.equity or not result.equity_times:
                failures.append(f"{symbol} {run.label}: 权益曲线为空")
                continue
            series_by_run[run].append((list(result.equity_times), list(result.equity)))
            trades_by_run[run] += int(result.performance.trades)
            perf = result.performance
            print(
                f"{symbol:8s} {run.label:>12s}  "
                f"成交 {perf.trades:3d}  净盈亏 {perf.net_pnl:+10.0f}  回撤 {perf.max_drawdown:8.0f}"
            )
    if failures:
        print("\n未跑成功的组合：")
        for item in failures:
            print(f"  {item}")

    stats: dict[Run, ThresholdStat] = {}
    for run in runs:
        if not series_by_run[run]:
            continue
        stats[run] = threshold_stat(
            series_by_run[run],
            run.deg if run.deg is not None else 0.0,
            trades_by_run[run],
            folds=args.folds,
        )
    if not stats:
        print("没有可用的回测结果，无法对照。")
        return 2

    base_key, verdicts = _judge_all(
        stats,
        baseline,
        min_trades=args.min_trades,
        calmar_ratio=args.calmar_ratio,
    )
    if base_key is None:
        print("没有基线回测，无法判断候选是否值得切换。")
        return 2

    print("\n" + "=" * 78)
    print(
        f"{'口径':>20} {'成交':>4} {'全段净盈亏':>12} {'全段回撤':>10} "
        f"{'样本内净':>10} {'样本内Calmar':>12} {'样本外净':>10} {'样本外Calmar':>12} {'正收益折':>8}  判定"
    )
    ordered = sorted(stats, key=_sort_key)
    rows: list[tuple[Run, ThresholdStat, str]] = []
    for run in ordered:
        verdict = verdicts[run]
        if run == base_key:
            tag = "基线（关闭）" if run.deg is None else "基线"
        else:
            tag = "达标" if verdict.passes else "；".join(verdict.reasons)
        rows.append((run, stats[run], tag))
    for run, stat, tag in rows:
        print(
            f"{run.label:>20} {stat.trades:4d} {stat.net:+12.0f} {stat.max_drawdown:10.0f} "
            f"{stat.in_sample.net:+10.0f} {stat.in_sample.calmar:12.2f} "
            f"{stat.out_sample.net:+10.0f} {stat.out_sample.calmar:12.2f} "
            f"{stat.positive_folds:>4}/{len(stat.folds):<3}  {tag}"
        )
    print("-" * 78)

    passing = [run for run in stats if run != base_key and verdicts[run].passes]
    recommended = max(passing, key=lambda run: stats[run].out_sample.calmar) if passing else None
    if baseline is not None:
        if recommended is None:
            print(f"结论：没有候选组合同时满足验收线，维持基线 {baseline.label}。")
        else:
            print(
                f"结论：建议把默认从 {baseline.label} 调到 {recommended.label}"
                f"（{_describe(recommended)}）。"
            )
    elif recommended is None:
        print(f"结论：没有候选组合同时满足验收线，{engine} 的 slope_gate 保持关闭。")
    else:
        print(
            f"结论：建议 {engine} 启用 slope_gate，"
            f"min_deg={recommended.deg:g}°，min_t={recommended.min_t:g}，"
            f"自适应={'开' if recommended.adaptive else '关'}"
            f"{'，日线冻结=开' if recommended.adaptive and recommended.adaptive_daily else ''}。"
        )

    out = Path(args.out) if args.out.strip() else ROOT / "data" / f"斜率闸门对照_{engine}.json"
    payload = {
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "引擎": engine,
        "品种": symbols,
        "对照": [run.label for run in runs],
        "折数": args.folds,
        "基线": baseline.label if baseline is not None else "关闭",
        "建议": recommended.label if recommended is not None else ("关闭" if baseline is None else baseline.label),
        "验收线": {
            "最少成交": args.min_trades,
            "样本外Calmar至少为样本内倍数": args.calmar_ratio,
            "最少正收益折": 2,
        },
        "明细": [
            {
                "label": run.label,
                "min_deg": run.deg,
                "min_t": run.min_t,
                "adaptive": run.adaptive,
                "adaptive_daily": run.adaptive_daily,
                "trades": stat.trades,
                "net": round(stat.net, 2),
                "max_drawdown": round(stat.max_drawdown, 2),
                "calmar": round(stat.calmar, 4),
                "in_sample": asdict(stat.in_sample),
                "out_sample": asdict(stat.out_sample),
                "folds": [asdict(fold) for fold in stat.folds],
                "passes": tag == "达标" or tag in {"基线", "基线（关闭）"},
                "reasons": [] if tag in {"达标", "基线", "基线（关闭）"} else tag.split("；"),
            }
            for run, stat, tag in rows
        ],
        "未跑成功": failures,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"明细已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
