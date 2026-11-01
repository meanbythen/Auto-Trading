"""信号账户：按开平仓价格算盈亏，出入金和结算只改余额。"""

from src.account.signal_book import (
    add_cash,
    append_trade,
    balance_of,
    describe,
    new_book,
    open_position_rows,
    record_program_trade,
    set_initial_capital,
    settle_to,
    today_realized_pnl,
)


def _open(book, symbol, direction, price, volume=1, multiplier=10, bar_id=""):
    """记一笔开仓。乘数写在这笔上，测试不必读配置。"""
    assert append_trade(
        book,
        symbol=symbol,
        offset="开仓",
        direction=direction,
        price=price,
        volume=volume,
        multiplier=multiplier,
        bar_id=bar_id,
    )


def _close(book, symbol, direction, price, volume=1, bar_id=""):
    """记一笔平仓。盈亏用开仓时记下的乘数。"""
    assert append_trade(
        book,
        symbol=symbol,
        offset="平仓",
        direction=direction,
        price=price,
        volume=volume,
        multiplier=10,
        bar_id=bar_id,
    )


def test_long_and_short_pnl_ignore_commission():
    """多单、空单都只按价差乘乘数乘手数。不扣手续费。"""
    book = new_book(10000, version="1.8.0", started_at="2026-10-22 18:00:00")
    _open(book, "bu2612", "多", 3500)
    _close(book, "bu2612", "多", 3520)
    _open(book, "bu2612", "空", 3500)
    _close(book, "bu2612", "空", 3480)
    # (20 + 20) × 10 × 1 = 400
    assert balance_of(book) == 10400


def test_open_position_does_not_change_balance():
    """没平的仓只挂着，浮动盈亏不进余额。"""
    book = new_book(10000, version="1.8.0")
    _open(book, "bu2612", "多", 3500, volume=2)
    assert balance_of(book) == 10000
    _close(book, "bu2612", "多", 3510, volume=1)
    assert balance_of(book) == 10100
    text = describe(book)
    assert "1 手" in text
    assert "还没平的仓" in text


def test_deposit_withdraw_and_settlement():
    """入金加余额，出金减余额，结算把余额改成指定的数。"""
    book = new_book(10000, version="1.8.0")
    add_cash(book, "deposit", 1000)
    add_cash(book, "withdraw", 500)
    assert balance_of(book) == 10500
    delta = settle_to(book, 10000)
    assert delta == -500
    assert balance_of(book) == 10000
    assert settle_to(book, 10000) == 0


def test_begin_period_clears_history_and_keeps_the_start_time():
    """记下初始资金是新账期：旧成交和资金变动清掉，起点时间留下来。"""
    from src.account.signal_book import begin_period

    book = new_book(10000, version="1.8.0", started_at="2026-10-22 18:00:00")
    _open(book, "bu2612", "多", 3500)
    _close(book, "bu2612", "多", 3600)
    add_cash(book, "deposit", 1000)
    fresh = begin_period(8000, version="1.9.0", when="2026-10-22 21:08:00")
    assert fresh["trades"] == []
    assert fresh["cashflows"] == []
    assert fresh["started_at"] == "2026-10-22 21:08:00"
    assert fresh["period_started_at"] == "2026-10-22 21:08:00"
    assert [row["kind"] for row in fresh["changes"]] == ["initial"]
    assert balance_of(fresh) == 8000
    # 旧账对象还在，新账期是另一本。
    assert book["trades"]
    text = describe(fresh)
    assert "本账期从 2026-10-22 21:08:00 开始" in text
    assert "新账期开始" in text


def test_new_period_keeps_the_old_book_in_its_own_file(tmp_path):
    """新账期开始前，旧账整份存档；新账期写进另一个文件。"""
    import json

    from src.account.signal_book import book_path, load_book, save_book, start_new_period

    book = new_book(10000, version="1.9.0", started_at="2026-10-22 18:00:00")
    _open(book, "bu2612", "多", 3500)
    _close(book, "bu2612", "多", 3600)
    save_book(tmp_path, book)
    fresh, archived = start_new_period(tmp_path, 8000, version="1.9.1", when="2026-10-22 21:11:00")
    assert archived is not None and archived.exists()
    saved_old = json.loads(archived.read_text(encoding="utf-8"))
    assert saved_old["trades"]
    assert saved_old["closed_at"] == "2026-10-22 21:11:00"
    assert saved_old["initial_capital"] == 10000
    live = load_book(tmp_path)
    assert live is not None
    assert live["trades"] == []
    assert live["initial_capital"] == 8000
    assert live["previous_archive"] == archived.name
    assert book_path(tmp_path).name == "signal_account.json"
    period = tmp_path / "data" / "signal_periods" / str(fresh["period_file"])
    assert period.exists()
    assert period.name != archived.name
    assert json.loads(period.read_text(encoding="utf-8"))["initial_capital"] == 8000
    # 当前账继续记账时，新存档跟着更新，旧存档不再被改。
    _open(live, "bu2612", "多", 3700)
    save_book(tmp_path, live)
    assert json.loads(period.read_text(encoding="utf-8"))["trades"]
    assert json.loads(archived.read_text(encoding="utf-8"))["initial_capital"] == 10000


def test_change_initial_capital_keeps_pnl():
    """改初始资金会重算余额，已经赚到的盈亏还在。"""
    book = new_book(10000, version="1.8.0")
    _open(book, "c2611", "多", 2200)
    _close(book, "c2611", "多", 2210)
    set_initial_capital(book, 5000)
    assert balance_of(book) == 5100


def test_close_without_open_does_not_invent_pnl():
    """这一版之前开的仓，平仓时对不上，就不编造盈亏。"""
    book = new_book(10000, version="1.8.0")
    _close(book, "bu2612", "多", 3600)
    assert balance_of(book) == 10000
    assert "没有算进盈亏" in describe(book)


def test_each_balance_change_uses_one_of_five_methods():
    """每一笔让余额变化的钱，都标明五种方式之一。开仓和平局不记资金变动。"""
    book = new_book(10000, version="1.8.0", started_at="2026-10-22 18:00:00")
    _open(book, "bu2612", "多", 3500)
    _close(book, "bu2612", "多", 3500)
    _open(book, "bu2612", "多", 3500)
    _close(book, "bu2612", "多", 3520)
    add_cash(book, "deposit", 1000)
    add_cash(book, "withdraw", 500)
    settle_to(book, 10000)
    set_initial_capital(book, 9000)
    text = describe(book)
    kinds = [row["kind"] for row in book["changes"]]
    assert set(kinds) <= {"initial", "deposit", "withdraw", "settlement", "pnl"}
    assert "初始资金" in text
    assert "交易盈亏" in text
    assert "每日结算差额" in text
    assert "入金" in text
    assert "出金" in text
    # 开仓和平局没有资金变动。结算把余额调回 10000 后，初始资金再减 1000，余额是 9000。
    assert kinds.count("pnl") == 1
    assert kinds.count("initial") == 2
    assert balance_of(book) == 9000


def test_legacy_book_gets_change_rows_without_changing_balance():
    """1.8.0 已经记下的本金和盈亏，补上变动方式后余额不变。"""
    book = {
        "version_started": "1.8.0",
        "started_at": "2026-10-22 18:00:00",
        "initial_capital": 10000,
        "cashflows": [{"time": "2026-10-22 18:01:00", "kind": "deposit", "amount": 1000, "note": ""}],
        "trades": [
            {"time": "2026-10-22 18:02:00", "symbol": "bu2612", "offset": "开仓", "direction": "多", "price": 3500, "volume": 1, "multiplier": 10},
            {"time": "2026-10-22 18:03:00", "symbol": "bu2612", "offset": "平仓", "direction": "多", "price": 3510, "volume": 1, "multiplier": 10},
        ],
    }
    assert balance_of(book) == 11100
    assert [row["kind"] for row in book["changes"]] == ["initial", "deposit", "pnl"]


def test_same_bar_is_not_recorded_twice(tmp_path):
    """同一根 K 线的同一方向只记一次。"""
    first = record_program_trade(
        tmp_path,
        initial_capital=20387,
        symbol="bu2612",
        offset="开仓",
        direction="多",
        price=4600,
        volume=1,
        multiplier=10,
        bar_id="2026-10-22 21:05",
    )
    second = record_program_trade(
        tmp_path,
        initial_capital=20387,
        symbol="bu2612",
        offset="开仓",
        direction="多",
        price=4600,
        volume=1,
        multiplier=10,
        bar_id="2026-10-22 21:05",
    )
    assert first == 20387
    assert second is None


def test_actual_close_price_books_the_loss(tmp_path):
    """按实际成交价平仓后，开着的那一手对上，余额按价差增减。"""
    from src.account.signal_book import _pair

    opened = record_program_trade(
        tmp_path,
        initial_capital=20148,
        symbol="bu2612",
        offset="开仓",
        direction="多",
        price=4832,
        volume=1,
        multiplier=10,
        bar_id="open-1",
    )
    closed = record_program_trade(
        tmp_path,
        initial_capital=20148,
        symbol="bu2612",
        offset="平仓",
        direction="多",
        price=4800,
        volume=1,
        multiplier=10,
        reason="按实际成交价平账",
        bar_id="manual-1",
    )
    assert opened == 20148
    assert closed == 20148 - 320
    book = __import__("src.account.signal_book", fromlist=["load_book"]).load_book(tmp_path)
    _pnl, still, wins, unmatched = _pair(book)
    assert still == []
    assert unmatched == []
    assert wins == [-320.0]


def test_same_bar_different_fill_keys_are_both_recorded():
    """同一根 K 线上两笔不同的成交回报，各自都要记一笔。"""
    book = new_book(10000, version="1.8.0")
    first = append_trade(
        book,
        symbol="bu2612",
        offset="开仓",
        direction="多",
        price=3500,
        volume=1,
        multiplier=10,
        bar_id="2026-11-01 10:00",
        fill_key="1|buy_open|1",
    )
    second = append_trade(
        book,
        symbol="bu2612",
        offset="开仓",
        direction="多",
        price=3510,
        volume=1,
        multiplier=10,
        bar_id="2026-11-01 10:00",
        fill_key="1|buy_open|2",
    )
    assert first is True
    assert second is True
    assert len(book["trades"]) == 2


def test_duplicate_fill_key_is_recorded_once():
    """同一个 fill_key 的回报来两次，只记第一笔。"""
    book = new_book(10000, version="1.8.0")
    first = append_trade(
        book,
        symbol="bu2612",
        offset="开仓",
        direction="多",
        price=3500,
        volume=1,
        multiplier=10,
        bar_id="2026-11-01 10:00",
        fill_key="9|buy_open|1",
    )
    second = append_trade(
        book,
        symbol="bu2612",
        offset="开仓",
        direction="多",
        price=3500,
        volume=1,
        multiplier=10,
        bar_id="2026-11-01 10:00",
        fill_key="9|buy_open|1",
    )
    assert first is True
    assert second is False
    assert len(book["trades"]) == 1


def test_open_position_rows_merges_lots_and_keeps_earliest_stop():
    """同方向的多笔开仓合成一行，开仓均价和最早的止损都要留住。"""
    book = new_book(10000, version="1.8.0")
    append_trade(
        book,
        symbol="bu2612",
        offset="开仓",
        direction="多",
        price=3500,
        volume=2,
        multiplier=10,
        fill_key="1|buy_open|1",
        stop_price=3480,
        when="2026-11-01 09:10:00",
    )
    append_trade(
        book,
        symbol="bu2612",
        offset="开仓",
        direction="多",
        price=3530,
        volume=1,
        multiplier=10,
        fill_key="2|buy_open|1",
        stop_price=3460,
        when="2026-11-01 09:20:00",
    )
    rows = open_position_rows(book)
    assert len(rows) == 1
    row = rows[0]
    assert row["volume"] == 3
    assert row["entry_price"] == round((3500 * 2 + 3530) / 3, 4)
    assert row["opened_at"] == "2026-11-01 09:10:00"
    assert row["stop_price"] == 3480

    # 同一品种多空同时在账上时，只返回净仓，不拆成两行。
    mixed = new_book(10000, version="1.8.0")
    append_trade(
        mixed,
        symbol="bu2612",
        offset="开仓",
        direction="多",
        price=3500,
        volume=2,
        multiplier=10,
        fill_key="3|buy_open|2",
    )
    append_trade(
        mixed,
        symbol="bu2612",
        offset="开仓",
        direction="空",
        price=3520,
        volume=1,
        multiplier=10,
        fill_key="4|sell_open|1",
    )
    net = open_position_rows(mixed)
    assert len(net) == 1
    assert net[0]["side"] == "long"
    assert net[0]["volume"] == 1


def test_today_realized_pnl_only_counts_pnl_and_commission():
    """当日盈亏只算交易盈亏和手续费，入金和前一天的钱都不算。"""
    book = new_book(10000, version="1.8.0")
    add_cash(book, "deposit", 5000)
    append_trade(
        book,
        symbol="bu2612",
        offset="开仓",
        direction="多",
        price=3480,
        volume=1,
        multiplier=10,
        fill_key="prev-open",
        when="2026-10-30 10:00:00",
    )
    append_trade(
        book,
        symbol="bu2612",
        offset="平仓",
        direction="多",
        price=3500,
        volume=1,
        multiplier=10,
        fill_key="prev-close",
        when="2026-10-31 10:00:00",
    )
    append_trade(
        book,
        symbol="bu2612",
        offset="开仓",
        direction="多",
        price=3500,
        volume=2,
        multiplier=10,
        fill_key="today-open",
        when="2026-11-01 10:00:00",
    )
    append_trade(
        book,
        symbol="bu2612",
        offset="平仓",
        direction="多",
        price=3520,
        volume=2,
        multiplier=10,
        fill_key="today-close",
        commission=3,
        when="2026-11-01 10:00:00",
    )
    # (3520-3500) × 2 手 × 乘数 10 = 400，再减当天手续费 3 元。
    assert today_realized_pnl(book, "2026-11-01") == 397
    # 前一天自己那笔平仓的盈亏仍归前一天（(3500-3480) × 1 × 10 = 200）。
    assert today_realized_pnl(book, "2026-10-31") == 200
