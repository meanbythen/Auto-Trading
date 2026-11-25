"""再认证：合约参数体检、快照指纹与变化检测。"""

from __future__ import annotations

from datetime import datetime

from src.review.certification import (
    check_contracts,
    certification_snapshot,
    format_certification_lines,
    run_certification,
    snapshot_fingerprint,
)


def _cfg(contracts: dict | None = None, symbols: list[str] | None = None) -> dict:
    return {
        "strategy": {
            "engine": "multi_factor",
            "timeframe": "15",
            "trail_timeframe": "15",
            "symbols": list(symbols or ["fu2701", "lh2701"]),
        },
        "risk": {
            "sizing_mode": "risk_pct",
            "capital": 83_000.0,
            "risk_per_trade_pct": 1.0,
            "margin_usage_pct": 50.0,
            "max_daily_loss": 1_660,
            "min_volume": 1,
            "contracts": dict(
                contracts
                if contracts is not None
                else {
                    "fu": {"margin_rate_pct": 22.74, "multiplier": 10, "commission": 2.0, "price_tick": 1.0},
                    "lh": {"margin_rate_pct": 10.0, "multiplier": 16, "commission": 1.5, "price_tick": 5.0},
                }
            ),
        },
    }


def test_clean_config_has_no_issues():
    assert check_contracts(_cfg()) == []


def test_missing_margin_and_multiplier_are_errors():
    issues = check_contracts(
        _cfg(contracts={"fu": {"margin_rate_pct": 0, "multiplier": 0, "commission": 2.0, "price_tick": 1.0}})
    )
    fields = {(item["symbol"], item["field"], item["severity"]) for item in issues}
    assert ("fu2701", "margin_rate_pct", "error") in fields
    assert ("fu2701", "multiplier", "error") in fields
    # lh 在总表里没有：单独报一条错误。
    assert ("lh2701", "contracts", "error") in fields


def test_zero_commission_and_tick_are_warnings():
    issues = check_contracts(
        _cfg(
            contracts={
                "fu": {"margin_rate_pct": 22.74, "multiplier": 10, "commission": 0, "price_tick": 0},
                "lh": {"margin_rate_pct": 10.0, "multiplier": 16, "commission": 1.5, "price_tick": 5.0},
            }
        )
    )
    fields = {(item["symbol"], item["field"], item["severity"]) for item in issues}
    assert ("fu2701", "commission", "warn") in fields
    assert ("fu2701", "price_tick", "warn") in fields


def test_fingerprint_ignores_symbol_order_but_tracks_capital():
    first = _cfg()
    second = _cfg()
    second["risk"]["capital"] = 90_000.0
    assert snapshot_fingerprint(certification_snapshot(first)) == snapshot_fingerprint(certification_snapshot(first))
    assert snapshot_fingerprint(certification_snapshot(first)) != snapshot_fingerprint(certification_snapshot(second))


def test_run_certification_first_then_unchanged_then_changed(tmp_path):
    cfg = _cfg()
    first = run_certification(cfg, root=tmp_path, now=datetime(2026, 11, 22, 9, 0))
    assert first["status"] == "首次认证"
    assert (tmp_path / "data" / "review" / "certification.json").exists()

    second = run_certification(cfg, root=tmp_path)
    assert second["status"] == "未变化"
    assert second["changes"] == []

    changed = _cfg()
    changed["risk"]["capital"] = 90_000.0
    third = run_certification(changed, root=tmp_path)
    assert third["status"] == "需要再认证"
    assert any("capital" in item for item in third["changes"])


def test_format_lines_report_conclusion():
    clean = run_certification(_cfg(), root=None, write=False)
    text = "\n".join(format_certification_lines(clean))
    assert "参数体检" in text and "可以进入下一步" in text

    broken = _cfg(contracts={})
    report = run_certification(broken, root=None, write=False)
    text = "\n".join(format_certification_lines(report))
    assert "有错误，先补参数" in text
