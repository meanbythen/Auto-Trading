"""
CTP 报单、今昨仓、回报和对账。

不连接真实柜台，也不加载动态库。用一个假的客户端把回调按柜台的顺序送进来。
"""

from __future__ import annotations

import json
import time

from src.trader.base import OrderRequest
from src.trader.ctp_gateway import CtpGateway, CtpSettings
from src.trader.ctp_logic import (
    OFFSET_CLOSE,
    OFFSET_CLOSE_TODAY,
    OFFSET_CLOSE_YESTERDAY,
    PositionView,
    SessionTracker,
    TrackedOrder,
    absorb_position_row,
    apply_order_report,
    apply_trade,
    book_differences,
    plan_close,
    product_code,
)
from src.trader.ctp_trader import CtpTrader


def _settings() -> CtpSettings:
    return CtpSettings(
        td_front="tcp://127.0.0.1:1234",
        broker_id="0000",
        user_id="10001",
        password="secret",
        app_id="client_AutoTrading_1.0",
        auth_code="auth",
        flow_dir="data/ctp_flow",
        project_root="",
        order_wait_sec=0.3,
        query_interval_sec=30,
        login_timeout_sec=2,
    )


class FakeClient:
    """按脚本回应。测试可以改 on_insert，决定这笔是成交还是拒单。"""

    def __init__(self) -> None:
        self.listener = None
        self.inserts: list[dict] = []
        self.on_insert = None

    def start(self, _settings, listener) -> None:
        self.listener = listener
        listener.on_connected()

    def authenticate(self, _settings) -> None:
        self.listener.on_auth(True, "")

    def login(self, _settings) -> None:
        self.listener.on_login(True, 7, 8, "20261020", "")

    def confirm(self, _settings) -> None:
        self.listener.on_confirm(True, "")

    def query_orders(self, _settings) -> None:
        self.listener.on_query_order("", "", 0, "", True)

    def query_positions(self, _settings) -> None:
        self.listener.on_position_row(None, True)

    def query_account(self, _settings) -> None:
        self.listener.on_account(88000.5, True)

    def insert(self, _settings, order: dict) -> None:
        self.inserts.append(dict(order))
        if self.on_insert is not None:
            self.on_insert(self.listener, order)
            return
        self.listener.on_order_status(order["order_ref"], "3", 0, "已报")
        self.listener.on_trade(order["order_ref"], order["price"], order["volume"])

    def release(self) -> None:
        return None


def test_product_code_does_not_treat_copper_as_corn() -> None:
    """沪铜 cu 不能因为首字母是 c 就被当成玉米。"""
    assert product_code("bu2612") == "bu"
    assert product_code("c2511") == "c"
    assert product_code("CU2601") == "cu"


def test_asphalt_and_corn_split_yesterday_then_today() -> None:
    """先平昨，剩下的再平今。其他品种一笔普通平仓。"""
    asphalt = plan_close("bu2612", 3, today=1, yesterday=2)
    assert [(item.offset, item.volume) for item in asphalt] == [
        (OFFSET_CLOSE_YESTERDAY, 2),
        (OFFSET_CLOSE_TODAY, 1),
    ]
    corn = plan_close("c2511", 1, today=0, yesterday=1)
    assert [(item.offset, item.volume) for item in corn] == [(OFFSET_CLOSE_YESTERDAY, 1)]
    copper = plan_close("cu2601", 2, today=2, yesterday=0)
    assert [(item.offset, item.volume) for item in copper] == [(OFFSET_CLOSE, 2)]


def test_reports_cover_accept_reject_partial_fill_and_cancel() -> None:
    """五种回报都要能认出来。"""
    order = TrackedOrder("1", "bu2612", "0", "0", 2, 4500)
    assert apply_order_report(order, status_code="3", filled=0) == "submitted"
    assert apply_trade(order, 4501, 1) == "partial"
    assert apply_trade(order, 4502, 1) == "filled"
    assert order.avg_price == 4501.5

    rejected = TrackedOrder("2", "bu2612", "0", "0", 1, 4500)
    assert apply_order_report(rejected, status_code="rejected", message="资金不足") == "rejected"

    cancelled = TrackedOrder("3", "bu2612", "1", "3", 1, 4500)
    assert apply_order_report(cancelled, status_code="5", filled=0) == "cancelled"


def test_position_rows_for_shfe_and_dce() -> None:
    """沥青按上期所拆成两行；玉米按一行里的今仓和昨仓。"""
    asphalt = PositionView()
    absorb_position_row(
        asphalt,
        {"symbol": "bu2612", "exchange": "SHFE", "direction": "2", "position": 2, "today": 0, "yesterday": 2},
    )
    absorb_position_row(
        asphalt,
        {"symbol": "bu2612", "exchange": "SHFE", "direction": "2", "position": 1, "today": 1, "yesterday": 0},
    )
    assert asphalt.long_yesterday == 2
    assert asphalt.long_today == 1
    assert asphalt.net == 3

    corn = PositionView()
    absorb_position_row(
        corn,
        {"symbol": "c2511", "exchange": "DCE", "direction": "3", "position": 4, "today": 1, "yesterday": 3},
    )
    assert corn.short_today == 1
    assert corn.short_yesterday == 3
    assert corn.net == -4


def test_book_mismatch_lists_the_symbol() -> None:
    remote = {"bu2612": PositionView(long_today=1)}
    assert book_differences({"bu2612": 1}, remote) == []
    notes = book_differences({"bu2612": 2}, remote)
    assert "bu2612" in notes[0]
    assert "2" in notes[0] and "1" in notes[0]


def test_reconnect_checks_orders_before_ready() -> None:
    """断线后不能直接发单，要等重新登录并查完报单。"""
    session = SessionTracker()
    assert session.on_connected() == "authenticate"
    assert session.on_authenticated(True) == "login"
    assert session.on_logged_in(True, 1, 2, "20261020") == "confirm"
    assert session.on_settlement_confirmed(True) == "query_orders"
    assert session.need_order_check is True
    session.on_orders_checked()
    assert session.ready
    assert session.on_disconnected("网络断开") == "reconnect"
    assert session.ready is False
    assert session.need_order_check is True


def test_login_chain_then_buy_open_is_a_real_fill() -> None:
    """认证、登录、确认结算单之后，买开收到成交回报才算全部成交。"""
    client = FakeClient()
    gateway = CtpGateway(_settings(), client)
    trader = CtpTrader({"dry_run": False, "trade": {"ctp": {}}}, gateway)
    ok, message = trader.connect()
    assert ok, message
    assert "结算单" in message
    result = trader.place_order(OrderRequest("bu2612", "buy_open", 1, 4569))
    assert result.raw["filled"] is True
    assert result.raw["fill_price"] == 4569
    assert client.inserts[0]["direction"] == "0"
    assert client.inserts[0]["offset"] == "0"


def test_connect_timeout_releases_client_so_retry_can_restart() -> None:
    """柜台一直不回调时，超时后要释放接口，第二次连接才能重新 start。"""

    class SilentClient:
        def __init__(self) -> None:
            self.starts = 0
            self.releases = 0

        def start(self, _settings, _listener) -> None:
            self.starts += 1

        def release(self) -> None:
            self.releases += 1

    settings = _settings()
    settings.login_timeout_sec = 0.2
    settings.td_front = "not-a-front"
    first = SilentClient()
    gateway = CtpGateway(settings, first)
    ok, message = gateway.connect()
    assert ok is False
    assert "超时" in message
    assert first.releases == 1
    assert gateway.client is None
    assert gateway._started is False

    second = SilentClient()
    gateway.client = second
    ok, message = gateway.connect()
    assert ok is False
    assert second.starts == 1
    assert second.releases == 1


def test_asphalt_close_sends_yesterday_then_today() -> None:
    client = FakeClient()
    gateway = CtpGateway(_settings(), client)
    trader = CtpTrader({"dry_run": False}, gateway)
    trader.connect()
    gateway.positions["bu2612"] = PositionView(long_today=1, long_yesterday=1)
    gateway.positions_ready = True
    result = trader.place_order(
        OrderRequest("bu2612", "close", 2, 4570, position_side="long")
    )
    assert result.raw["filled"] is True
    assert [item["offset"] for item in client.inserts] == [
        OFFSET_CLOSE_YESTERDAY,
        OFFSET_CLOSE_TODAY,
    ]
    assert [item["direction"] for item in client.inserts] == ["1", "1"]


def test_corn_close_short_buys_yesterday() -> None:
    client = FakeClient()
    gateway = CtpGateway(_settings(), client)
    trader = CtpTrader({"dry_run": False}, gateway)
    trader.connect()
    gateway.positions["c2511"] = PositionView(short_yesterday=2)
    gateway.positions_ready = True
    trader.place_order(OrderRequest("c2511", "close", 2, 2300, position_side="short"))
    assert client.inserts[0]["offset"] == OFFSET_CLOSE_YESTERDAY
    assert client.inserts[0]["direction"] == "0"


def test_reject_is_not_a_fill() -> None:
    client = FakeClient()

    def reject(listener, order):
        listener.on_reject(order["order_ref"], "不在交易时间")

    client.on_insert = reject
    gateway = CtpGateway(_settings(), client)
    trader = CtpTrader({"dry_run": False}, gateway)
    trader.connect()
    result = trader.place_order(OrderRequest("bu2612", "sell_open", 1, 4500))
    assert result.raw["filled"] is False
    assert "拒单" in result.message


def test_mismatch_stops_new_opens_until_books_match() -> None:
    client = FakeClient()
    gateway = CtpGateway(_settings(), client)
    trader = CtpTrader({"dry_run": False}, gateway)
    trader.connect()
    gateway.positions = {"bu2612": PositionView(long_today=1)}
    gateway.positions_ready = True
    gateway.available = 1000
    trader._last_query = time.time()
    ok, note = trader.reconcile({"bu2612": 2})
    assert ok is False
    assert trader.blocks_new_opens()
    assert "停止开新仓" in note
    assert "1000.00" in note
    ok, _note = trader.reconcile({"bu2612": 1})
    assert ok is True
    assert trader.blocks_new_opens() is False


def test_disconnect_then_reconnect_queries_orders_before_new_order() -> None:
    client = FakeClient()
    gateway = CtpGateway(_settings(), client)
    trader = CtpTrader({"dry_run": False}, gateway)
    trader.connect()
    queried = {"count": 0}
    original = client.query_orders

    def counting(settings):
        queried["count"] += 1
        original(settings)

    client.query_orders = counting
    client.listener.on_disconnected("测试断开")
    assert gateway.session.ready is False
    # 柜台自动重连后会再次认证。假的客户端在 on_connected 里把登录和查询走完。
    client.listener.on_connected()
    assert gateway.session.ready is True
    assert queried["count"] == 1
    result = trader.place_order(OrderRequest("bu2612", "buy_open", 1, 4500))
    assert result.raw["filled"] is True


def test_working_order_blocks_another_open() -> None:
    """限价还挂着、没有全部成交时，不能当成已经成交，也不能再开一笔。"""
    client = FakeClient()

    def accept_only(listener, order):
        listener.on_order_status(order["order_ref"], "3", 0, "已报")

    client.on_insert = accept_only
    settings = _settings()
    settings.order_wait_sec = 0.05
    gateway = CtpGateway(settings, client)
    trader = CtpTrader({"dry_run": False}, gateway)
    trader.connect()
    result = trader.place_order(OrderRequest("bu2612", "buy_open", 1, 4569))
    assert result.raw["filled"] is False
    assert "已报" in result.message
    assert trader.has_working_order("bu2612") is True
    assert trader.blocks_new_opens() is True


def test_reconnect_query_blocks_when_broker_still_has_a_working_order() -> None:
    """查回报时，柜台还有一笔程序不认识的未完成报单，就先不开新仓。"""
    client = FakeClient()

    def query(_settings):
        client.listener.on_query_order("99", "3", 0, "bu2612", True)

    client.query_orders = query
    gateway = CtpGateway(_settings(), client)
    trader = CtpTrader({"dry_run": False}, gateway)
    trader.connect()
    assert trader.blocks_new_opens() is True
    assert "99" in gateway.unknown_working


def test_bad_orders_are_stopped_before_the_counter() -> None:
    """合约、价位、手数不对时，本机直接拒绝，测试用的假柜台也收不到。"""
    from src.trader.ctp_logic import check_order_instruction

    assert "合约代码" in check_order_instruction("ABC", 4600, 1, price_tick=1, max_volume=2)
    assert "最小变动价位" in check_order_instruction("bu2612", 0.5, 1, price_tick=1, max_volume=2)
    assert "单笔最大" in check_order_instruction("bu2612", 4600, 3, price_tick=1, max_volume=2)
    assert check_order_instruction("bu2612", 4600, 1, price_tick=1, max_volume=2) == ""

    client = FakeClient()
    gateway = CtpGateway(_settings(), client)
    trader = CtpTrader({"dry_run": False}, gateway)
    trader.connect()
    result = trader.place_order(OrderRequest("ABC", "buy_open", 1, 4600))
    assert result.ok is False
    assert client.inserts == []


def test_order_and_cancel_counts_warn_at_the_threshold() -> None:
    """报单、撤单按笔数计，到了阈值要写出警示。"""
    from src.trader.ctp_logic import OrderActivity

    activity = OrderActivity(order_warn=2, cancel_warn=2)
    assert "阈值" not in activity.note_order()
    assert "阈值" in activity.note_order()
    assert activity.orders == 2
    first_cancel = activity.note_cancel()
    assert activity.cancels == 1
    assert "阈值" not in first_cancel
    assert "阈值" in activity.note_cancel()
    assert activity.cancels == 2


def test_sim_front_refuses_the_live_account() -> None:
    """仿真前置不能拿实盘资金账号登录。"""
    from src.trader.ctp_logic import test_front_blocks_live_account

    text = test_front_blocks_live_account("tcp://59.36.3.115:27226", "20318052", "20318052")
    assert "仿真" in text
    assert test_front_blocks_live_account("tcp://59.36.3.115:27226", "10001437", "20318052") == ""


def test_cancel_uses_the_counter_id() -> None:
    """有柜台编号才能撤。撤单笔数单独加一。"""
    from src.trader.ctp_logic import TrackedOrder

    client = FakeClient()
    sent = []
    client.cancel = lambda _settings, order: sent.append(dict(order))
    gateway = CtpGateway(_settings(), client)
    trader = CtpTrader({"dry_run": False}, gateway)
    order = TrackedOrder("7", "bu2612", "0", "0", 1, 4500.0, status="submitted")
    gateway.table.add(order)
    refused = trader.cancel_latest("bu2612")
    assert refused.ok is False
    assert sent == []
    order.order_sys_id = "1001"
    ok = trader.cancel_latest("bu2612")
    assert ok.ok is True
    assert sent[0]["order_sys_id"] == "1001"
    assert gateway.activity.cancels == 1


def test_missing_front_does_not_crash() -> None:
    """明天技术还没给前置时，下单要说明缺什么，而不是抛异常。"""
    trader = CtpTrader({"dry_run": False, "account": {}, "trade": {"ctp": {}}})
    result = trader.place_order(OrderRequest("bu2612", "buy_open", 1, 4500))
    assert result.ok is False
    assert "交易前置" in result.message


def test_shfe_contract_is_sent_in_lowercase() -> None:
    """上期所沥青必须小写。写成 BU2612，柜台会回找不到合约。"""
    from src.trader.ctp_logic import ctp_exchange_id, ctp_instrument_id

    assert ctp_instrument_id("bu2612") == "bu2612"
    assert ctp_instrument_id("BU2612") == "bu2612"
    assert ctp_exchange_id("bu2612") == "SHFE"
    assert ctp_instrument_id("SA2701") == "SA2701"
    assert ctp_exchange_id("c2511") == "DCE"


def test_other_accounts_orders_are_not_our_errors() -> None:
    """公用仿真账号推来的别人的成交和拒单，不记成我们自己的报单。"""
    gateway = CtpGateway(_settings(), FakeClient())
    gateway.on_trade("别人的单", 2746.0, 1)
    gateway.on_reject("另一笔", "CTP:资金不足")
    gateway.on_cancel_error("29202", "不能撤销强平单")
    assert gateway.foreign_reports == 3
    assert gateway.table.get("别人的单") is None


def test_price_band_comes_from_the_instrument_query() -> None:
    """涨停价和跌停价用柜台合约资料，不拿同花顺的现价来猜。"""
    client = FakeClient()

    def query_depth(_settings, symbol: str) -> None:
        client.listener.on_instrument(
            {"symbol": symbol, "upper": 4500, "lower": 4100.5},
            True,
            "",
        )

    client.query_depth = query_depth
    gateway = CtpGateway(_settings(), client)
    client.listener = gateway
    gateway.session.on_connected()
    gateway.session.on_authenticated(True)
    gateway.session.on_logged_in(True, 1, 2, "20261022")
    gateway.session.on_settlement_confirmed(True)
    gateway.session.on_orders_checked()
    ok, text = gateway.query_price_band("bu2612")
    assert ok
    assert "涨停 4500" in text
    assert "跌停 4100.50" in text


# ---------------------------------------------------------------- 报单终态与部分成交


def test_part_traded_not_queueing_ends_the_order_with_a_partial_fill() -> None:
    """部分成交后剩余被撤：报单要结束，成交的那一手只记一次账。"""
    client = FakeClient()
    seen: list[tuple[int, str]] = []

    def partial_then_gone(listener, order):
        listener.on_order_status(order["order_ref"], "1", 1, "部分成交", order_sys_id="1001")
        listener.on_order_status(order["order_ref"], "2", 1, "部分成交剩余已撤")

    client.on_insert = partial_then_gone
    gateway = CtpGateway(_settings(), client)
    trader = CtpTrader({"dry_run": False}, gateway)
    trader.set_fill_handler(lambda report: seen.append((int(report["filled"]), report["status"])))
    trader.connect()
    result = trader.place_order(OrderRequest("bu2612", "buy_open", 2, 4569))
    assert result.ok is True
    assert result.raw["filled"] is False
    assert result.raw["filled_volume"] == 1
    assert result.raw["accounted"] is True
    assert "成交1手后撤单" in result.message
    assert seen == [(1, "cancelled")]
    # 报单已经结束，不能一直挡住后续开平。
    assert trader.has_working_order("bu2612") is False


def test_no_trade_not_queueing_is_terminal() -> None:
    """未成交且已经不在队列（状态 4）也要算结束，不能一直显示「已报」。"""
    from src.trader.ctp_logic import is_terminal

    order = TrackedOrder("1", "bu2612", "0", "0", 1, 4500, status="submitted")
    assert apply_order_report(order, status_code="4", filled=0) == "cancelled"
    order.status = "submitted"
    assert apply_order_report(order, status_code="5", filled=0) == "cancelled"
    assert is_terminal("cancelled")


def test_cancelled_partial_order_is_accounted_once_by_the_gateway() -> None:
    """撤单回报如果带着成交手数，网关也要补记账，避免内部持仓永远少一截。"""
    seen: list[int] = []
    gateway = CtpGateway(_settings(), FakeClient())
    gateway.fill_handler = lambda report: seen.append(int(report["filled"]))
    order = TrackedOrder("1", "bu2612", "0", "0", 2, 4500, status="submitted")
    gateway.table.add(order)
    gateway.on_order_status("1", "5", 1, "已撤，部分成交")
    assert order.status == "cancelled"
    assert seen == [1]
    # 再来一条迟到的成交回报，不能重复记账。
    gateway.on_trade("1", 4501, 1)
    assert seen == [1]


# ---------------------------------------------------------------- 超时自动撤单


def _auto_cancel_trader(client: FakeClient, *, cancel_wait_sec: float = 0.3, on: bool = True):
    settings = _settings()
    settings.order_wait_sec = 0.05
    settings.cancel_wait_sec = cancel_wait_sec
    settings.auto_cancel_on_timeout = on
    gateway = CtpGateway(settings, client)
    trader = CtpTrader({"dry_run": False}, gateway)
    trader.connect()
    return gateway, trader


def test_timeout_auto_cancels_the_resting_order() -> None:
    """等待回报超时后单子还挂着：自动撤单，不再留在柜台等行情反向时成交。"""
    client = FakeClient()
    cancels: list[dict] = []

    def accept(listener, order):
        listener.on_order_status(order["order_ref"], "3", 0, "已报", order_sys_id="1001")

    def cancel(_settings, payload):
        cancels.append(dict(payload))
        client.listener.on_order_status(payload["order_ref"], "5", 0, "已撤")

    client.on_insert = accept
    client.cancel = cancel
    gateway, trader = _auto_cancel_trader(client)
    result = trader.place_order(OrderRequest("bu2612", "buy_open", 1, 4569))
    assert len(cancels) == 1
    assert result.raw["working"] is False
    assert result.raw["filled_volume"] == 0
    assert result.ok is False
    assert "未成交" in result.message
    assert trader.has_working_order("bu2612") is False


def test_timeout_without_counter_id_keeps_blocking_new_orders() -> None:
    """柜台一直不回编号时撤不了单，但也不能当成成交：仍要挡住后续开仓。"""
    client = FakeClient()
    cancels: list[dict] = []

    def accept(listener, order):
        listener.on_order_status(order["order_ref"], "3", 0, "已报")

    client.on_insert = accept
    client.cancel = lambda _settings, payload: cancels.append(dict(payload))
    gateway, trader = _auto_cancel_trader(client)
    result = trader.place_order(OrderRequest("bu2612", "buy_open", 1, 4569))
    assert cancels == []
    assert result.raw["working"] is True
    assert gateway.table.get("1").status == "submitted"
    assert trader.has_working_order("bu2612") is True


def test_auto_cancel_can_be_switched_off() -> None:
    """配置关掉自动撤单时，超时后保持原来的挂单行为。"""
    client = FakeClient()
    cancels: list[dict] = []

    def accept(listener, order):
        listener.on_order_status(order["order_ref"], "3", 0, "已报", order_sys_id="1001")

    client.on_insert = accept
    client.cancel = lambda _settings, payload: cancels.append(dict(payload))
    gateway, trader = _auto_cancel_trader(client, on=False)
    trader.place_order(OrderRequest("bu2612", "buy_open", 1, 4569))
    assert cancels == []
    assert gateway.table.get("1").status == "submitted"


def test_settings_read_the_auto_cancel_switches() -> None:
    """两个新开关从 config.yaml 的 trade.ctp 段读入。"""
    settings = CtpSettings.from_cfg(
        {"trade": {"ctp": {"auto_cancel_on_timeout": "false", "cancel_wait_sec": 1.5}}}
    )
    assert settings.auto_cancel_on_timeout is False
    assert settings.cancel_wait_sec == 1.5


def test_order_report_and_trade_event_do_not_double_count() -> None:
    """委托回报带累计量、成交回报带增量，同一笔成交不能被算两遍。"""
    from src.trader.ctp_logic import TrackedOrder

    order = TrackedOrder("1", "bu2612", "0", "0", 2, 4500)
    assert apply_order_report(order, status_code="1", filled=1) == "partial"
    assert order.filled == 1
    # 同一笔成交的增量回报再来一次：累计值仍是 1，不能变成 2。
    assert apply_trade(order, 4501, 1) == "partial"
    assert order.filled == 1
    assert order.traded == 1
    # 下一手：委托累计 2 → 全部成交。
    assert apply_order_report(order, status_code="1", filled=2) == "filled"
    assert order.filled == 2
    assert apply_trade(order, 4502, 1) == "filled"
    assert order.filled == 2
    assert order.traded == 2
    assert order.avg_price == 4501.5


def test_runner_rolls_over_daytime_counters_when_date_changes(tmp_path) -> None:
    """挂机跨日后，昨日盈亏、开仓次数和熔断必须清零；持仓不动。"""
    from src.scheduler.runner import TradingBot

    cfg = {
        "dry_run": False,
        "trade": {"symbol": "bu2612", "executor": "sim", "volume": 1},
        "strategy": {
            "engine": "daily_body",
            "timeframe": "5",
            "trail_timeframe": "5",
            "symbols": ["bu2612"],
        },
        "schedule": {"scan_interval_sec": 30, "trading_sessions": [["09:00", "15:00"]]},
        "risk": {"sizing_mode": "fixed", "capital": 10000, "max_daily_loss": 1000},
        "data": {},
        "_project_root": str(tmp_path),
    }
    bot = TradingBot(cfg)
    from src.strategy.types import PositionState

    # 隔夜持仓：换日只清日内计数，这一手要留着，并写进事件记录。
    bot.strategy.positions["bu2612"] = PositionState(
        symbol="bu2612",
        side="long",
        volume=1,
        entry_price=4500.0,
        initial_stop=4400.0,
        trail_stop=4450.0,
    )
    bot.trading_day = "2000-01-01"
    bot.risk.daily_pnl = -500.0
    bot.risk.orders_today = 3
    bot.risk.open_orders_today = 3
    bot.risk.halted = True
    bot.risk.halt_reason = "旧熔断"

    assert bot._rollover_trading_day() is True
    assert bot.risk.daily_pnl == 0.0
    assert bot.risk.orders_today == 0
    assert bot.risk.open_orders_today == 0
    assert bot.risk.halted is False
    assert bot.risk.halt_reason == ""
    # 跨日持仓还在，开仓价/止损都没被清掉。
    held = bot.strategy.positions["bu2612"]
    assert held.side == "long" and held.volume == 1
    assert held.entry_price == 4500.0 and held.trail_stop == 4450.0

    rows = [
        json.loads(line)
        for line in (tmp_path / "data" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rollover = [row for row in rows if row.get("event") == "day_rollover"][-1]
    assert rollover["daily_pnl_before"] == -500.0
    assert rollover["positions"]["bu2612"]["volume"] == 1
    assert rollover["positions"]["bu2612"]["trail_stop"] == 4450.0
    # 同一天再调用不重复清零。
    assert bot._rollover_trading_day() is False


def test_strategy_volume_cannot_exceed_risk_sized_lots() -> None:
    """策略写 meta.volume 时只能往下压，不能超过资金管理算出的手数。"""
    from types import SimpleNamespace

    from src.scheduler.runner import TradingBot
    from src.strategy.types import StrategyAction, StrategyDecision

    class FakeSizer:
        @staticmethod
        def calc_open(*_args, **_kwargs):
            return SimpleNamespace(volume=1, reason="风控只允许1手")

    class FakeRisk:
        positions: dict[str, int] = {}

        @staticmethod
        def total_abs_position() -> int:
            return 0

    stub = SimpleNamespace(sizer=FakeSizer(), risk=FakeRisk())
    decision = StrategyDecision(
        StrategyAction.BUY_OPEN,
        "jm2701",
        "测试",
        entry_price=1000.0,
        stop_price=970.0,
        meta={"volume": 3},
    )
    vol, note = TradingBot._size_open(stub, decision)
    assert vol == 1
    assert "取小1手" in note

    # 填 0 手时仍然跳过，不会被当成「没指定」。
    decision.meta = {"volume": 0}
    vol, _note = TradingBot._size_open(stub, decision)
    assert vol == 0


def test_runner_accounts_the_partial_fill_left_over_by_the_broker() -> None:
    """柜台回调没记账时，调度层要按成交手数补记，而不是整笔当没成交。"""
    from src.scheduler.runner import TradingBot
    from src.strategy.types import StrategyAction, StrategyDecision
    from src.trader.base import OrderResult

    applied: list[tuple[int, float]] = []

    class Stub:
        def _apply_fill_accounting(self, symbol, decision, vol, entry, fill, **_kwargs):
            applied.append((vol, fill))

        def _record_event(self, event, **_kwargs):
            return None

    decision = StrategyDecision(StrategyAction.BUY_OPEN, "bu2612", "测试", entry_price=4569)
    result = OrderResult(
        ok=True,
        message="成交1手后撤单（共成交 1 手）",
        raw={
            "filled": False,
            "accounted": False,
            "fill_price": 4569.0,
            "filled_volume": 1,
            "accounted_volume": 0,
            "working": False,
            "statuses": ["成交1手后撤单"],
        },
    )
    TradingBot._finish_ctp_order(Stub(), decision, 2, result, "buy_open", "")
    assert applied == [(1, 4569.0)]


def test_ctp_fill_passes_fill_key_and_stop_price() -> None:
    """柜台回报要带上报单号加手数的去重键，以及止损价。"""
    from src.scheduler.runner import TradingBot

    seen: dict = {}

    class Stub:
        def _apply_fill_accounting(self, symbol, decision, vol, entry, fill, **kwargs):
            seen.update(
                symbol=symbol,
                action=decision.action.value,
                vol=vol,
                entry=entry,
                fill=fill,
                **kwargs,
            )

        def _record_event(self, *args, **kwargs):
            return None

    report = {
        "order_ref": "7",
        "symbol": "bu2612",
        "direction": "0",
        "offset": "0",
        "avg_price": 4569,
        "filled": 1,
        "bar_id": "2026-11-01 10:00",
        "stop_price": 4500,
    }
    TradingBot._on_ctp_fill(Stub(), report)
    assert seen["fill_key"] == "7|buy_open|1"
    assert seen["stop_price"] == 4500.0


def test_duplicate_ctp_fill_report_is_not_booked_twice(tmp_path, monkeypatch) -> None:
    """同一份柜台回报再来一次，账本只记一笔，持仓也不能加两次。"""
    from src.account.signal_book import load_book
    from src.scheduler.runner import TradingBot

    # 记账成功后会把余额写进真实配置文件，测试里必须挡住。
    monkeypatch.setattr("src.account.signal_book.write_capital_to_config", lambda balance: None)
    cfg = {
        "dry_run": False,
        "trade": {"symbol": "bu2612", "executor": "sim", "volume": 1},
        "strategy": {
            "engine": "daily_body",
            "timeframe": "5",
            "trail_timeframe": "5",
            "symbols": ["bu2612"],
        },
        "schedule": {"scan_interval_sec": 30, "trading_sessions": [["09:00", "15:00"]]},
        "risk": {"sizing_mode": "fixed", "capital": 10000, "max_daily_loss": 1000},
        "data": {},
        "_project_root": str(tmp_path),
    }
    bot = TradingBot(cfg)
    report = {
        "order_ref": "9",
        "symbol": "bu2612",
        "direction": "0",
        "offset": "0",
        "avg_price": 4569,
        "filled": 2,
        "bar_id": "2026-11-01 10:00",
        "stop_price": 4500,
    }
    bot._on_ctp_fill(report)
    bot._on_ctp_fill(report)

    book = load_book(tmp_path)
    rows = [row for row in book["trades"] if str(row.get("fill_key") or "") == "9|buy_open|2"]
    assert len(rows) == 1
    assert bot.strategy.positions["bu2612"].volume == 2
