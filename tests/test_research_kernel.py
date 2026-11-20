"""P4 研究内核：运行清单与各页结果口径一致性。"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing

from src.account.trading_db import connect
from src.backtest.engine import run_backtest
from src.data.kline import KlineProvider
from src.research.kernel import build_run_manifest, record_run, result_summary
from src.screen.picker import _fill_backtest_row


def _cfg() -> dict:
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


def test_run_manifest_has_required_fields() -> None:
    manifest = build_run_manifest(
        "backtest",
        _cfg(),
        symbols=["bu2612"],
        period="15",
        duration_ms=12.5,
        metrics={"trades": 1, "net_pnl": 10.0},
    )
    assert manifest["run_id"].startswith("RS-")
    assert manifest["source"] == "backtest"
    assert manifest["status"] == "ok"
    assert manifest["symbols"] == ["bu2612"]
    assert manifest["period"] == "15"
    assert manifest["trace_id"].startswith("OT-")
    assert re.fullmatch(r"[0-9a-f]{64}", manifest["params_sha256"]) is not None
    assert isinstance(manifest["strategy"], dict)
    assert isinstance(manifest["data_catalog"], dict)
    assert isinstance(manifest["commit"], dict)
    assert manifest["metrics"]["trades"] == 1


def test_run_backtest_attaches_run_manifest() -> None:
    cfg = _cfg()
    bars = KlineProvider(cfg).fetch("bu2612", "15")
    result = run_backtest(
        cfg,
        symbol="bu2612",
        bars_signal=bars,
        bars_trail=bars,
        warmup=40,
        source="backtest",
        run_id="RS-test-fixed",
    )
    assert result.run["run_id"] == "RS-test-fixed"
    assert result.run["source"] == "backtest"
    assert result.to_dict()["run"]["run_id"] == "RS-test-fixed"


def test_result_summary_matches_performance() -> None:
    cfg = _cfg()
    bars = KlineProvider(cfg).fetch("bu2612", "15")
    result = run_backtest(cfg, symbol="bu2612", bars_signal=bars, bars_trail=bars, warmup=40)
    summary = result_summary(result)
    assert summary["trades"] == result.performance.trades
    assert summary["net_pnl"] == result.performance.net_pnl


def test_picker_row_uses_same_summary() -> None:
    cfg = _cfg()
    bars = KlineProvider(cfg).fetch("bu2612", "15")
    result = run_backtest(cfg, symbol="bu2612", bars_signal=bars, bars_trail=bars, warmup=40)
    row: dict = {}
    _fill_backtest_row(result, row)
    summary = result_summary(result)
    assert row["trades"] == summary["trades"]
    assert row["net_pnl"] == summary["net_pnl"]
    assert row["run_id"] == result.run["run_id"]


def test_record_run_is_idempotent(tmp_path) -> None:
    manifest = build_run_manifest("backtest", _cfg(), symbols=["bu2612"], period="15")
    assert record_run(manifest, root=tmp_path) is True
    assert record_run(manifest, root=tmp_path) is True
    db_path = tmp_path / "data" / "trading.db"
    with closing(connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT run_id, source FROM research_runs WHERE run_id = ?",
            (manifest["run_id"],),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["source"] == "backtest"


def test_trading_db_schema_is_idempotent(tmp_path) -> None:
    db_path = tmp_path / "data" / "trading.db"
    with closing(connect(db_path)) as conn:
        first = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    with closing(connect(db_path)) as conn:
        second = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {"signals", "audit_log", "research_runs"} <= first
    assert first == second
    with closing(connect(db_path)) as conn:
        assert "trace_id" in {
            row["name"] for row in conn.execute("PRAGMA table_info(risk_decisions)")
        }


def test_sqlite_tables_are_queryable(tmp_path) -> None:
    db_path = tmp_path / "data" / "trading.db"
    with closing(connect(db_path)) as conn:
        conn.execute("SELECT COUNT(*) FROM signals").fetchone()
        conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()
        conn.execute("SELECT COUNT(*) FROM research_runs").fetchone()
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_connect_rejects_bad_path(tmp_path) -> None:
    """路径是目录时应报错，而不是悄悄写到一个奇怪的位置。"""
    import pytest

    with pytest.raises(sqlite3.Error):
        connect(tmp_path)
