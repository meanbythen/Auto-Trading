"""状态栏执行通道卡片：CTP 要写出连没连上。"""

from src.ui.app_common import format_exec_card


def test_gui_channel_keeps_old_line() -> None:
    assert format_exec_card("gui") == "同花顺点击 gui"
    assert format_exec_card("gui", running=True, daily_pnl=12) == "同花顺点击 gui · 今日+12元"


def test_ctp_idle_says_not_started() -> None:
    assert format_exec_card("ctp") == "CTP 柜台\n未挂机"


def test_ctp_connected_shows_account() -> None:
    text = format_exec_card(
        "ctp",
        running=True,
        dry_run=False,
        ctp_ready=True,
        ctp_user="20318052",
        ctp_kind="live",
        daily_pnl=0,
    )
    assert text == "CTP 已连接\n真实 20318052\n今日+0元"


def test_ctp_down_shows_auth_hint() -> None:
    text = format_exec_card(
        "ctp",
        running=True,
        dry_run=False,
        ctp_ready=False,
        ctp_error="还不能连接柜台，缺少：认证码（密钥没读到）",
    )
    assert text == "CTP 未连接\n密钥没读到"


def test_ctp_dry_run_does_not_pretend_connected() -> None:
    text = format_exec_card("ctp", running=True, dry_run=True, ctp_ready=False)
    assert "不连柜台" in text
    assert "已连接" not in text
