"""策略健康分数：只对照、不拦开仓。"""

import json

from src.ops.health import evaluate_health


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
