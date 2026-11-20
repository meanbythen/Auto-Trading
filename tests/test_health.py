"""策略健康分数：默认只对照，配置打开后才可能停开新仓。"""

import json

from src.kernel.risk_chain import TradeIntent, run_early_gates
from src.ops.health import (
    HealthReport,
    entry_block_decision,
    evaluate_health,
    maybe_emit_health,
)


def _book(pnls: list[float]) -> dict:
    trades: list[dict] = []
    for i, pnl in enumerate(pnls):
        trades.append(
            {
                "offset": "开仓",
                "direction": "多",
                "symbol": "fu2701",
                "volume": 1,
                "price": 3000.0,
                "time": f"2026-10-01 09:0{i}:00" if i < 10 else f"2026-10-01 10:{i:02d}:00",
                "multiplier": 10,
            }
        )
        close_px = 3000.0 + pnl / 10.0
        trades.append(
            {
                "offset": "平仓",
                "direction": "多",
                "symbol": "fu2701",
                "volume": 1,
                "price": close_px,
                "time": f"2026-10-01 11:{i:02d}:00",
                "multiplier": 10,
            }
        )
    return {"trades": trades, "changes": []}


def test_skip_when_too_few_live_trades(tmp_path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "signal_account.json").write_text(
        json.dumps(_book([100, -50, 80])), encoding="utf-8"
    )
    report = evaluate_health(tmp_path, min_trades=10)
    assert report.status == "skip"
    assert report.live_n == 3


def test_watch_when_win_rate_drops(tmp_path) -> None:
    (tmp_path / "data").mkdir()
    # 10 笔里只赢 2 笔。
    pnls = [100, 100] + [-50] * 8
    (tmp_path / "data" / "signal_account.json").write_text(
        json.dumps(_book(pnls)), encoding="utf-8"
    )
    (tmp_path / "data" / "backtest_last.json").write_text(
        json.dumps(
            {
                "performance": {
                    "trades": 40,
                    "win_rate": 0.55,
                    "profit_factor": 1.8,
                }
            }
        ),
        encoding="utf-8",
    )
    report = evaluate_health(tmp_path, min_trades=10, winrate_drop=0.20)
    assert report.status == "watch"
    assert "胜率" in report.message
    assert "不停手" in report.message


def test_ok_when_close_to_backtest(tmp_path) -> None:
    (tmp_path / "data").mkdir()
    pnls = [100] * 6 + [-40] * 4
    (tmp_path / "data" / "signal_account.json").write_text(
        json.dumps(_book(pnls)), encoding="utf-8"
    )
    (tmp_path / "data" / "backtest_last.json").write_text(
        json.dumps(
            {
                "performance": {
                    "trades": 30,
                    "win_rate": 0.55,
                    "profit_factor": 1.4,
                }
            }
        ),
        encoding="utf-8",
    )
    report = evaluate_health(tmp_path, min_trades=10, winrate_drop=0.20, pf_ratio=0.5)
    assert report.status == "ok"


def _watch_book(root) -> None:
    (root / "data").mkdir(exist_ok=True)
    (root / "data" / "signal_account.json").write_text(
        json.dumps(_book([100, 100] + [-50] * 8)), encoding="utf-8"
    )
    (root / "data" / "backtest_last.json").write_text(
        json.dumps(
            {
                "symbol": "fu2701",
                "timeframe": "5",
                "params_snapshot": {"engine": "trend"},
                "performance": {"trades": 40, "win_rate": 0.55, "profit_factor": 1.8},
            }
        ),
        encoding="utf-8",
    )


def test_scope_mismatch_shows_not_compared(tmp_path) -> None:
    """回测跑的是别的策略或品种时，不能拿它当对照误报偏差。"""
    _watch_book(tmp_path)
    report = evaluate_health(
        tmp_path, min_trades=10, engine="multi_factor", symbol="lh2701", timeframe="15"
    )
    assert report.status == "skip"
    assert report.scope_mismatch is True
    assert "未对照" in report.message
    assert "multi_factor" in report.message


def test_old_backtest_without_scope_is_labeled_for_reference(tmp_path) -> None:
    """旧回测没记引擎和品种时仍可看数字，但必须说明只是参考。"""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "signal_account.json").write_text(
        json.dumps(_book([100, 100] + [-50] * 8)), encoding="utf-8"
    )
    (tmp_path / "data" / "backtest_last.json").write_text(
        json.dumps(
            {"performance": {"trades": 40, "win_rate": 0.55, "profit_factor": 1.8}}
        ),
        encoding="utf-8",
    )
    report = evaluate_health(tmp_path, min_trades=10, engine="multi_factor", symbol="fu2701")
    assert report.status == "watch"
    assert report.scope_recorded is False
    assert "未记录" in report.message
    assert "仅供参考" in report.message


def test_watch_days_count_once_per_day_and_reset_on_ok(tmp_path) -> None:
    _watch_book(tmp_path)
    first = maybe_emit_health(tmp_path, engine="trend", symbol="fu2701", timeframe="5")
    second = maybe_emit_health(tmp_path, engine="trend", symbol="fu2701", timeframe="5")
    assert first.status == "watch"
    assert first.watch_days == 1
    assert second.watch_days == 1
    assert "连续偏离第 1 个交易日" in second.message

    (tmp_path / "data" / "signal_account.json").write_text(
        json.dumps(_book([100] * 6 + [-40] * 4)), encoding="utf-8"
    )
    ok = maybe_emit_health(tmp_path, engine="trend", symbol="fu2701", timeframe="5")
    assert ok.status == "ok"
    assert ok.watch_days == 0


def test_entry_block_is_off_by_default(tmp_path) -> None:
    _watch_book(tmp_path)
    report = maybe_emit_health(tmp_path, engine="trend", symbol="fu2701", timeframe="5")
    assert entry_block_decision({}, report) == (False, "")
    assert entry_block_decision({"runtime": {"health_block_entry": False}}, report) == (False, "")


def test_entry_block_only_after_enough_watch_days() -> None:
    cfg = {"runtime": {"health_block_entry": True, "health_block_days": 3}}
    early = HealthReport(status="watch", message="偏差", watch_days=2)
    assert entry_block_decision(cfg, early) == (False, "")
    blocked, reason = entry_block_decision(cfg, HealthReport(status="watch", message="偏差", watch_days=3))
    assert blocked is True
    assert "停开新仓" in reason
    assert entry_block_decision(cfg, HealthReport(status="ok", message="正常", watch_days=9)) == (False, "")


def test_health_gate_blocks_only_new_opens() -> None:
    """停开闸只拦买开 / 卖开，平仓和止损必须放行。"""
    open_intent = TradeIntent(
        symbol="fu2701", side="buy_open", volume=1, health_blocked=True, health_block_reason="连续偏离"
    )
    hit = run_early_gates(open_intent, None)
    assert hit.ok is False
    assert hit.code == "strategy_health"

    close_intent = TradeIntent(symbol="fu2701", side="sell_open", volume=1, health_blocked=True)
    assert close_intent.side == "sell_open"
    assert run_early_gates(close_intent, None).code == "strategy_health"

    exit_intent = TradeIntent(symbol="fu2701", side="close", volume=1, health_blocked=True)
    assert run_early_gates(exit_intent, None).ok is True

    normal = TradeIntent(symbol="fu2701", side="buy_open", volume=1)
    assert run_early_gates(normal, None).ok is True
