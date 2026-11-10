"""日期日历和时间下拉写成的字符串，要和原来手填的格式一样。"""

from datetime import date

from src.ui.datetime_pickers import combine_datetime, format_date, parse_date_text, parse_time_text


def test_parse_date_reads_common_shapes() -> None:
    assert parse_date_text("2026-09-22") == date(2026, 9, 22)
    assert parse_date_text("2026/09/22") == date(2026, 9, 22)
    assert parse_date_text("") is None
    assert parse_date_text("不是日期") is None


def test_parse_time_reads_hour_minute() -> None:
    assert parse_time_text("10:50") == (10, 50)
    assert parse_time_text("21:00:00") == (21, 0)
    assert parse_time_text("") is None
    assert parse_time_text("不选") is None


def test_combine_datetime_keeps_old_delete_format() -> None:
    assert combine_datetime("2026-10-14", "") == "2026-10-14"
    assert combine_datetime("2026-10-14", "21:00") == "2026-10-14 21:00:00"
    assert combine_datetime("", "21:00") == ""
    assert format_date(date(2026, 1, 1)) == "2026-01-01"
