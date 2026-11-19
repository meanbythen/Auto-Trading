"""准备同花顺时不能下单：登录失败或对手价失败，都不能算准备完成。"""

from src.login.prepare import prepare_trading_desk, startup_should_login


def _settled() -> bool:
    return True


def test_login_failure_does_not_continue() -> None:
    ok, lines = prepare_trading_desk(
        {"trade": {"executor": "gui"}, "account": {}},
        login=lambda _cfg: (False, "登录失败"),
        arm_price=lambda _cfg: (_raise_if_called(), ""),
        confirm_settlement=_settled,
    )
    assert ok is False
    assert any("登录失败" in line for line in lines)


def test_price_failure_blocks_start() -> None:
    ok, lines = prepare_trading_desk(
        {"trade": {"executor": "gui"}, "account": {}},
        login=lambda _cfg: (True, "已登录"),
        arm_price=lambda _cfg: (False, "没有点到对手价"),
        confirm_settlement=_settled,
        dismiss_settlement=lambda: "absent",
    )
    assert ok is False
    assert any("没有点到对手价" in line for line in lines)
    assert any("不会启动" in line for line in lines)


def test_gui_ready_does_not_claim_an_order() -> None:
    ok, lines = prepare_trading_desk(
        {"trade": {"executor": "gui"}, "account": {"skip_auto_login": True}},
        login=lambda cfg: (cfg["account"]["skip_auto_login"] is False, "已登录"),
        arm_price=lambda _cfg: (True, "已改成对手价"),
        confirm_settlement=_settled,
    )
    assert ok is True
    text = "\n".join(lines)
    assert "买开" in text
    assert "没有点买开" in text


def test_sim_channel_does_not_open_ths() -> None:
    """模拟通道也不走同花顺准备，避免定时登录误开软件。"""
    called = {"login": False, "price": False}

    def _login(_cfg):
        called["login"] = True
        return True, "不该被调用"

    def _arm(_cfg):
        called["price"] = True
        return True, "不该被调用"

    ok, lines = prepare_trading_desk(
        {"trade": {"executor": "sim"}, "account": {}},
        login=_login,
        arm_price=_arm,
        confirm_settlement=_settled,
    )
    assert ok is True
    assert called["login"] is False
    assert called["price"] is False
    assert any("不打开同花顺" in line for line in lines)


def test_ctp_channel_does_not_open_ths() -> None:
    """CTP 通道绝不能拉同花顺或点华泰登录框。"""
    called = {"login": False}

    def _login(_cfg):
        called["login"] = True
        return True, "不该被调用"

    ok, lines = prepare_trading_desk(
        {"trade": {"executor": "ctp"}, "account": {}},
        login=_login,
        arm_price=lambda _cfg: (_raise_if_called(), ""),
        confirm_settlement=_settled,
    )
    assert ok is True
    assert called["login"] is False
    assert any("ctp" in line.lower() for line in lines)
    assert any("不打开同花顺" in line for line in lines)


def test_settlement_window_blocks_the_resident_start() -> None:
    """结算单窗口还在、按钮没点到时，不能接着改价格，也不能启动常驻。"""
    ok, lines = prepare_trading_desk(
        {"trade": {"executor": "gui"}, "account": {}},
        login=lambda _cfg: (True, "已登录"),
        arm_price=lambda _cfg: (_raise_if_called(), ""),
        confirm_settlement=lambda: False,
    )
    assert ok is False
    assert any("确认结算单" in line for line in lines)


def test_settlement_covering_the_price_menu_is_clicked_then_retried() -> None:
    """对手价点不到时，如果结算单还挡着，点掉它再改一次价格。"""
    calls = {"n": 0}

    def _arm(_cfg):
        calls["n"] += 1
        if calls["n"] == 1:
            return False, "没有点到对手价"
        return True, "已改成对手价"

    ok, lines = prepare_trading_desk(
        {"trade": {"executor": "gui"}, "account": {}},
        login=lambda _cfg: (True, "已登录"),
        arm_price=_arm,
        confirm_settlement=_settled,
        dismiss_settlement=lambda: "clicked",
    )
    assert ok is True
    assert calls["n"] == 2
    text = "\n".join(lines)
    assert "已经点掉" in text
    assert "已改成对手价" in text


def test_prepare_success_means_resident_should_not_login_again() -> None:
    assert startup_should_login({}) is True
    assert startup_should_login({"_startup_login_done": True}) is False


def _raise_if_called() -> bool:
    raise AssertionError("登录失败后不应该再去改价格")
