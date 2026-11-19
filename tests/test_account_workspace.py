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
    gw = _Gateway()
    text = push_gateway_account(tmp_path, gw)
    assert "30139.28" in text
    sim = load_named_book(tmp_path, CTP_SIM)
    assert sim is not None
    assert abs(balance_of(sim) - 30139.28) < 0.01
    assert sim["ctp_snapshot"]["user_id"] == "28077"
    assert load_named_book(tmp_path, PAPER) is None
    assert load_named_book(tmp_path, LIVE) is None
    # 数字没变时不再写盘、也不再报「已写入」，否则常驻每轮扫描都会刷。
    again = push_gateway_account(tmp_path, gw)
    assert again == ""
    history = list((load_named_book(tmp_path, CTP_SIM) or {}).get("ctp_history") or [])
    assert len(history) == 1
    gw.account = {"balance": 32699.02, "available": 32699.02, "pre_balance": 32743.76}
    gw.available = 32699.02
    gw.account = {"balance": 30219.28, "available": 28080.0, "pre_balance": 30000}
    gw.available = 28080.0
    small = push_gateway_account(tmp_path, gw)
    assert small == ""
    sim_small = load_named_book(tmp_path, CTP_SIM)
    assert sim_small is not None
    assert abs(float(sim_small["ctp_snapshot"]["balance"]) - 30219.28) < 0.01
    gw.account = {"balance": 32699.02, "available": 32699.02, "pre_balance": 32743.76}
    gw.available = 32699.02
    changed = push_gateway_account(tmp_path, gw)
    assert "32699.02" in changed
    sim2 = load_named_book(tmp_path, CTP_SIM)
    assert sim2 is not None
    assert abs(float(sim2["ctp_snapshot"]["balance"]) - 32699.02) < 0.01


def test_push_gateway_does_not_overwrite_unreadable_book(tmp_path, monkeypatch) -> None:
    """账本文件还在但这一轮读失败时，不要新建空账把成交盖掉。"""
    from src.account.ctp_sync import push_gateway_account
    from src.account.signal_book import load_named_book, save_named_book

    class _Settings:
        user_id = "28077"
        account_kind = "sim"

    class _Gateway:
        settings = _Settings()
        account = {"balance": 40000.0, "available": 40000.0}
        available = 40000.0

    keep = {
        "initial_capital": 10000.0,
        "trades": [{"symbol": "lh2701", "offset": "开", "volume": 1}],
        "changes": [{"kind": "initial", "amount": 10000.0}],
        "ctp_snapshot": {"balance": 30139.28, "user_id": "28077"},
    }
    save_named_book(tmp_path, CTP_SIM, keep)
    monkeypatch.setattr("src.account.ctp_sync.load_named_book", lambda *_a, **_k: None)
    text = push_gateway_account(tmp_path, _Gateway())
    assert "不改文件" in text
    book = load_named_book(tmp_path, CTP_SIM)
    assert book is not None
    assert book["trades"][0]["symbol"] == "lh2701"
    assert abs(float(book["ctp_snapshot"]["balance"]) - 30139.28) < 0.01


def _ctp_cfg(kind: str, user_id: str) -> dict:
    return {
        "trade": {
            "executor": "ctp",
            "ctp": {
                "account_kind": kind,
                "ctp_profile": "test",
                "ctp_profiles": {
                    "test": {
                        "kind": kind,
                        "user_id": user_id,
                        "td_front": "tcp://1.2.3.4:1",
                    }
                },
            },
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
    assert "CTP真实" in text
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
    assert "CTP仿真" in text
    set_active_id(tmp_path, CTP_SIM)
    assert resident_book_guard(tmp_path, cfg) == ""


def test_align_active_book_follows_ctp_kind(tmp_path) -> None:
    """切到仿真就看 CTP 仿真账，切到真实就看 CTP 真实账。常驻在跑时不换。"""
    from src.account.ctp_sync import align_active_book_to_ctp

    cfg = _ctp_cfg("sim", "28077")
    set_active_id(tmp_path, CTP_LIVE)
    note = align_active_book_to_ctp(tmp_path, cfg, running=False)
    assert "仿真" in note
    assert load_active_id(tmp_path) == CTP_SIM
    assert align_active_book_to_ctp(tmp_path, cfg, running=False) == ""
    set_active_id(tmp_path, CTP_LIVE)
    blocked = align_active_book_to_ctp(tmp_path, cfg, running=True)
    assert "常驻还在跑" in blocked
    assert load_active_id(tmp_path) == CTP_LIVE
    live_cfg = _ctp_cfg("live", "20318052")
    set_active_id(tmp_path, CTP_SIM)
    live_note = align_active_book_to_ctp(tmp_path, live_cfg, running=False)
    assert "真实" in live_note
    assert load_active_id(tmp_path) == CTP_LIVE


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

        def set_daily_pnl(self, value, *, log: bool = True):
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


def test_broker_day_pnl_does_not_treat_quota_as_withdraw() -> None:
    """可取资金等于权益时，当日盈亏不能变成整户出金。"""
    from src.account.ctp_sync import broker_day_pnl

    assert broker_day_pnl(
        {
            "pre_balance": 32743.76,
            "balance": 32699.0,
            "deposit": 0.0,
            "withdraw": 32743.76,
            "withdraw_amt": 0.0,
        }
    ) == -44.76
    assert broker_day_pnl(
        {
            "pre_balance": 32743.76,
            "balance": 32743.76,
            "deposit": 0.0,
            "withdraw": 32743.76,
            "withdraw_amt": 0.0,
        }
    ) == 0.0


def test_settled_zero_snapshot_does_not_wipe_ctp_daily() -> None:
    """收盘后柜台清零当日字段时，不能把白天已记的日历盈亏盖成 0。"""
    from src.account.signal_book import apply_ctp_equity

    _, book = apply_ctp_equity(
        None,
        {
            "account_id": "20318052",
            "balance": 33139.29,
            "available": 30000.0,
            "pre_balance": 32743.76,
            "close_profit": 520.0,
            "position_profit": 0.0,
            "commission": 124.47,
        },
        user_id="20318052",
        version="0",
    )
    # 人为把时间钉在日盘，避免测试跑在夜盘时交易日跳到明天。
    book["ctp_snapshot"]["time"] = "2026-11-19 14:55:22"
    book["ctp_history"][-1]["time"] = "2026-11-19 14:55:22"
    from src.account.signal_book import _store_ctp_day_pnl

    _store_ctp_day_pnl(book, book["ctp_snapshot"])
    assert abs(float(book["ctp_daily"]["2026-11-19"]) - 395.53) < 0.02

    _, book = apply_ctp_equity(
        book,
        {
            "account_id": "20318052",
            "balance": 33139.33,
            "available": 33139.33,
            "pre_balance": 33139.33,
            "close_profit": 0.0,
            "position_profit": 0.0,
            "commission": 0.0,
        },
        user_id="20318052",
        version="0",
    )
    book["ctp_snapshot"]["time"] = "2026-11-19 20:50:52"
    book["ctp_history"][-1]["time"] = "2026-11-19 20:50:52"
    _store_ctp_day_pnl(book, book["ctp_snapshot"])
    assert abs(float(book["ctp_daily"]["2026-11-19"]) - 395.53) < 0.02


def test_repair_ctp_daily_from_history_recovers_wiped_zero() -> None:
    """已经被盖成 0 的日历，打开账本时能从柜台历史找回白天盈亏。"""
    from src.account.signal_book import repair_ctp_daily
    from src.account.signal_stats import analyze_book
    from datetime import date

    book = {
        "funding": "ctp",
        "trades": [],
        "changes": [{"time": "2026-11-19 09:00:00", "kind": "initial", "amount": 32743.76}],
        "ctp_daily": {"2026-11-19": 0.0},
        "ctp_history": [
            {
                "time": "2026-11-19 14:55:22",
                "balance": 33139.29,
                "pre_balance": 32743.76,
                "close_profit": 520.0,
                "position_profit": 0.0,
                "commission": 124.47,
                "available": 30000.0,
            },
            {
                "time": "2026-11-19 20:50:52",
                "balance": 33139.33,
                "pre_balance": 33139.33,
                "close_profit": 0.0,
                "position_profit": 0.0,
                "commission": 0.0,
                "available": 33139.33,
            },
        ],
        "ctp_snapshot": {
            "time": "2026-11-19 20:50:52",
            "balance": 33139.33,
            "pre_balance": 33139.33,
            "close_profit": 0.0,
            "position_profit": 0.0,
            "commission": 0.0,
            "available": 33139.33,
        },
    }
    assert repair_ctp_daily(book) is True
    assert abs(float(book["ctp_daily"]["2026-11-19"]) - 395.53) < 0.02
    report = analyze_book(book, start=date(2026, 11, 19), end=date(2026, 11, 19))
    assert abs(report.daily_pnl[date(2026, 11, 19)] - 395.53) < 0.02
    assert abs(report.account.net_pnl - 395.53) < 0.02
