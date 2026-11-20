"""K线窗口里用来叠图的均线：只算数，不碰界面。"""

from src.ui.kline_chart import moving_average


def test_moving_average_fills_from_the_window_end() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    line = moving_average(values, 3)
    assert line[:2] == [None, None]
    assert line[2:] == [2.0, 3.0, 4.0]


def test_moving_average_handles_empty_and_non_positive_window() -> None:
    assert moving_average([], 20) == []
    values = [1.0, 2.0, 3.0]
    assert moving_average(values, 0) == [None, None, None]
    assert moving_average(values, -5) == [None, None, None]


def test_moving_average_is_empty_when_bars_are_fewer_than_window() -> None:
    assert moving_average([1.0, 2.0], 20) == [None, None]


def test_moving_average_never_returns_values_beyond_the_points() -> None:
    values = [float(index) for index in range(10)]
    line = moving_average(values, 4)
    assert len(line) == len(values)
    assert line[-1] == sum(values[-4:]) / 4
    assert all(value is None for value in line[:3])
