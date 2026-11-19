"""节假日日历：外置 YAML 解析、缺年份识别、与内置 2026 表等价。"""

from datetime import date, datetime
from pathlib import Path

from src.utils.holidays import HolidayCalendar, calendar_from_config
from src.utils.trading_day import EXCHANGE_HOLIDAYS, calendar_coverage_ok, is_trading_day

ROOT = Path(__file__).resolve().parents[1]


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "holidays.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_shipped_calendar_matches_builtin_2026() -> None:
    """仓库自带日历和硬编码的 2026 表必须逐日一致，升级当天行为不能变。"""
    calendar = HolidayCalendar.load(ROOT / "config" / "holidays.yaml")
    assert calendar is not None
    assert set(calendar.years[2026]) == set(EXCHANGE_HOLIDAYS)
    day = date(2026, 1, 1)
    while day.year == 2026:
        assert is_trading_day(day, calendar=calendar) == is_trading_day(day), day
        day = date.fromordinal(day.toordinal() + 1)


def test_extra_year_is_recognized(tmp_path: Path) -> None:
    """补上 2027 后，2027 的节假日按休市处理。"""
    path = _write(
        tmp_path,
        "version: 1\nyears:\n  2027:\n    - '2027-01-01'\n    - '2027-10-01'\n",
    )
    calendar = HolidayCalendar.load(path)
    assert calendar is not None
    assert calendar.is_closed(date(2027, 1, 1))
    assert not is_trading_day(date(2027, 1, 1), calendar=calendar)
    # 没列的工作日照常交易。
    assert is_trading_day(date(2027, 1, 4), calendar=calendar)


def test_bad_rows_are_skipped(tmp_path: Path) -> None:
    """坏日期和跨年日期跳过，不影响其它行。"""
    path = _write(
        tmp_path,
        "version: 1\nyears:\n  2026:\n    - '2026-13-40'\n    - 'nope'\n"
        "    - '2027-01-01'\n    - '2026-01-01'\n",
    )
    calendar = HolidayCalendar.load(path)
    assert calendar is not None
    assert calendar.years[2026] == frozenset({date(2026, 1, 1)})


def test_empty_year_counts_as_covered(tmp_path: Path) -> None:
    """写空列表表示「该年已确认没有节假日」，不算缺配置。"""
    path = _write(tmp_path, "version: 1\nyears:\n  2027: []\n")
    calendar = HolidayCalendar.load(path)
    assert calendar is not None
    assert calendar.covers(date(2027, 6, 1))
    assert not calendar.is_closed(date(2027, 6, 1))


def test_missing_year_not_covered(tmp_path: Path) -> None:
    """只配了 2026 时，2027 属于缺年份。"""
    path = _write(tmp_path, "version: 1\nyears:\n  2026:\n    - '2026-01-01'\n")
    calendar = HolidayCalendar.load(path)
    assert calendar is not None
    assert calendar.covers(date(2026, 12, 31))
    assert not calendar.covers(date(2027, 1, 4))
    assert calendar.covered_until() == date(2026, 12, 31)


def test_missing_or_broken_file_falls_back(tmp_path: Path) -> None:
    """文件不存在或写坏时返回 None，调用方回退内置表。"""
    assert HolidayCalendar.load(tmp_path / "nope.yaml") is None
    broken = _write(tmp_path, "years: [oops\n")
    assert HolidayCalendar.load(broken) is None
    empty = _write(tmp_path, "version: 1\nyears: {}\n")
    assert HolidayCalendar.load(empty) is None


def test_calendar_from_config_resolves_relative_path(tmp_path: Path) -> None:
    """配置里的相对路径按项目根解析。"""
    folder = tmp_path / "config"
    folder.mkdir()
    (folder / "holidays.yaml").write_text(
        "version: 1\nyears:\n  2026:\n    - '2026-01-01'\n", encoding="utf-8"
    )
    calendar = calendar_from_config({"schedule": {"holidays_file": "config/holidays.yaml"}}, tmp_path)
    assert calendar is not None
    assert calendar.is_closed(date(2026, 1, 1))


def test_coverage_check() -> None:
    """未来 60 天跨年时，缺 2027 就算覆盖不足。"""
    calendar = HolidayCalendar({2026: frozenset()}, source="test")
    assert calendar_coverage_ok(calendar, datetime(2026, 11, 15), min_days=30)
    assert not calendar_coverage_ok(calendar, datetime(2026, 11, 15), min_days=60)
    # 没外置日历时按内置表处理，一律放行，避免旧环境突然停开仓。
    assert calendar_coverage_ok(None, datetime(2026, 11, 15), min_days=60)


def test_shipped_calendar_covers_sixty_days_in_november_2026() -> None:
    """仓库自带表必须盖住 2026 年 11 月开实盘所需的 60 天，否则一开 CTP 就会禁开仓。"""
    calendar = HolidayCalendar.load(ROOT / "config" / "holidays.yaml")
    assert calendar is not None
    assert calendar.covers(date(2027, 1, 17))
    assert calendar_coverage_ok(calendar, datetime(2026, 11, 18), min_days=60)
    assert calendar.is_closed(date(2027, 1, 1))
