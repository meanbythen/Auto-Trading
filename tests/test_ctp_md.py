"""仿真报价的数字格式。不连接柜台。"""

from src.trader.ctp_logic import ctp_depth_row, ctp_field_symbol
from src.trader.ctp_md import CtpMdClient, describe_quote, market_front, merge_quote, quote_line, quote_text


def test_market_front_pairs_huatai_ports() -> None:
    """没单独写行情地址时，华泰交易端口配到行情端口。"""
    assert market_front("tcp://59.36.3.115:27226") == "tcp://59.36.3.115:27236"
    assert market_front("tcp://59.36.3.115:27226", "tcp://1.2.3.4:27236") == "tcp://1.2.3.4:27236"


def test_quote_line_ignores_missing_text_fields() -> None:
    """有没有 last_text，写成的那一行都要一样，避免日志来回刷。"""
    raw = {"symbol": "fu2701", "last": 4035, "bid": 4034, "ask": 0, "upper": 4440, "lower": 4017}
    pretty = describe_quote(raw)
    assert quote_line(raw) == quote_line(pretty)
    assert "卖一 —" in quote_line(raw)


def test_quote_text_hides_empty_prices() -> None:
    """柜台用一个特别大的数表示没有买价或卖价，窗口上显示横线。"""
    assert quote_text(4097) == "4097"
    assert quote_text(4100.5) == "4100.50"
    assert quote_text(0) == "—"
    assert quote_text(1.797e308) == "—"


def test_quote_reads_old_instrument_field() -> None:
    """6.7 行情常把合约写在 reserve1，只读 InstrumentID 会丢跳动。"""

    class _Tick:
        InstrumentID = ""
        reserve1 = "fu2701"
        LastPrice = 4035
        BidPrice1 = 4035
        AskPrice1 = 1.797e308
        BidVolume1 = 2
        AskVolume1 = 0
        UpperLimitPrice = 4440
        LowerLimitPrice = 4017
        UpdateTime = "14:18:12"
        UpdateMillisec = 250

    assert ctp_field_symbol(_Tick()) == "fu2701"
    row = ctp_depth_row(_Tick())
    assert row is not None
    assert row["symbol"] == "fu2701"
    assert row["time"] == "14:18:12.250"
    assert quote_text(row["ask"]) == "—"
    client = CtpMdClient(_settings())
    client.wanted = "fu2701"
    assert client.apply_tick(row)
    assert client.latest is not None
    assert client.latest["bid_text"] == "4035"
    assert client.latest["ask_text"] == "—"


def test_snapshot_does_not_erase_live_ask() -> None:
    """柜台快照没有卖一时，不能把已经收到的卖一擦掉。"""
    live = describe_quote({"symbol": "fu2701", "last": 4036, "bid": 4035, "ask": 4037, "time": "14:20:01"})
    snapshot = {"symbol": "fu2701", "last": 4035, "bid": 4035, "ask": 1.797e308, "time": "14:18:12"}
    merged = merge_quote(live, snapshot, replace_empty=False)
    assert merged["ask_text"] == "4037"
    assert merged["last_text"] == "4035"
    wiped = merge_quote(live, snapshot, replace_empty=True)
    assert wiped["ask_text"] == "—"


def test_only_the_watched_contract_updates_the_quote() -> None:
    """期权代码更长，不能把期货的报价盖掉。"""
    client = CtpMdClient(_settings())
    client.wanted = "bu2612"
    assert client.apply_tick({"symbol": "bu2612C4200", "last": 10, "bid": 9, "ask": 11, "upper": 20, "lower": 1}) is False
    assert client.latest is None
    assert client.apply_tick(
        {"symbol": "bu2612", "last": 4090, "bid": 4089, "ask": 4091, "upper": 4097, "lower": 3782, "time": "14:59:00"}
    )
    assert client.latest is not None
    assert "最新 4090" in quote_line(client.latest)
    assert "涨停 4097" in quote_line(describe_quote(client.latest))


class _settings:
    td_front = "tcp://59.36.3.115:27226"
    md_front = ""
    flow_dir = "data/ctp_flow"
    broker_id = "8080"
    user_id = "12430"
    password = ""
    project_root = ""
    login_timeout_sec = 1
