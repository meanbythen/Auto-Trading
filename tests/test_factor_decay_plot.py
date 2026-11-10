"""
因子衰减图：资料第二节的画图思路接到本程序字段。
只检查分摊和读数，不改开平仓。
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from src.export.factor_decay_plot import (
    factor_summary,
    load_factor_trades,
    plot_factor_decay,
    write_factor_decay_plot,
)


def _backtest_payload() -> dict:
    return {
        "engine": "multi_factor",
        "trades": [
            {
                "symbol": "fu2701",
                "entry_time": "2026-09-15 10:05:00",
                "exit_time": "2026-09-15 14:50:00",
                "pnl": 200.0,
                "score_ma": 1.0,
                "score_turtle": 1.0,
                "score_boll": 0.0,
                "score_sar": 0.0,
            },
            {
                "symbol": "fu2701",
                "entry_time": "2026-09-16 10:05:00",
                "exit_time": "2026-09-16 14:50:00",
                "pnl": -50.0,
                "score_ma": 0.0,
                "score_turtle": 0.0,
                "score_boll": 1.0,
                "score_sar": 0.0,
            },
        ],
    }


def test_load_skips_non_factor_trades(tmp_path: Path) -> None:
    """日内实体那种没有四因子分数的成交不进图。"""
    (tmp_path / "data").mkdir()
    payload = {
        "trades": [
            {"symbol": "bu2612", "pnl": 10.0, "exit_time": "2026-09-15 14:50:00"},
            *_backtest_payload()["trades"],
        ]
    }
    (tmp_path / "data" / "backtest_last.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    df = load_factor_trades(tmp_path)
    assert len(df) == 2
    assert abs(df.iloc[0]["pnl_ma"] - 100.0) < 1e-9
    assert abs(df.iloc[0]["pnl_turtle"] - 100.0) < 1e-9
    assert abs(df.iloc[1]["pnl_boll"] + 50.0) < 1e-9


def test_csv_don_score_alias(tmp_path: Path) -> None:
    """资料示例 CSV 用 don_score / datetime，也能画。"""
    csv_path = tmp_path / "factor_trade_log.csv"
    pd.DataFrame(
        [
            {
                "datetime": "2026-01-02 10:00:00",
                "pnl": 40.0,
                "ma_score": 1.0,
                "don_score": 1.0,
                "boll_score": 0.0,
                "sar_score": 0.0,
            }
        ]
    ).to_csv(csv_path, index=False)
    df = load_factor_trades(tmp_path, csv_path=csv_path, include_backtest=False, include_live=False)
    assert len(df) == 1
    assert abs(df.iloc[0]["pnl_ma"] - 20.0) < 1e-9
    assert abs(df.iloc[0]["pnl_turtle"] - 20.0) < 1e-9


def test_live_jsonl_and_summary(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "cta_factor_trades.jsonl").write_text(
        json.dumps(
            {
                "symbol": "fu2701",
                "exit_time": "2026-09-20 14:50:00",
                "pnl": 80.0,
                "score_ma": 0.0,
                "score_turtle": 0.0,
                "score_boll": 0.0,
                "score_sar": 1.0,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    df = load_factor_trades(tmp_path, include_backtest=False)
    table = factor_summary(df)
    sar = table[table["因子"] == "SAR转向"].iloc[0]
    assert abs(sar["总盈亏"] - 80.0) < 1e-9
    assert int(sar["交易次数"]) == 1


def test_write_png(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "backtest_last.json").write_text(
        json.dumps(_backtest_payload(), ensure_ascii=False),
        encoding="utf-8",
    )
    dest, df = write_factor_decay_plot(tmp_path, window=2)
    assert dest.exists()
    assert dest.suffix == ".png"
    assert (tmp_path / "data" / "因子衰减图.png").exists()
    assert len(df) == 2
    plot_factor_decay(df, tmp_path / "data" / "once.png", window=2)
    assert (tmp_path / "data" / "once.png").exists()
