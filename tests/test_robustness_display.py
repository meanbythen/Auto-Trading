"""批次1可信度底座：稳健性分区只展示、不改变评估结论（AC-8 / TR-4.1）。"""

from __future__ import annotations


def _demo_cfg() -> dict:
    return {
        "dry_run": True,
        "strategy": {
            "engine": "daily_body",
            "timeframe": "15",
            "trail_timeframe": "15",
            "symbols": ["bu2612"],
            "flatten_times": ["14:55", "22:55"],
            "daily_body": {"eth_minutes": 0, "regime_filter": False},
        },
        "trade": {"symbol": "bu2612", "volume": 1, "executor": "sim"},
        "risk": {"capital": 1_000_000, "contracts": {"bu": {"multiplier": 10}}},
        "data": {"source": "demo", "cache_dir": "data/klines", "min_bars": 60},
        "schedule": {"trading_sessions": [["09:00", "23:00"]]},
    }


def test_robustness_section_appears_without_changing_evaluation():
    from src.backtest.engine import run_backtest
    from src.backtest.metrics import format_risk_metrics_lines
    from src.backtest.robustness import analyze_experiment, format_robustness_lines
    from src.data.kline import KlineProvider

    cfg = _demo_cfg()
    provider = KlineProvider(cfg)
    bars = provider.fetch("bu2612", "15")
    result = run_backtest(cfg, symbol="bu2612", bars_signal=bars, bars_trail=bars, warmup=40)

    ev = result.evaluation
    conclusion_before = ev.conclusion
    check_keys_before = {c.key for c in ev.checks}

    # 复现 backtest_actions 渲染路径里对风险指标与稳健性分区的调用。
    lines = list(format_risk_metrics_lines(result.performance))
    summary = analyze_experiment(
        {
            "trades": [{"pnl": float(t.pnl)} for t in result.trades],
            "trials": [],
        }
    )
    lines.extend(format_robustness_lines(summary))
    text = "\n".join(lines)

    # 分区出现。
    assert "稳健性" in text
    # 只展示：评估结论与检查项集合完全不变。
    assert ev.conclusion == conclusion_before
    assert {c.key for c in ev.checks} == check_keys_before
