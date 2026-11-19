"""领域层账本读取与序列化的失败路径用例。"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from typing import Any

from src.domain.contracts import contract_multipliers
from src.domain.ledger import (
    balance_of,
    ctp_history_equity,
    equity_and_peak,
    read_book_file,
    shown_equity,
)
from src.domain.position import PositionState
from src.domain.trade import TradeRecord


def _write_book(tmp_path: Path, payload: Any) -> Path:
    path = tmp_path / "book.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_read_book_file_retries_permission_denied(tmp_path: Path, monkeypatch) -> None:
    """Windows 正在换文件时会暂时 Permission denied，多试几次应能读到。"""
    path = _write_book(tmp_path, {"initial_capital": 12.5})
    hits = {"n": 0}
    real = Path.read_text

    def flaky(self: Path, *args: object, **kwargs: object) -> str:
        if self.resolve() == path.resolve():
            hits["n"] += 1
            if hits["n"] < 3:
                raise PermissionError(13, "Permission denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky)
    monkeypatch.setattr("src.domain.ledger.time.sleep", lambda _s: None)
    book = read_book_file(path)
    assert book is not None
    assert book["initial_capital"] == 12.5
    assert hits["n"] == 3


def test_read_book_file_missing_broken_and_non_dict(tmp_path: Path) -> None:
    assert read_book_file(tmp_path / "nope.json") is None

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert read_book_file(broken) is None

    assert read_book_file(_write_book(tmp_path, [1, 2, 3])) is None


def test_read_book_file_cleans_bad_initial_capital(tmp_path: Path) -> None:
    book = read_book_file(_write_book(tmp_path, {"initial_capital": "oops"}))
    assert book is not None
    assert book["initial_capital"] == 0.0
    assert book["changes"][0]["kind"] == "initial"


def test_balance_of_legacy_book(tmp_path: Path) -> None:
    book = read_book_file(
        _write_book(
            tmp_path,
            {
                "started_at": "2026-01-01",
                "initial_capital": 1000,
                "cashflows": [
                    {"time": "2026-01-02", "kind": "deposit", "amount": 500},
                    {"time": "2026-01-03", "kind": "withdraw", "amount": 200},
                ],
                "trades": [
                    {
                        "time": "2026-01-04",
                        "symbol": "rb",
                        "direction": "多",
                        "offset": "开仓",
                        "price": 3000,
                        "volume": 1,
                        "multiplier": 10,
                    },
                    {
                        "time": "2026-01-05",
                        "symbol": "rb",
                        "direction": "多",
                        "offset": "平仓",
                        "price": 3010,
                        "volume": 1,
                    },
                ],
            },
        )
    )
    assert book is not None
    # 1000 + 500 - 200 + (3010-3000)*1*10
    assert balance_of(book) == 1400.0


def test_shown_equity_prefers_ctp_snapshot() -> None:
    assert shown_equity({"funding": "ctp", "ctp_snapshot": {"balance": 123.4}}) == 123.4
    assert shown_equity({"funding": "ctp", "ctp_snapshot": {"available": 55}}) == 55.0
    assert shown_equity({"changes": [{"amount": 10}]}) == 10.0
    assert shown_equity(None) == 0.0


def test_equity_and_peak() -> None:
    book = {"changes": [{"amount": 100}, {"amount": 50}, {"amount": -30}]}
    assert equity_and_peak(book) == (120.0, 150.0)


def test_ctp_history_equity_skip_duplicate_snapshot() -> None:
    book = {
        "ctp_history": [{"time": "t1", "balance": 100}],
        "ctp_snapshot": {"time": "t1", "balance": 100},
    }
    assert ctp_history_equity(book) == [("t1", 100.0)]

    book["ctp_snapshot"] = {"time": "t2", "balance": 120}
    assert ctp_history_equity(book) == [("t1", 100.0), ("t2", 120.0)]


def test_position_state_roundtrip_and_bad_input() -> None:
    pos = PositionState(
        side="long",
        volume=2,
        entry_price=10.5,
        initial_stop=9.0,
        trail_stop=9.5,
        trail_armed=True,
        box_high=11.0,
        box_low=8.0,
        box_height=3.0,
        symbol="rb",
        opened_at="2026-01-01 09:00",
    )
    raw = pos.to_dict()
    assert PositionState.from_dict("rb", raw) == pos

    restored = PositionState.from_dict("rb", {"side": "weird", "volume": "3", "foo": 1})
    assert restored.side == "flat"
    assert restored.volume == 3
    assert PositionState.from_dict("rb", {"volume": None}).volume == 0


def test_contract_multipliers_merge_and_empty() -> None:
    assert contract_multipliers({}) == {}
    cfg = {"risk": {"contracts": {"rb": {"multiplier": 5, "name": "螺纹"}}}}
    merged = contract_multipliers(cfg)
    assert merged["rb"] == 5.0
    assert merged["bu"] == 10.0


def test_trade_record_to_dict_matches_fields() -> None:
    trade = TradeRecord(
        symbol="rb",
        side="long",
        entry_time="2026-01-01",
        exit_time="2026-01-02",
        entry_price=3000,
        exit_price=3010,
        volume=1,
        pnl=100,
    )
    assert set(trade.to_dict()) == {item.name for item in fields(TradeRecord)}
