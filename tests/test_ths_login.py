"""交易登录框的纯逻辑：不打开同花顺，也不输入真密码。"""

from src.login.ths_login import (
    control_shows_order_panel,
    escape_sendkeys,
    login_trade_account,
    looks_like_order_window,
    order_probe_points,
    pick_popup_menu_item,
    text_shows_order_panel,
)


def test_password_braces_are_typed_as_themselves() -> None:
    """密码里的大括号不能被键盘模拟当成特殊功能键。"""
    assert escape_sendkeys("a{b}c") == "a{{}b{}}c"
    assert escape_sendkeys("plain") == "plain"


def test_popup_menu_item_prefers_the_one_below_the_title_bar() -> None:
    """贴在最顶上的同名菜单不点，点弹出来的那一项。"""
    hidden = object()
    popup = object()
    chosen = pick_popup_menu_item([(46, hidden), (220, popup)])
    assert chosen is popup


def test_order_window_title_is_the_ctp_panel() -> None:
    """截图里的下单窗口标题含「CTP主席」，主窗口和登录框不算。"""
    assert looks_like_order_window("非套期货-CTP主席-20318052") is True
    assert looks_like_order_window("同花顺期货通") is False
    assert looks_like_order_window("同花顺期货通交易") is False
    from src.login.ths_login import trade_window_rank

    assert trade_window_rank("华泰期货_CTP主席-20318052") > trade_window_rank("同花顺期货通")


def test_inner_order_tab_counts_as_the_panel() -> None:
    """下单页签嵌在主窗口里也算开着。交易菜单里的同名项不算。"""
    assert control_shows_order_panel("普通下单", "TextControl", 120, 80, 24) is True
    assert control_shows_order_panel("非套期货-CTP主席-20318052", "WindowControl", 40, 500, 28) is True
    assert control_shows_order_panel("普通下单", "MenuItemControl", 200, 120, 24) is False
    assert control_shows_order_panel("同花顺期货通", "WindowControl", 0, 800, 600) is False


def test_calibrated_price_text_means_the_panel_is_open() -> None:
    """价格框写着「对手」，或按钮是买开、平仓，都算下单窗口还开着。"""
    assert text_shows_order_panel("对手") is True
    assert text_shows_order_panel("买开") is True
    assert text_shows_order_panel("买多") is True
    assert text_shows_order_panel("卖空") is True
    assert text_shows_order_panel("平空") is True
    assert text_shows_order_panel("4649") is False
    assert text_shows_order_panel("同花顺期货通") is False
    points = order_probe_points({"price_input": [675, 1126], "buy_open_btn": [387, 1209]})
    assert points == [(675, 1126), (387, 1209)]


def test_open_order_window_skips_the_login_click() -> None:
    """窗口还在时直接返回，不能再去点交易菜单。"""
    ok, message = login_trade_account("不该被用到", window_open=lambda: True)
    assert ok is True
    assert "不登录" in message


def test_only_top_item_is_still_usable() -> None:
    """只有一份菜单时，即使位置靠上也可以用。"""
    only = object()
    assert pick_popup_menu_item([(46, only)]) is only


def test_settlement_click_only_hits_the_named_button() -> None:
    """标题上的同名文字不能点，只点窗口里那个按钮。买卖按钮更不能点。"""
    from src.login.ths_login import confirm_settlement_if_present, pick_settlement_button

    controls = [
        ("WindowControl", "确认结算单", 400, 300),
        ("TextControl", "确认结算单", 120, 24),
        ("ButtonControl", "取消", 80, 28),
        ("ButtonControl", "买开", 80, 28),
        ("ButtonControl", "确认结算单", 120, 32),
    ]
    assert pick_settlement_button(controls) == 4
    assert pick_settlement_button([("ButtonControl", "确认结算单", 4, 4)]) is None
    # 贴在屏幕最顶上的同名文字是藏起来的副本，不能点。靠下的按钮才点。
    hidden_copy = [
        ("TextControl", "确认结算单", 120, 22, 12),
        ("ButtonControl", "确认结算单", 120, 32, 640),
    ]
    assert pick_settlement_button(hidden_copy) == 1

    # 蓝按钮有时不是按钮类型，标题和按钮都叫同一个名字。点更靠下的那一个。
    drawn = [
        ("TextControl", "确认结算单", 120, 22, 40),
        ("TextControl", "买多", 80, 28, 760),
        ("TextControl", "确认结算单", 110, 30, 690),
    ]
    assert pick_settlement_button(drawn) == 2

    from src.login.ths_login import settlement_click_point, window_title_is_settlement

    assert window_title_is_settlement("确认结算单") is True
    assert window_title_is_settlement("同花顺期货通") is False
    from src.login.ths_login import settlement_search_host

    # 本程序标题里有「期货」，不能走进 Tk 窗口去点结算单。
    assert settlement_search_host("Auto-Trading 45.5.0  ·  期货自动交易台", "TkTopLevel") is False
    assert settlement_search_host("期货自动交易台", "TkTopLevel") is False
    assert settlement_search_host("同花顺期货通", "") is True
    assert settlement_search_host("确认结算单", "") is True
    assert settlement_search_host("华泰期货_CTP主席-20318052", "") is True
    point = settlement_click_point(200, 120, 980, 760)
    assert point is not None
    assert 200 < point[0] < 980
    assert 168 < point[1] < 760
    assert settlement_click_point(0, 0, 1600, 900) is None

    assert confirm_settlement_if_present(wait_sec=0, look=lambda: "absent") is True
    assert confirm_settlement_if_present(wait_sec=0, look=lambda: "clicked") is True
    assert confirm_settlement_if_present(wait_sec=0, look=lambda: "stuck") is False


def test_settlement_check_runs_once_per_trading_day(monkeypatch) -> None:
    """同一交易日只查一次；换交易日才再查。下单链路不再调用它。"""
    from datetime import date

    from src.login import ths_login as ths

    calls: list[int] = []
    day = {"value": "2026-11-09"}
    monkeypatch.setattr(ths, "_SETTLEMENT_DONE_DAY", "")
    monkeypatch.setattr(ths, "settlement_session_day", lambda: date.fromisoformat(day["value"]))
    monkeypatch.setattr(ths, "_look_and_click_settlement", lambda: calls.append(1) or "absent")

    assert ths.confirm_settlement_if_present(wait_sec=0) is True
    assert ths.confirm_settlement_if_present(wait_sec=0) is True
    assert len(calls) == 1, "同一交易日第二次不该再扫"

    # 结算单会话日换了才重扫。20:50 夜盘登录和次日日盘共用这一天。
    day["value"] = "2026-11-10"
    assert ths.confirm_settlement_if_present(wait_sec=0) is True
    assert len(calls) == 2


def test_settlement_still_open_keeps_retrying(monkeypatch) -> None:
    """结算单还挡着时不记「今天查过」，下次登录还会再查，避免把订单点进结算单。"""
    from datetime import date

    from src.login import ths_login as ths

    calls: list[int] = []
    monkeypatch.setattr(ths, "_SETTLEMENT_DONE_DAY", "")
    monkeypatch.setattr(ths, "settlement_session_day", lambda: date(2026, 11, 9))
    monkeypatch.setattr(ths, "_look_and_click_settlement", lambda: calls.append(1) or "stuck")

    assert ths.confirm_settlement_if_present(wait_sec=0) is False
    assert ths.confirm_settlement_if_present(wait_sec=0) is False
    assert len(calls) == 2


def test_settlement_check_before_login_does_not_use_up_the_day(monkeypatch) -> None:
    """登录前那一眼不占额度；登录后那次才记账，之后整天不再扫。"""
    from datetime import date

    from src.login import ths_login as ths

    calls: list[int] = []
    monkeypatch.setattr(ths, "_SETTLEMENT_DONE_DAY", "")
    monkeypatch.setattr(ths, "settlement_session_day", lambda: date(2026, 11, 9))
    monkeypatch.setattr(ths, "_look_and_click_settlement", lambda: calls.append(1) or "absent")

    assert ths.confirm_settlement_if_present(wait_sec=0, mark_day=False) is True
    assert ths._SETTLEMENT_DONE_DAY == "", "登录前看到没有窗口，不能记成今天查过"
    assert ths.confirm_settlement_if_present(wait_sec=0) is True
    assert ths._SETTLEMENT_DONE_DAY == "2026-11-09"
    assert ths.confirm_settlement_if_present(wait_sec=0) is True
    assert len(calls) == 2


def test_empty_window_handle_is_not_brought_forward() -> None:
    """没有窗口句柄时不要去调用置顶。"""
    from src.login.app_login import force_foreground

    assert force_foreground(0) is False


def test_account_field_must_stay_still_before_password() -> None:
    """账号只出现一次还不能写密码。连续两次相同，才算同花顺填完。"""
    from src.login.ths_login import account_has_settled, account_text_ready, pick_account_text

    assert account_text_ready("20318052", "20318052") is True
    assert account_text_ready("", "20318052") is False
    assert account_text_ready("请选择账号", "20318052") is False
    assert account_text_ready("华泰期货_CTP主席", "20318052") is False
    assert pick_account_text(["华泰期货_CTP主席", "20318052"], "20318052") == "20318052"
    assert pick_account_text(["99990000"], "20318052") == ""
    assert account_has_settled(["", "20318052"], "20318052") is False
    assert account_has_settled(["20318052", "20318052"], "20318052") is True
    assert account_has_settled(["20318052", ""], "20318052") is False


def test_password_waits_until_account_stops_changing() -> None:
    """账号从空变成编号后，要再确认一次没变，才允许写密码。"""
    from src.login.ths_login import _wait_account_settled

    seen = iter(["", "20318052", "20318052", "20318052"])
    clock = {"t": 0.0}

    def read(_dialog: object) -> str:
        return next(seen)

    def now() -> float:
        return clock["t"]

    def sleep(seconds: float) -> None:
        clock["t"] += seconds

    text = _wait_account_settled(
        object(),
        "20318052",
        timeout=8,
        pause=0.35,
        read=read,
        now=now,
        sleep=sleep,
        still_open=lambda: True,
    )
    assert text == "20318052"


def test_grey_login_button_types_password_again(monkeypatch) -> None:
    """第一次写完按钮变灰，就再等账号并重写一次，不带着空密码去点登录。"""
    import src.login.ths_login as ths

    fills: list[str] = []
    monkeypatch.setattr(ths, "_fill_password", lambda _edit, password: fills.append("filled"))
    answers = iter([False, True])
    monkeypatch.setattr(ths, "_login_button_stays_ready", lambda _button, **_kwargs: next(answers))
    monkeypatch.setattr(ths, "_wait_account_settled", lambda *_args, **_kwargs: "20318052")
    result = ths._type_password_until_kept(object(), object(), object(), "secret", "20318052")
    assert result == "click"
    assert fills == ["filled", "filled"]


def test_grey_login_button_twice_does_not_click(monkeypatch) -> None:
    """重写后按钮还是灰的，就停下来，不再点登录。"""
    import src.login.ths_login as ths

    fills: list[str] = []
    monkeypatch.setattr(ths, "_fill_password", lambda _edit, _password: fills.append("filled"))
    monkeypatch.setattr(ths, "_login_button_stays_ready", lambda _button, **_kwargs: False)
    monkeypatch.setattr(ths, "_wait_account_settled", lambda *_args, **_kwargs: "20318052")
    result = ths._type_password_until_kept(object(), object(), object(), "secret", "20318052")
    assert result == "failed"
    assert len(fills) == 2


def test_missing_password_does_not_count_as_logged_in(monkeypatch) -> None:
    """窗口已经打开、但密码没读到时，不能当成登录成功。"""
    from src.login.app_login import AppLoginManager
    import src.login.ths_login as ths

    manager = AppLoginManager(
        {
            "dry_run": False,
            "app": {"window_title": "期货通", "path": ""},
            "account": {"skip_auto_login": False, "resolved_password": "", "user_id": "1"},
            "trade": {"gui": {}},
        }
    )
    monkeypatch.setattr(manager, "start_app", lambda: True)
    monkeypatch.setattr(manager, "focus_window", lambda: True)
    monkeypatch.setattr(manager, "window_exists", lambda: True)
    monkeypatch.setattr(ths, "order_probe_points", lambda _gui: [])
    monkeypatch.setattr(ths, "order_window_is_open", lambda _points=None: False)
    monkeypatch.setattr(ths, "confirm_settlement_if_present", lambda **_kwargs: True)
    assert manager.login() is False
