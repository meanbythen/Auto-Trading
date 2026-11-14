"""交易时段判断测试。"""

from datetime import datetime, time as dtime

from src.scheduler.runner import in_trading_session
from src.utils.session_clock import cutoff_passed, last_open_at


def test_in_morning_session() -> None:
    sessions = [["09:00", "11:30"], ["13:30", "15:00"]]
    now = datetime(2026, 10, 13, 10, 0, 0)
    assert in_trading_session(sessions, now) is True


def test_out_of_session() -> None:
    sessions = [["09:00", "11:30"]]
    now = datetime(2026, 10, 13, 12, 0, 0)
    assert in_trading_session(sessions, now) is False


def test_grace_keeps_last_bar_after_session_end() -> None:
    """11:30 收线后几十秒仍算在时段里，否则最后一根永远扫不到。"""
    sessions = [["09:00", "11:30"]]
    assert in_trading_session(sessions, datetime(2026, 10, 13, 11, 30, 30)) is True
    assert in_trading_session(sessions, datetime(2026, 10, 13, 11, 32, 0)) is False


def test_old_2300_config_is_not_overnight() -> None:
    """沥青这类写到 23:00 的品种，凌晨不算在盘里。"""
    sessions = [["09:00", "11:30"], ["13:30", "15:00"], ["21:00", "23:00"]]
    assert in_trading_session(sessions, datetime(2026, 11, 10, 0, 37)) is False
    assert in_trading_session(sessions, datetime(2026, 11, 10, 22, 10)) is True


def test_ag_uses_late_night_even_without_table_row() -> None:
    """品种表没填时，白银默认收到 02:30，燃油默认到 23:00。"""
    from src.utils.session_clock import in_trading_sessions, sessions_for_symbol

    cfg = {"schedule": {"trading_sessions": [["09:00", "11:30"], ["13:30", "15:00"], ["21:00", "23:00"]]}}
    ag = sessions_for_symbol(cfg, "ag2702")
    fu = sessions_for_symbol(cfg, "fu2701")
    now = datetime(2026, 11, 10, 0, 37)
    assert in_trading_sessions(ag, now) is True
    assert in_trading_sessions(fu, now) is False


def test_overnight_session_covers_evening_and_early_morning() -> None:
    """白银夜盘 21:00～次日 02:30：晚上和凌晨都算在盘里，中间休市不算。"""
    sessions = [["09:00", "11:30"], ["13:30", "15:00"], ["21:00", "02:30"]]
    assert in_trading_session(sessions, datetime(2026, 11, 9, 22, 10)) is True
    assert in_trading_session(sessions, datetime(2026, 11, 10, 0, 27)) is True
    assert in_trading_session(sessions, datetime(2026, 11, 10, 2, 30)) is True
    assert in_trading_session(sessions, datetime(2026, 11, 10, 2, 32)) is False
    assert in_trading_session(sessions, datetime(2026, 11, 9, 20, 50)) is False
    assert in_trading_session(sessions, datetime(2026, 11, 10, 3, 0)) is False


def test_last_open_at_uses_yesterday_after_midnight() -> None:
    """凌晨看夜盘开盘，应该是昨天 21:00。"""
    now = datetime(2026, 11, 10, 1, 5)
    assert last_open_at(now, dtime(21, 0)) == datetime(2026, 11, 9, 21, 0)
    assert last_open_at(datetime(2026, 11, 9, 21, 40), dtime(21, 0)) == datetime(2026, 11, 9, 21, 0)


def test_night_cutoff_same_evening_also_blocks_after_midnight() -> None:
    """截止写 22:30 时，当天晚上过了和次日凌晨都算过了，不会凌晨又去追。"""
    open_at = dtime(21, 0)
    cutoff = dtime(22, 30)
    assert cutoff_passed(datetime(2026, 11, 9, 22, 10), open_at, cutoff) is False
    assert cutoff_passed(datetime(2026, 11, 9, 22, 31), open_at, cutoff) is True
    assert cutoff_passed(datetime(2026, 11, 10, 1, 0), open_at, cutoff) is True


def test_session_bounds_follow_the_table() -> None:
    """燃油夜盘收到 23:00，白银收到 02:30，日盘开盘都跟表走。"""
    from src.utils.session_clock import bounds_for_symbol, in_flatten_window

    cfg = {
        "risk": {
            "contracts": {
                "fu": {
                    "trading_sessions": "09:00-11:30,13:30-15:00,21:00-23:00",
                    "flatten_times": "14:55,22:55",
                },
                "ag": {
                    "trading_sessions": "09:00-11:30,13:30-15:00,21:00-02:30",
                    "flatten_times": "14:55,02:25",
                },
            }
        }
    }
    fu = bounds_for_symbol(cfg, "fu2701")
    ag = bounds_for_symbol(cfg, "ag2702")
    assert fu["day_open"] == dtime(9, 0)
    assert fu["night_close"] == dtime(23, 0)
    assert ag["night_close"] == dtime(2, 30)
    assert in_flatten_window([dtime(2, 25)], datetime(2026, 11, 10, 2, 26)) is True
    assert in_flatten_window([dtime(22, 55)], datetime(2026, 11, 10, 0, 1)) is False


def test_near_session_close_uses_real_close_not_lunch() -> None:
    """日盘收盘 15:00、夜盘 23:00；上午 11:30 午休还可以开。"""
    from src.utils.session_clock import near_session_close

    sessions = [["09:00", "11:30"], ["13:30", "15:00"], ["21:00", "23:00"]]
    assert near_session_close(datetime(2026, 11, 13, 14, 50), sessions, minutes=10) is True
    assert near_session_close(datetime(2026, 11, 13, 14, 40), sessions, minutes=10) is False
    assert near_session_close(datetime(2026, 11, 13, 11, 25), sessions, minutes=10) is False
    assert near_session_close(datetime(2026, 11, 13, 22, 51), sessions, minutes=10) is True
    assert near_session_close(datetime(2026, 11, 13, 22, 40), sessions, minutes=10) is False


def test_near_session_close_overnight_uses_next_morning() -> None:
    """白银夜盘收到 02:30：晚上 22 点还早，凌晨 02:22 才算收盘前。"""
    from src.utils.session_clock import near_session_close

    sessions = [["09:00", "11:30"], ["13:30", "15:00"], ["21:00", "02:30"]]
    assert near_session_close(datetime(2026, 11, 13, 22, 55), sessions, minutes=10) is False
    assert near_session_close(datetime(2026, 11, 14, 2, 20), sessions, minutes=10) is True
    assert near_session_close(datetime(2026, 11, 14, 2, 10), sessions, minutes=10) is False
