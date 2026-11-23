"""斜率标定：未来标签、AUC、向量化角度和报告输出。"""

import numpy as np
import pandas as pd
import pytest
from openpyxl import load_workbook

from src.data.bar_store import BarStore
from src.strategy.slope import measure_slope
from src.strategy.slope_calibration import (
    calibrate_frame,
    calibrate_store,
    future_trend_labels,
    roc_auc,
    slope_vectors,
    write_report,
)


def _frame(closes: np.ndarray) -> pd.DataFrame:
    idx = pd.date_range("2026-01-05 09:00", periods=len(closes), freq="15min")
    close = pd.Series(closes, index=idx)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1.0,
            "open_interest": 1.0,
        }
    )


def test_future_labels_up_down_chop() -> None:
    up = np.linspace(100.0, 200.0, 40)
    down = np.linspace(200.0, 100.0, 40)
    flat = np.full(40, 100.0)
    atr = np.full(40, 1.0)
    assert future_trend_labels(up, atr, horizon=5)[0] == 1.0
    assert future_trend_labels(down, atr, horizon=5)[0] == -1.0
    assert future_trend_labels(flat, atr, horizon=5)[0] == 0.0
    assert np.isnan(future_trend_labels(up, atr, horizon=5)[-1])


def test_roc_auc_perfect_and_single_class() -> None:
    scores = np.array([0.1, 0.2, 0.8, 0.9])
    labels = np.array([False, False, True, True])
    assert roc_auc(scores, labels) == pytest.approx(1.0)
    assert roc_auc(scores, np.zeros(4, dtype=bool)) != roc_auc(scores, np.zeros(4, dtype=bool))


def test_slope_vectors_match_single_bar() -> None:
    ema = 100.0 + np.cumsum(np.sin(np.arange(120) / 5.0) + 0.1)
    atr = np.full(len(ema), 1.3)
    angle, t_stat, r2 = slope_vectors(ema, atr, 20)
    for i in (25, 60, 110):
        got = measure_slope(pd.Series(ema[: i + 1]), 1.3, lookback=20)
        assert got is not None
        assert angle[i] == pytest.approx(got.angle_deg, abs=1e-10)
        assert t_stat[i] == pytest.approx(got.t_stat, rel=1e-9)
        assert r2[i] == pytest.approx(got.r2, rel=1e-9)


def test_calibrate_frame_reports_split_and_monotonic_coverage() -> None:
    x = np.arange(600, dtype=float)
    closes = 1000.0 + 0.05 * x + 6.0 * np.sin(x / 9.0)
    frame = _frame(closes)
    rows, summary = calibrate_frame(frame, "lh2701", "15", lookbacks=(20,), split=0.7)
    assert rows
    assert summary["合约"] == "lh2701"
    assert summary["有效根数"] > 0
    ordered = sorted(rows, key=lambda r: r.threshold)
    coverages = [r.calib_coverage for r in ordered]
    assert coverages == sorted(coverages, reverse=True)
    assert any(np.isfinite(r.oos_auc) for r in rows)
    assert 1.5 <= summary["q80"] <= 12.0


def test_write_report_creates_sheets(tmp_path) -> None:
    x = np.arange(400, dtype=float)
    frame = _frame(1000.0 + 0.05 * x + 6.0 * np.sin(x / 9.0))
    rows, summary = calibrate_frame(frame, "lh2701", "15", lookbacks=(20,))
    out = write_report(tmp_path / "斜率标定.xlsx", rows, [summary], {"生成时间": "test"})
    wb = load_workbook(out)
    assert wb.sheetnames == ["说明", "明细", "推荐"]
    headers = [cell.value for cell in wb["推荐"][1]]
    assert "建议阈值" in headers
    assert "样本外AUC" in headers


def test_calibrate_store_picks_tq_symbol(tmp_path) -> None:
    kline_dir = tmp_path / "data" / "klines"
    kline_dir.mkdir(parents=True)
    store = BarStore(kline_dir / "market.sqlite")
    x = np.arange(400, dtype=float)
    frame = _frame(1000.0 + 0.05 * x + 6.0 * np.sin(x / 9.0))
    store.upsert("lh2701@tq", "15", frame)
    rows, summaries, meta = calibrate_store(tmp_path, symbols=["lh2701"], periods=("15",))
    assert rows
    assert summaries[0]["合约"] == "lh2701"
    assert "lh2701@tq" not in meta["未纳入"]
