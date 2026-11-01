"""交易时段判断测试。"""

from datetime import datetime

from src.scheduler.runner import in_trading_session


def test_in_morning_session() -> None:
    sessions = [["09:00", "11:30"], ["13:30", "15:00"]]
    now = datetime(2026, 10, 13, 10, 0, 0)
    assert in_trading_session(sessions, now) is True


def test_out_of_session() -> None:
    sessions = [["09:00", "11:30"]]
    now = datetime(2026, 10, 13, 12, 0, 0)
    assert in_trading_session(sessions, now) is False
