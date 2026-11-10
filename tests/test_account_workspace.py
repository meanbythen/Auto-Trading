"""两本账互不覆盖：实盘文件还在，模拟账另写一份。"""

from src.account.signal_book import book_path, load_book, new_book, periods_dir, record_program_trade, save_book
from src.account.workspace import (
    CTP_LIVE,
    CTP_SIM,
    LIVE,
    PAPER,
    book_file,
    book_id_for_ctp_kind,
    is_ctp_book,
    normalize_id,
    intent_file,
    load_active_id,
    periods_folder,
    set_active_id,
    state_file,
)
from src.utils.state_store import default_state_path


def test_default_is_live_and_keeps_old_filenames(tmp_path):
    """没换过账时，仍用原来的三个文件名，旧账不用搬家。"""
    assert load_active_id(tmp_path) == LIVE
    assert book_path(tmp_path).name == "signal_account.json"
    assert periods_dir(tmp_path).name == "signal_periods"
    assert default_state_path(tmp_path).name == "bot_state.json"
    assert intent_file(tmp_path).name == "order_intent.json"


def test_paper_uses_other_files(tmp_path):
    """换成模拟账后，账本、旧账期、持仓、点单意图都换成另一套名字。"""
    set_active_id(tmp_path, PAPER)
    assert load_active_id(tmp_path) == PAPER
    assert book_path(tmp_path).name == "signal_account_paper.json"
    assert periods_dir(tmp_path).name == "signal_periods_paper"
    assert default_state_path(tmp_path).name == "bot_state_paper.json"
    assert intent_file(tmp_path).name == "order_intent_paper.json"


def test_paper_trades_do_not_touch_live_book(tmp_path):
    """模拟账记下的开仓，不会出现在实盘那份文件里。"""
    live = new_book(10000, version="18.1.0", started_at="2026-11-10 00:00:00")
    save_book(tmp_path, live)
    live_file = book_file(tmp_path, LIVE)
    assert live_file.exists()

    set_active_id(tmp_path, PAPER)
    record_program_trade(
        tmp_path,
        initial_capital=8000,
        symbol="ag2702",
        offset="开仓",
        direction="多",
        price=10000,
        volume=1,
        multiplier=15,
        fill_key="paper-1",
    )
    paper = load_book(tmp_path)
    assert paper is not None
    assert paper["initial_capital"] == 8000
    assert len(paper["trades"]) == 1
    assert paper["account_id"] == PAPER
    assert book_file(tmp_path, PAPER).exists()

    set_active_id(tmp_path, LIVE)
    restored = load_book(tmp_path)
    assert restored is not None
    assert restored["initial_capital"] == 10000
    assert restored["trades"] == []
    assert live_file.read_text(encoding="utf-8")
    # 换回实盘后，模拟账文件还在，只是当前不读它。
    assert book_file(tmp_path, PAPER).exists()


def test_periods_folder_splits_by_account(tmp_path):
    """模拟账的存档文件夹和实盘分开。"""
    assert periods_folder(tmp_path, LIVE).name == "signal_periods"
    assert periods_folder(tmp_path, PAPER).name == "signal_periods_paper"
    assert state_file(tmp_path, LIVE) != state_file(tmp_path, PAPER)
    assert book_file(tmp_path, CTP_SIM).name == "signal_account_ctp_sim.json"
    assert book_file(tmp_path, CTP_LIVE).name == "signal_account_ctp_live.json"


def test_huatai_sim_is_not_ths_paper() -> None:
    """华泰仿真不要被收成同花顺模拟账。"""
    assert normalize_id("华泰仿真 28077") == CTP_SIM
    assert normalize_id("同花顺模拟账") == PAPER
    assert is_ctp_book(CTP_SIM)
    assert is_ctp_book(CTP_LIVE)
    assert not is_ctp_book(PAPER)
    assert book_id_for_ctp_kind("sim") == CTP_SIM
    assert book_id_for_ctp_kind("live") == CTP_LIVE


def test_ctp_equity_writes_sim_book_only(tmp_path) -> None:
    """柜台仿真资金只进华泰仿真账，同花顺模拟账不动。"""
    from src.account.ctp_sync import push_gateway_account
    from src.account.signal_book import balance_of, load_named_book

    class _Settings:
        user_id = "28077"
        account_kind = "sim"

    class _Gateway:
        settings = _Settings()
        account = {"balance": 30139.28, "available": 28000.0, "pre_balance": 30000}
        available = 28000.0

    set_active_id(tmp_path, PAPER)
    text = push_gateway_account(tmp_path, _Gateway())
    assert "30139.28" in text
    sim = load_named_book(tmp_path, CTP_SIM)
    assert sim is not None
    assert abs(balance_of(sim) - 30139.28) < 0.01
    assert sim["ctp_snapshot"]["user_id"] == "28077"
    assert load_named_book(tmp_path, PAPER) is None
    assert load_named_book(tmp_path, LIVE) is None


def _ctp_cfg(kind: str, user_id: str) -> dict:
    return {
        "trade": {
            "executor": "ctp",
            "ctp": {"account_kind": kind, "user_id": user_id, kind: {"user_id": user_id}},
        },
        "account": {"user_id": user_id if kind == "live" else ""},
    }


def test_resident_guard_requires_matching_live_book(tmp_path) -> None:
    """真实通道必须先选华泰真实账，账号还要对上。"""
    from src.account.ctp_sync import resident_book_guard
    from src.account.signal_book import apply_ctp_equity, save_named_book

    cfg = _ctp_cfg("live", "20318052")
    set_active_id(tmp_path, PAPER)
    text = resident_book_guard(tmp_path, cfg)
    assert "华泰真实" in text
    assert "同花顺模拟" in text or "模拟" in text

    set_active_id(tmp_path, CTP_LIVE)
    assert resident_book_guard(tmp_path, cfg) == ""

    _, book = apply_ctp_equity(
        None,
        {"balance": 1000, "available": 800},
        user_id="11111111",
        version="0",
    )
    save_named_book(tmp_path, CTP_LIVE, book)
    mismatch = resident_book_guard(tmp_path, cfg)
    assert "11111111" in mismatch
    assert "20318052" in mismatch


def test_resident_guard_requires_sim_book_for_sim_ctp(tmp_path) -> None:
    """仿真通道必须先选华泰仿真账。"""
    from src.account.ctp_sync import resident_book_guard

    cfg = _ctp_cfg("sim", "28077")
    set_active_id(tmp_path, CTP_LIVE)
    text = resident_book_guard(tmp_path, cfg)
    assert "华泰仿真" in text
    set_active_id(tmp_path, CTP_SIM)
    assert resident_book_guard(tmp_path, cfg) == ""


def test_resident_guard_skips_non_ctp() -> None:
    from src.account.ctp_sync import resident_book_guard

    assert resident_book_guard(".", {"trade": {"executor": "gui"}}) == ""


def test_shown_equity_follows_ctp_snapshot(tmp_path) -> None:
    """华泰账的显示权益跟柜台快照，不被后来估的成交盈亏带跑。"""
    from src.account.signal_book import apply_ctp_equity, shown_equity, _append_change

    _, book = apply_ctp_equity(
        None,
        {"balance": 30139.28, "available": 28000, "pre_balance": 30000},
        user_id="28077",
        version="0",
    )
    _append_change(book, "pnl", 500.0, note="程序估的平仓盈亏")
    assert abs(shown_equity(book) - 30139.28) < 0.01


def test_ctp_account_keeps_broker_numbers_untouched() -> None:
    """CTP 账用回报里的账号和权益，第二次查询不再用差额去改本地流水。"""
    from src.account.signal_book import append_trade, apply_ctp_equity, shown_equity

    _, book = apply_ctp_equity(
        None,
        {"account_id": "28077", "balance": 10000, "available": 8000},
        user_id="ignore-me",
        version="0",
    )
    assert book["ctp_snapshot"]["account_id"] == "28077"
    assert book["ctp_user_id"] == "28077"
    first_n = len(book["changes"])
    _, book = apply_ctp_equity(
        book,
        {"account_id": "28077", "balance": 11000, "available": 9000},
        user_id="ignore-me",
        version="0",
    )
    assert len(book["changes"]) == first_n
    assert abs(shown_equity(book) - 11000) < 0.01
    assert len(book["ctp_history"]) == 2
    assert append_trade(
        book,
        symbol="bu2612",
        offset="开仓",
        direction="多",
        price=4000,
        volume=1,
        multiplier=10,
        commission=2.5,
    )
    assert len(book["changes"]) == first_n
    assert abs(shown_equity(book) - 11000) < 0.01
    assert book["trades"][-1]["symbol"] == "bu2612"


def test_apply_broker_runtime_syncs_lots_and_daily_pnl(tmp_path) -> None:
    """柜台资金要写进账本，并改算手数用的权益、可用资金、当日盈亏。"""
    from src.account.ctp_sync import apply_broker_runtime, broker_day_pnl
    from src.account.signal_book import load_named_book, shown_equity

    class _Settings:
        user_id = "28077"
        account_kind = "sim"

    class _Gateway:
        settings = _Settings()
        account = {
            "balance": 30139.28,
            "available": 28000.0,
            "pre_balance": 30000.0,
            "close_profit": 100.0,
            "position_profit": 39.28,
        }
        available = 28000.0

    class _Trader:
        gateway = _Gateway()
        last_broker_snapshot = None
        last_broker_equity = 30139.28

    class _Sizer:
        capital = 1.0
        cash_available = None

    class _Risk:
        daily_pnl = 0.0

        def set_daily_pnl(self, value):
            self.daily_pnl = round(float(value or 0), 2)

    set_active_id(tmp_path, CTP_SIM)
    sizer = _Sizer()
    risk = _Risk()
    cfg = {"risk": {"capital": 1.0}}
    apply_broker_runtime(tmp_path, cfg=cfg, sizer=sizer, risk=risk, trader=_Trader())
    book = load_named_book(tmp_path, CTP_SIM)
    assert book is not None
    assert book["ctp_snapshot"]["user_id"] == "28077"
    assert abs(shown_equity(book) - 30139.28) < 0.01
    assert abs(sizer.capital - 30139.28) < 0.01
    assert abs(sizer.cash_available - 28000) < 0.01
    assert abs(cfg["risk"]["capital"] - 30139.28) < 0.01
    assert abs(risk.daily_pnl - 139.28) < 0.01
    assert abs(broker_day_pnl(book["ctp_snapshot"]) - 139.28) < 0.01
