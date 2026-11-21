"""审查报告第五批（P3/P4 收尾）回归：结果口径、数据哈希、可观测接口、目录刷新、参数校验。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from src.data import catalog
from src.ops import journal
from src.research.kernel import build_run_manifest, record_run, result_summary
from src.strategy import factory, plugins

_PLUGIN = """
from src.strategy.factory import REGISTRY

SPEC = {
    "code": "my_break",
    "label": "我的突破",
    "factory": REGISTRY["daily_body"],
    "version": "9.9.9",
    "params": {"lookback": {"type": "int", "default": 20}},
}
"""


def _perf() -> SimpleNamespace:
    return SimpleNamespace(
        trades=5,
        net_pnl=100.0,
        win_rate=0.6,
        profit_factor=1.5,
        max_drawdown=30.0,
        wins=3,
        losses=2,
        avg_win=50.0,
        avg_loss=25.0,
        payoff=2.0,
        expectancy=10.0,
        max_drawdown_pct=0.03,
    )


# ---------------------------------------------------------------- R1


def test_result_summary_accepts_result_perf_and_dict() -> None:
    perf = _perf()
    from_result = result_summary(SimpleNamespace(performance=perf))
    from_perf = result_summary(perf)
    from_dict = result_summary(perf.__dict__)
    assert from_result == from_perf == from_dict
    assert from_result["trades"] == 5
    assert from_result["net_pnl"] == 100.0
    assert from_result["wins"] == 3
    assert from_result["max_drawdown_pct"] == 0.03


def test_result_summary_none_is_zeroed_and_complete() -> None:
    summary = result_summary(None)
    expected = {
        "trades",
        "net_pnl",
        "win_rate",
        "profit_factor",
        "max_drawdown",
        "wins",
        "losses",
        "avg_win",
        "avg_loss",
        "payoff",
        "expectancy",
        "max_drawdown_pct",
    }
    assert set(summary) == expected
    assert summary["trades"] == 0
    assert summary["net_pnl"] == 0.0
    assert summary["expectancy"] is None


def test_result_summary_keeps_none_expectancy() -> None:
    perf = _perf()
    perf.expectancy = None
    assert result_summary(perf)["expectancy"] is None


# ---------------------------------------------------------------- R2


def test_manifest_carries_data_sha256() -> None:
    manifest = build_run_manifest("backtest", {}, symbols=["bu2612"], data_sha256="abc123")
    assert manifest["data_sha256"] == "abc123"
    assert build_run_manifest("backtest", {})["data_sha256"] == ""


def test_record_run_persists_data_sha256(tmp_path: Path) -> None:
    manifest = build_run_manifest("backtest", {}, symbols=["bu2612"], data_sha256="deadbeef")
    assert record_run(manifest, root=tmp_path) is True
    with closing(sqlite3.connect(tmp_path / "data" / "trading.db")) as conn:
        row = conn.execute(
            "SELECT data_sha256 FROM research_runs WHERE run_id = ?",
            (manifest["run_id"],),
        ).fetchone()
    assert row is not None
    assert row[0] == "deadbeef"


def test_old_research_runs_table_gets_column(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "trading.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE research_runs (
                run_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL DEFAULT '',
                finished_at TEXT NOT NULL DEFAULT '',
                duration_ms REAL NOT NULL DEFAULT 0,
                strategy_id TEXT NOT NULL DEFAULT '',
                strategy_version TEXT NOT NULL DEFAULT '',
                params_sha256 TEXT NOT NULL DEFAULT '',
                period TEXT NOT NULL DEFAULT '',
                symbols_json TEXT NOT NULL DEFAULT '[]',
                commit_hash TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.commit()
    manifest = build_run_manifest("backtest", {}, symbols=["bu2612"], data_sha256="cafe")
    assert record_run(manifest, root=tmp_path) is True
    with closing(sqlite3.connect(db_path)) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(research_runs)")}
        assert "data_sha256" in cols
        value = conn.execute(
            "SELECT data_sha256 FROM research_runs WHERE run_id = ?",
            (manifest["run_id"],),
        ).fetchone()[0]
    assert value == "cafe"


# ---------------------------------------------------------------- R3


def test_metrics_snapshot_aggregates_files(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    timeline = [
        {"ts": "2026-11-21T09:00:00", "stage": "order_sent", "decision_to_send_ms": 12.0},
        {"ts": "2026-11-21T09:00:01", "stage": "accounted", "stage_ms": 30.0, "slip_ticks": -1},
        {"ts": "2026-11-21T09:00:02", "stage": "risk_intent", "allowed": False},
    ]
    (data_dir / "order_timeline.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in timeline) + "\n",
        encoding="utf-8",
    )
    alerts = [
        {"ts": "2026-11-21T09:05:00", "code": "freeze", "message": "冻结"},
        {"ts": "2026-11-21T09:06:00", "code": "reconcile_fail", "message": "对账失败"},
    ]
    (data_dir / "alerts.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in alerts) + "\n",
        encoding="utf-8",
    )
    snapshot = journal.metrics_snapshot("2026-11-21", root=tmp_path)
    assert snapshot["orders_sent"] == 1
    assert snapshot["fills"] == 1
    assert snapshot["rejected"] == 1
    assert snapshot["avg_send_ms"] == 12.0
    assert snapshot["avg_fill_ms"] == 30.0
    assert snapshot["freeze_alerts"] == 1
    assert snapshot["reconcile_alerts"] == 1


def test_trace_events_merges_signal_and_audit(tmp_path: Path) -> None:
    trace_id = "OT-test-trace"
    assert journal.record_signal(
        trace_id=trace_id,
        symbol="bu2612",
        bar_time="2026-11-21 09:15",
        action="buy_open",
        reason="突破",
        root=tmp_path,
    )
    assert journal.record_audit("order_sent", trace_id=trace_id, symbol="bu2612", root=tmp_path)
    rows = journal.trace_events(trace_id, root=tmp_path)
    events = {row["event"] for row in rows}
    assert "signal_generated" in events
    assert "order_sent" in events
    assert journal.trace_events("", root=tmp_path) == []


# ---------------------------------------------------------------- R4


def test_refresh_all_is_silent_when_sqlite_disabled(tmp_path: Path) -> None:
    result = catalog.refresh_all(tmp_path, {"storage": {"sqlite_enabled": False}})
    assert result == {"instruments": "", "klines": 0, "ticks": 0}
    assert not (tmp_path / "data" / "trading.db").exists()


# ---------------------------------------------------------------- R5


def test_validate_strategy_params_reports_bad_type(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "my_break.py").write_text(_PLUGIN, encoding="utf-8")
    monkeypatch.setattr(plugins, "PLUGIN_DIR", tmp_path)

    bad = {"strategy": {"engine": "my_break", "my_break": {"lookback": "abc"}}}
    messages = factory.validate_strategy_params(bad)
    assert messages
    assert any("lookback" in item for item in messages)

    good = {"strategy": {"engine": "my_break", "my_break": {"lookback": 20}}}
    assert factory.validate_strategy_params(good) == []


def test_validate_strategy_params_skips_builtin_and_rule() -> None:
    assert factory.validate_strategy_params({"strategy": {"engine": "daily_body"}}) == []
    assert factory.validate_strategy_params({"strategy": {"engine": "rule_dsl:my_rule"}}) == []
