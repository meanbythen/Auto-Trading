"""同花顺点击下单：手数面板必须先收起，再点卖空。不真的移动鼠标。"""

from src.trader.base import OrderRequest
from src.trader.gui_trader import GuiTrader


def test_escape_closes_the_volume_pad_before_sell(monkeypatch) -> None:
    """填完手数后先按 Esc，再点卖空。卖空在手数面板底下，不收起就会点空。"""
    events: list[tuple] = []

    monkeypatch.setattr(
        "src.trader.gui_trader.pyautogui.click",
        lambda **kwargs: events.append(("click", kwargs["x"], kwargs["y"])),
    )
    monkeypatch.setattr(
        "src.trader.gui_trader.pyautogui.press",
        lambda key: events.append(("press", key)),
    )
    monkeypatch.setattr(
        "src.trader.gui_trader.pyautogui.hotkey",
        lambda *keys: events.append(("hotkey", keys)),
    )
    monkeypatch.setattr(
        "src.trader.gui_trader.pyautogui.typewrite",
        lambda text, interval=0: events.append(("type", text)),
    )
    monkeypatch.setattr("src.trader.gui_trader.time.sleep", lambda _seconds: None)

    trader = GuiTrader(
        {
            "dry_run": False,
            "trade": {
                "gui": {
                    "volume_input": [498, 1129],
                    "buy_open_btn": [382, 1207],
                    "sell_open_btn": [548, 1206],
                    "close_btn": [707, 1215],
                    "confirm_mode": "enter",
                    "confirm_times": 1,
                    "confirm_delay_sec": 0,
                }
            },
        }
    )
    monkeypatch.setattr(trader, "_focus_ths_before_click", lambda: None)

    result = trader.place_order(OrderRequest("bu2612", "sell_open", 1))

    assert result.ok
    typed_at = next(index for index, item in enumerate(events) if item[0] == "type")
    vol_click = next(index for index, item in enumerate(events) if item[:3] == ("click", 498, 1129))
    sell_at = next(index for index, item in enumerate(events) if item[:3] == ("click", 548, 1206))
    esc_before_type = next(
        index for index, item in enumerate(events) if index > vol_click and item == ("press", "esc") and index < typed_at
    )
    esc_before_sell = next(
        index for index, item in enumerate(events) if index > typed_at and item == ("press", "esc") and index < sell_at
    )
    # 点开手数框后先收回下拉再输入，输入后再收回，然后才点卖空。
    assert vol_click < esc_before_type < typed_at < esc_before_sell < sell_at


def test_lot_typed_into_the_price_box_does_not_click_close(monkeypatch) -> None:
    """价格框从对手价变成 1 时，不再点平仓，也不按回车把这张单子发出去。"""
    events: list[tuple] = []
    readings = iter(["对手价", "1"])

    monkeypatch.setattr(
        "src.trader.gui_trader.pyautogui.click",
        lambda **kwargs: events.append(("click", kwargs["x"], kwargs["y"])),
    )
    monkeypatch.setattr(
        "src.trader.gui_trader.pyautogui.press",
        lambda key: events.append(("press", key)),
    )
    monkeypatch.setattr(
        "src.trader.gui_trader.pyautogui.hotkey",
        lambda *keys: events.append(("hotkey", keys)),
    )
    monkeypatch.setattr(
        "src.trader.gui_trader.pyautogui.typewrite",
        lambda text, interval=0: events.append(("type", text)),
    )
    monkeypatch.setattr("src.trader.gui_trader.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "src.trader.price_mode.ensure_opponent_price",
        lambda cfg, panel=None: (True, "已改回对手价"),
    )

    trader = GuiTrader(
        {
            "dry_run": False,
            "trade": {
                "gui": {
                    "price_input": [671, 1127],
                    "volume_input": [498, 1129],
                    "buy_open_btn": [382, 1207],
                    "sell_open_btn": [548, 1206],
                    "close_btn": [707, 1215],
                    "confirm_mode": "enter",
                    "confirm_times": 2,
                    "confirm_delay_sec": 0,
                }
            },
        }
    )
    monkeypatch.setattr(trader, "_focus_ths_before_click", lambda: None)
    trader._price_reader = lambda: next(readings)

    result = trader.place_order(OrderRequest("bu2612", "close", 1))

    assert not result.ok
    assert "1" in result.message
    assert not any(item[:3] == ("click", 707, 1215) for item in events)
    assert ("press", "enter") not in events


def _mute_mouse(monkeypatch, trader: GuiTrader, readings: list[str]) -> list[tuple]:
    """挡住真实鼠标，并按顺序交出价格框读数。"""
    events: list[tuple] = []
    pending = iter(readings)
    monkeypatch.setattr(
        "src.trader.gui_trader.pyautogui.click",
        lambda **kwargs: events.append(("click", kwargs["x"], kwargs["y"])),
    )
    monkeypatch.setattr(
        "src.trader.gui_trader.pyautogui.press",
        lambda key: events.append(("press", key)),
    )
    monkeypatch.setattr(
        "src.trader.gui_trader.pyautogui.hotkey",
        lambda *keys: events.append(("hotkey", keys)),
    )
    monkeypatch.setattr(
        "src.trader.gui_trader.pyautogui.typewrite",
        lambda text, interval=0: events.append(("type", text)),
    )
    monkeypatch.setattr("src.trader.gui_trader.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(trader, "_focus_ths_before_click", lambda: None)
    trader._price_reader = lambda: next(pending)
    return events


def _trader() -> GuiTrader:
    return GuiTrader(
        {
            "dry_run": False,
            "trade": {
                "gui": {
                    "price_input": [671, 1127],
                    "volume_input": [498, 1129],
                    "sell_open_btn": [548, 1206],
                    "confirm_mode": "enter",
                    "confirm_times": 1,
                    "confirm_delay_sec": 0,
                }
            },
        }
    )


def test_simulator_types_its_own_price_before_the_same_click(monkeypatch) -> None:
    """模拟器先全选价格框，再用限价替换，然后走和常驻一样的填手数、点卖空、回车。"""
    trader = _trader()
    events = _mute_mouse(monkeypatch, trader, ["4000", "4000"])
    result = trader.place_order(OrderRequest("bu2612", "sell_open", 1, 4000, gui_price="limit"))
    assert result.ok
    assert result.assumed_fill is False
    typed = [item[1] for item in events if item[0] == "type"]
    assert typed == ["4000", "1"]
    price_at = next(index for index, item in enumerate(events) if item == ("type", "4000"))
    sell_at = next(index for index, item in enumerate(events) if item[:3] == ("click", 548, 1206))
    price_click = next(index for index, item in enumerate(events) if item[:3] == ("click", 671, 1127))
    menu_closed = next(index for index, item in enumerate(events) if index > price_click and item == ("press", "esc"))
    selected_at = next(
        index for index, item in enumerate(events) if index > menu_closed and item == ("hotkey", ("ctrl", "a"))
    )
    # 先收起价格菜单再全选。菜单开着时全选无效，新数字会插进旧价格中间。不能按删除。
    assert price_click < menu_closed < selected_at < price_at < sell_at
    assert ("press", "backspace") not in events
    assert ("press", "delete") not in events


def test_resident_order_does_not_type_a_limit_price(monkeypatch) -> None:
    """常驻即使请求里带了价格，也只核对对手价，不把数字打进价格框。"""
    trader = _trader()
    events = _mute_mouse(monkeypatch, trader, ["对手价", "对手价"])
    result = trader.place_order(OrderRequest("bu2612", "sell_open", 1, 4720))
    assert result.ok
    assert result.assumed_fill is True
    typed = [item[1] for item in events if item[0] == "type"]
    assert typed == ["1"]
    # 常驻只核对对手价，不能把价格框里的字删掉或改成限价。
    assert ("press", "backspace") not in events
    assert ("press", "delete") not in events


def test_ready_trade_window_is_brought_forward_once_and_not_logged_in(monkeypatch) -> None:
    """交易窗口已经在时，只拉到最前一次，不再登录，也不再激活第二遍。"""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    calls = {"front": 0, "login": 0, "panel": 0}

    class _Win:
        title = "非套期货-CTP主席-资金账号"
        _hWnd = 42

    class _Manager:
        def __init__(self, _cfg: dict) -> None:
            self.last_hwnd = 0

        def window_exists(self) -> bool:
            return True

        def start_app(self) -> bool:
            raise AssertionError("软件已经开着，不应该再启动")

        def find_windows(self) -> list:
            return [_Win()]

        def focus_window(self) -> bool:
            raise AssertionError("下单准备不应该再走会连试三次的激活")

        def login(self) -> bool:
            raise AssertionError("交易窗口已经在，不应该登录")

    monkeypatch.setattr("src.login.AppLoginManager", _Manager)
    monkeypatch.setattr(
        "src.login.app_login.force_foreground",
        lambda _hwnd: calls.__setitem__("front", calls["front"] + 1) or True,
    )
    monkeypatch.setattr("src.login.app_login.pin_topmost", lambda _hwnd: None)
    monkeypatch.setattr("src.login.ths_login.confirm_settlement_if_present", lambda **_kwargs: True)
    monkeypatch.setattr(
        "src.login.ths_login.order_window_is_open",
        lambda _points=None: calls.__setitem__("panel", calls["panel"] + 1) or True,
    )
    monkeypatch.setattr(
        "src.login.ths_login.login_trade_account",
        lambda *_args, **_kwargs: calls.__setitem__("login", calls["login"] + 1) or (True, ""),
    )
    monkeypatch.setattr("src.trader.gui_trader.time.sleep", lambda _seconds: None)

    trader = GuiTrader({"dry_run": False, "trade": {"gui": {"price_input": [1, 2]}}})
    assert trader._ensure_trade_desk() is True
    assert calls["front"] == 1
    assert calls["panel"] == 1
    assert calls["login"] == 0
    assert trader._pinned_hwnd == 42


def test_missing_trade_window_logs_in_once(monkeypatch) -> None:
    """没有交易窗口时只登录一次。登录后再看一次，看到了就停止，不再反复找。"""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    calls = {"login": 0, "panel": 0}

    class _Win:
        title = "同花顺期货通"
        _hWnd = 7

    class _Manager:
        def __init__(self, _cfg: dict) -> None:
            self.last_hwnd = 0

        def window_exists(self) -> bool:
            return True

        def find_windows(self) -> list:
            return [_Win()]

    seen = {"n": 0}

    def panel(_points=None) -> bool:
        calls["panel"] += 1
        seen["n"] += 1
        return seen["n"] >= 2

    monkeypatch.setattr("src.login.AppLoginManager", _Manager)
    monkeypatch.setattr("src.login.app_login.force_foreground", lambda _hwnd: True)
    monkeypatch.setattr("src.login.app_login.pin_topmost", lambda _hwnd: None)
    monkeypatch.setattr("src.login.ths_login.confirm_settlement_if_present", lambda **_kwargs: True)
    monkeypatch.setattr("src.login.ths_login.order_window_is_open", panel)
    monkeypatch.setattr(
        "src.login.ths_login.login_trade_account",
        lambda *_args, **_kwargs: calls.__setitem__("login", calls["login"] + 1) or (True, "已登录"),
    )
    monkeypatch.setattr("src.trader.gui_trader.time.sleep", lambda _seconds: None)

    trader = GuiTrader(
        {
            "dry_run": False,
            "account": {"resolved_password": "secret", "user_id": "1"},
            "trade": {"gui": {"price_input": [1, 2]}},
        }
    )
    assert trader._ensure_trade_desk() is True
    assert calls["login"] == 1
    assert calls["panel"] == 2
