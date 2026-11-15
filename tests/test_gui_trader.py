"""同花顺点击下单：手数面板必须先收起，再点卖空。不真的移动鼠标。"""

import threading

import src.trader.gui_trader as gui_mod
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
                    "confirm_times": 0,
                    "confirm_delay_sec": 0,
                }
            },
        }
    )
    monkeypatch.setattr(trader, "_focus_ths_before_click", lambda: None)

    result = trader.place_order(OrderRequest("bu2612", "sell_open", 1))

    assert result.ok
    # 点完卖空不再按回车：期货通没有确认框，回车可能再发一单。
    typed_at = next(index for index, item in enumerate(events) if item[0] == "type")
    vol_click = next(index for index, item in enumerate(events) if item[:3] == ("click", 498, 1129))
    sell_at = next(index for index, item in enumerate(events) if item[:3] == ("click", 548, 1206))
    esc_before_type = next(
        index for index, item in enumerate(events) if index > vol_click and item == ("press", "esc") and index < typed_at
    )
    esc_before_sell = next(
        index for index, item in enumerate(events) if index > typed_at and item == ("press", "esc") and index < sell_at
    )
    # 点开手数框后先收回下拉再输入，输入后再收回，然后才点卖空。点完不再按回车。
    assert vol_click < esc_before_type < typed_at < esc_before_sell < sell_at
    assert not any(index > sell_at and item == ("press", "enter") for index, item in enumerate(events))


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
                    "confirm_times": 0,
                    "confirm_delay_sec": 0,
                }
            },
        }
    )


def test_simulator_types_its_own_price_before_the_same_click(monkeypatch) -> None:
    """模拟器先全选价格框，再用限价替换，然后走和常驻一样的填手数、点卖空。点完不再按回车。"""
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
    assert not any(index > sell_at and item == ("press", "enter") for index, item in enumerate(events))


def test_resident_order_does_not_type_a_limit_price(monkeypatch) -> None:
    """常驻即使请求里带了价格，也只核对对手价，不把数字打进价格框。"""
    trader = _trader()
    events = _mute_mouse(monkeypatch, trader, ["对手价", "对手价"])
    result = trader.place_order(
        OrderRequest("bu2612", "sell_open", 1, 4720, bar_id="bar-1", trace_id="OT-GUI-1")
    )
    assert result.ok
    assert result.assumed_fill is True
    assert result.raw["trace_id"] == "OT-GUI-1"
    assert result.raw["bar_id"] == "bar-1"
    assert "trade_id" not in result.raw
    assert "trade_time" not in result.raw
    typed = [item[1] for item in events if item[0] == "type"]
    assert typed == ["1"]
    # 常驻只核对对手价，不能把价格框里的字删掉或改成限价。
    assert ("press", "backspace") not in events
    assert ("press", "delete") not in events


def test_click_succeeded_but_confirm_failed_counts_as_sent(monkeypatch) -> None:
    """点完买卖后关确认框出错，必须按「已发出」返回，否则上层会对着同一根K线再点一次。"""
    trader = _trader()
    _mute_mouse(monkeypatch, trader, ["对手价", "对手价"])

    def _boom() -> None:
        raise RuntimeError("确认框关不掉")

    monkeypatch.setattr(trader, "_dismiss_confirm", _boom)

    result = trader.place_order(OrderRequest("bu2612", "sell_open", 1))

    assert result.ok is True
    assert result.assumed_fill is True
    assert result.raw["unclean"] is True
    assert "核对" in result.message


def test_symbol_switch_is_verified_before_clicking(monkeypatch) -> None:
    """写进合约框后回读不一致，就不点买卖按钮，避免把单点到别的品种上。"""
    trader = _trader()
    trader.gui["symbol_input"] = [100, 200]
    events = _mute_mouse(monkeypatch, trader, ["对手价", "对手价"])
    monkeypatch.setattr(trader, "_price_control_text", lambda point: (None, "ag2612"))

    result = trader.place_order(OrderRequest("bu2612", "sell_open", 1))

    assert result.ok is False
    assert "合约" in result.message
    assert not any(item[:3] == ("click", 548, 1206) for item in events)


def test_symbol_switch_passes_when_readback_matches(monkeypatch) -> None:
    """回读和要切的合约一致时正常下单。"""
    trader = _trader()
    trader.gui["symbol_input"] = [100, 200]
    _mute_mouse(monkeypatch, trader, ["对手价", "对手价"])
    monkeypatch.setattr(trader, "_price_control_text", lambda point: (None, "BU2612"))

    result = trader.place_order(OrderRequest("bu2612", "sell_open", 1))

    assert result.ok is True


def test_place_order_skips_when_another_click_is_running(monkeypatch) -> None:
    """两个 GuiTrader 实例（常驻 / 模拟器）共用一把锁，抢不到就跳过，绝不并发点击。"""
    held = threading.Lock()
    held.acquire()
    monkeypatch.setattr(gui_mod, "_TRADE_LOCK", held)
    monkeypatch.setattr(gui_mod, "_TRADE_LOCK_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr("src.trader.gui_trader.pyautogui.click", lambda **kwargs: None)

    trader = GuiTrader({"dry_run": False, "trade": {"gui": {}}})
    result = trader.place_order(OrderRequest("bu2612", "buy_open", 1))

    assert result.ok is False
    assert "跳过" in result.message
    held.release()


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


def test_order_refocuses_the_window_before_giving_up(monkeypatch) -> None:
    """准备窗口后焦点被抢走时，先拉回同花顺再点，不等到下一轮扫描。"""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_gui_trader")
    events: list[tuple] = []
    refocus = {"n": 0}

    monkeypatch.setattr(
        "src.trader.gui_trader.pyautogui.click",
        lambda **kwargs: events.append(("click", kwargs["x"], kwargs["y"])),
    )
    monkeypatch.setattr("src.trader.gui_trader.pyautogui.press", lambda key: None)
    monkeypatch.setattr("src.trader.gui_trader.pyautogui.hotkey", lambda *keys: None)
    monkeypatch.setattr("src.trader.gui_trader.pyautogui.typewrite", lambda text, interval=0: None)
    monkeypatch.setattr("src.trader.gui_trader.time.sleep", lambda _seconds: None)

    trader = GuiTrader(
        {
            "dry_run": False,
            "trade": {
                "gui": {
                    "volume_input": [498, 1129],
                    "close_btn": [707, 1215],
                    "confirm_mode": "enter",
                    "confirm_times": 0,
                    "confirm_delay_sec": 0,
                }
            },
        }
    )
    monkeypatch.setattr(trader, "_ensure_trade_desk", lambda: True)

    def _focus() -> bool:
        refocus["n"] += 1
        return True

    monkeypatch.setattr(trader, "_focus_ths_before_click", _focus)
    states = iter([False, True])
    monkeypatch.setattr(trader, "_foreground_is_trade_panel", lambda: next(states, True))
    trader._price_reader = lambda: "对手价"

    result = trader.place_order(OrderRequest("fu2701", "close", 1))

    assert result.ok
    assert refocus["n"] == 1
    assert ("click", 707, 1215) in events

def test_volume_readback_mismatch_stops_before_buying(monkeypatch) -> None:
    """键盘兜底打完字后回读手数框；数字没写进去就抛错，不许点买卖。"""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    trader = _trader()
    monkeypatch.setattr(trader, "_write_number_into_box", lambda point, text: False)
    monkeypatch.setattr(trader, "_foreground_is_trade_panel", lambda: True)
    monkeypatch.setattr(trader, "_type_into", lambda key, text: None)
    monkeypatch.setattr(trader, "_price_control_text", lambda point: (object(), "99"))

    try:
        trader._fill_volume("1")
    except RuntimeError as exc:
        assert "手数没有写进手数框" in str(exc)
    else:
        raise AssertionError("手数没写进去，应该抛错")


def test_dismiss_confirm_does_nothing_when_times_zero(monkeypatch) -> None:
    """confirm_times=0 时连回车都不按，避免空按落到买卖键上。以前写成 0 仍会按一轮。"""
    events: list[tuple] = []
    monkeypatch.setattr(
        "src.trader.gui_trader.pyautogui.click",
        lambda **kwargs: events.append(("click", kwargs["x"], kwargs["y"])),
    )
    monkeypatch.setattr(
        "src.trader.gui_trader.pyautogui.press",
        lambda key: events.append(("press", key)),
    )
    monkeypatch.setattr("src.trader.gui_trader.time.sleep", lambda _seconds: None)

    trader = GuiTrader(
        {
            "dry_run": False,
            "trade": {
                "gui": {
                    "close_btn": [707, 1215],
                    "confirm_mode": "enter",
                    "confirm_times": 0,
                    "confirm_delay_sec": 0.2,
                    "confirm_btn": [800, 900],
                }
            },
        }
    )
    trader._order_armed = True
    trader._dismiss_confirm()
    assert events == []


def test_default_confirm_times_is_zero() -> None:
    """没写配置时也不关弹窗，和期货通无确认框一致。"""
    trader = GuiTrader({"dry_run": True, "trade": {"gui": {}}})
    assert trader.confirm_times == 0


def test_confirm_enter_is_skipped_when_no_popup(monkeypatch) -> None:
    """账号仍开着下单确认、且标定了确定按钮时：没弹窗就不按回车，改点确认按钮。"""
    events: list[tuple] = []
    monkeypatch.setattr(
        "src.trader.gui_trader.pyautogui.click",
        lambda **kwargs: events.append(("click", kwargs["x"], kwargs["y"])),
    )
    monkeypatch.setattr(
        "src.trader.gui_trader.pyautogui.press",
        lambda key: events.append(("press", key)),
    )
    monkeypatch.setattr("src.trader.gui_trader.time.sleep", lambda _seconds: None)

    trader = GuiTrader(
        {
            "dry_run": False,
            "trade": {
                "gui": {
                    "close_btn": [707, 1215],
                    "confirm_mode": "both",
                    "confirm_times": 1,
                    "confirm_delay_sec": 0,
                    "confirm_btn": [800, 900],
                }
            },
        }
    )
    trader._pinned_hwnd = 42
    trader._order_armed = True
    monkeypatch.setattr(trader, "_focus_ths_before_click", lambda: True)
    monkeypatch.setattr(trader, "_confirm_dialog_in_front", lambda: False)

    trader._dismiss_confirm()

    assert ("press", "enter") not in events
    assert ("click", 800, 900) in events


def test_dry_run_does_not_block_the_real_mouse(monkeypatch) -> None:
    """模拟点击只关掉四角急停，不锁人手鼠标，避免测试把电脑鼠标冻住。"""
    calls: list[bool] = []

    def _block(flag: bool) -> int:
        calls.append(bool(flag))
        return 1

    monkeypatch.setattr(gui_mod.ctypes.windll.user32, "BlockInput", _block)
    gui_mod.pyautogui.FAILSAFE = True

    token = gui_mod.grab_pointer_for_order(dry_run=True)
    assert token["blocked"] is False
    assert calls == []
    assert gui_mod.pyautogui.FAILSAFE is False

    gui_mod.release_pointer_after_order(token)
    assert calls == []
    assert gui_mod.pyautogui.FAILSAFE is False


def test_live_click_blocks_user_mouse_and_keeps_failsafe_off(monkeypatch) -> None:
    """真下单：挡住人手；程序自己点按钮时四角急停必须关，否则会跟填价格抢鼠标。"""
    calls: list[bool] = []

    def _block(flag: bool) -> int:
        calls.append(bool(flag))
        return 1

    monkeypatch.setattr(gui_mod.sys, "platform", "win32")
    monkeypatch.setattr(gui_mod.sys, "modules", {})
    monkeypatch.setattr(gui_mod.ctypes.windll.user32, "BlockInput", _block)
    gui_mod.pyautogui.FAILSAFE = True

    token = gui_mod.grab_pointer_for_order(dry_run=False)
    assert token["blocked"] is True
    assert calls == [True]
    assert gui_mod.pyautogui.FAILSAFE is False

    gui_mod.release_pointer_after_order(token)
    assert calls == [True, False]
    assert gui_mod.pyautogui.FAILSAFE is False


def test_old_failsafe_would_raise_when_cursor_sits_in_a_corner(monkeypatch) -> None:
    """对照：急停开着、光标在左上角时，点一下就会报错。用来证明我们关掉它是有用的。"""
    gui_mod.pyautogui.FAILSAFE = True
    monkeypatch.setattr(gui_mod.pyautogui, "position", lambda: (0, 0))
    raised = False
    try:
        gui_mod.pyautogui.failSafeCheck()
    except gui_mod.pyautogui.FailSafeException:
        raised = True
    assert raised is True


def test_grab_pointer_lets_clicks_continue_when_cursor_is_in_every_corner(monkeypatch) -> None:
    """下单前先关掉急停后，光标就算在四个角，检查函数也不能抛异常。"""
    corners = list(gui_mod.pyautogui.FAILSAFE_POINTS)
    assert len(corners) >= 4
    gui_mod.grab_pointer_for_order(dry_run=True)
    assert gui_mod.pyautogui.FAILSAFE is False
    for corner in corners:
        monkeypatch.setattr(gui_mod.pyautogui, "position", lambda c=corner: c)
        gui_mod.pyautogui.failSafeCheck()


def test_place_order_does_not_surface_failsafe_error_if_cursor_in_corner(monkeypatch) -> None:
    """走一遍模拟下单：光标假装在角落，不能出现 FailSafe 报错，订单应成功。"""
    monkeypatch.setattr(gui_mod.pyautogui, "position", lambda: (0, 0))
    gui_mod.pyautogui.FAILSAFE = True

    trader = GuiTrader(
        {
            "dry_run": True,
            "trade": {
                "gui": {
                    "volume_input": [10, 10],
                    "buy_open_btn": [0, 0],
                    "sell_open_btn": [20, 20],
                    "close_btn": [30, 30],
                    "confirm_mode": "enter",
                    "confirm_times": 0,
                    "confirm_delay_sec": 0,
                }
            },
        }
    )
    trader._park_own_windows = lambda: None
    trader._restore_own_windows = lambda: None
    trader._unpin_trade_window = lambda: None
    trader._ensure_trade_desk = lambda: True
    trader._foreground_is_trade_panel = lambda: True
    trader._fill_volume = lambda _text: False
    trader._read_price_text = lambda: "对手价"
    trader._prepare_opponent_price = lambda: "对手价"

    result = trader.place_order(OrderRequest("bu2612", "buy_open", 1))
    assert result.ok is True
    assert "FailSafe" not in result.message
    assert "fail-safe" not in result.message.lower()
    assert gui_mod.pyautogui.FAILSAFE is False
