"""仿真报价的数字格式。不连接柜台。"""

from src.trader.ctp_md import CtpMdClient, describe_quote, market_front, quote_line, quote_text


def test_market_front_pairs_huatai_ports() -> None:
    """没单独写行情地址时，华泰交易端口配到行情端口。"""
    assert market_front("tcp://59.36.3.115:27226") == "tcp://59.36.3.115:27236"
    assert market_front("tcp://59.36.3.115:27226", "tcp://1.2.3.4:27236") == "tcp://1.2.3.4:27236"


def test_quote_text_hides_empty_prices() -> None:
    """柜台用一个特别大的数表示没有买价或卖价，窗口上显示横线。"""
    assert quote_text(4097) == "4097"
    assert quote_text(4100.5) == "4100.50"
    assert quote_text(0) == "—"
    assert quote_text(1.797e308) == "—"


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
