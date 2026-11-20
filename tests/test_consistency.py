"""今日一致性：回测镜像、1 分钟复核、实盘闸门合成一页。"""

import json
from datetime import datetime
from types import SimpleNamespace

import pandas as pd

from src.review.consistency import build_consistency_lines
from src.review.session import SessionWindow


def _window() -> list[SessionWindow]:
    return [SessionWindow("日盘", datetime(2026, 11, 20, 9, 0), datetime(2026, 11, 20, 15, 0))]


def _write_events(root) -> None:
    rows = [
        {"ts": "2026-11-20T10:05:00", "event": "stale_block_open", "symbol": "fu2701", "reason": "行情太旧"},
        {"ts": "2026-11-20T10:06:00", "event": "slip_block_open", "symbol": "fu2701", "reason": "对手价滑点过大"},
        {"ts": "2026-11-20T10:07:00", "event": "risk_block", "symbol": "fu2701", "reason": "停止开新仓：日亏"},
        # 时段外、别的品种都不该算进来
        {"ts": "2026-11-20T16:00:00", "event": "slip_block_open", "symbol": "fu2701"},
        {"ts": "2026-11-20T10:08:00", "event": "slip_block_open", "symbol": "lh2701"},
    ]
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data" / "events.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def _write_backtest(root, symbol: str = "fu2701") -> None:
    payload = {
        "symbol": symbol,
        "timeframe": "5",
        "params_snapshot": {
            "engine": "trend",
            "mirror_live_gates": True,
            "mirror_gate_hits": [
                {"kind": "stale_block", "label": "K线过期", "time": "2026-11-20T10:05:00",
                 "side": "多", "signal_price": 4000, "live_price": 4000, "chase": 120, "limit": 90},
                {"kind": "slip_block", "label": "开仓滑点过大", "time": "2026-11-20T11:05:00",
                 "side": "空", "signal_price": 4010, "live_price": 4000, "chase": 10, "limit": 5},
            ],
        },
    }
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data" / "backtest_last.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _minutes() -> pd.DataFrame:
    index = pd.date_range("2026-11-20 10:00", periods=40, freq="1min")
    frame = pd.DataFrame({"high": 4001.0, "low": 4001.0, "close": 4001.0}, index=index)
    frame.loc[pd.Timestamp("2026-11-20 10:10"), "low"] = 3989.0
    return frame


def _trade() -> SimpleNamespace:
    return SimpleNamespace(
        side="long",
        entry_time=datetime(2026, 11, 20, 10, 0),
        exit_time=datetime(2026, 11, 20, 10, 30),
        entry_price=4000.0,
        exit_price=4005.0,
        stop_price=3990.0,
        reason_entry="突破",
        reason_exit="目标",
        pnl_points=5.0,
    )


def _cfg() -> dict:
    return {"strategy": {"engine": "trend", "timeframe": "5"}}


def test_consistency_combines_mirror_minute_and_live_gates(tmp_path) -> None:
    _write_events(tmp_path)
    _write_backtest(tmp_path)
    text = "\n".join(
        build_consistency_lines(
            tmp_path,
            symbol="fu2701",
            windows=_window(),
            cfg=_cfg(),
            trades=[_trade()],
            minutes=_minutes(),
        )
    )
    assert "今日一致性" in text
    assert "回测镜像  开；放弃 2 次（追价1 过期1）" in text
    assert "1分钟复核  成交 1 笔，其中更早碰到止损 1 笔" in text
    assert "原出场 2026-11-20 10:30 → 1分钟 2026-11-20 10:10" in text
    assert "实盘闸门  共 3 次" in text
    assert "过期K 1 次" in text and "追价 1 次" in text and "风控拦截 1 次" in text
    assert "回测镜像放弃 2 次，实盘拦下 3 次" in text


def test_consistency_refuses_to_compare_other_scope(tmp_path) -> None:
    _write_backtest(tmp_path, symbol="lh2701")
    text = "\n".join(
        build_consistency_lines(tmp_path, symbol="fu2701", windows=_window(), cfg=_cfg())
    )
    assert "未对照" in text
    assert "不是同一口径" in text
    assert "放弃 2 次" not in text


def test_consistency_explains_missing_inputs(tmp_path) -> None:
    text = "\n".join(
        build_consistency_lines(tmp_path, symbol="fu2701", windows=_window(), cfg=_cfg())
    )
    assert "还没有最近一次回测" in text
    assert "当天重放没有可复核的已平仓成交" in text
    assert "还没有事件日志" in text
    assert "结论" in text
