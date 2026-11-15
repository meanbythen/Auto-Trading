"""
节假日日历（外置 YAML）
功能：把交易所休市日放到 config/holidays.yaml，跨年不用改代码。
原理：只收录工作日的休市；周末本来就不交易。文件缺失或写坏时返回 None，
     调用方回退到 trading_day 内置的 2026 表，保证旧环境行为不变。
     实盘启动还会检查覆盖范围，缺年份时禁止开仓（见 calendar_coverage_ok）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import yaml
from loguru import logger

DEFAULT_HOLIDAYS_FILE = "config/holidays.yaml"


@dataclass(frozen=True)
class HolidayCalendar:
    """按年份分组的休市日集合。"""

    years: dict[int, frozenset[date]]
    source: str = ""

    @classmethod
    def load(cls, path: Path | str | None) -> HolidayCalendar | None:
        """读 YAML 日历。文件不存在/格式不对/没有任何有效年份时返回 None，表示用内置表。"""
        if not path:
            return None
        file = Path(path)
        if not file.exists():
            logger.info("节假日日历不存在，沿用内置休市表: {}", file)
            return None
        try:
            raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("节假日日历读不出来，沿用内置休市表: {}（{}）", file, exc)
            return None
        if not isinstance(raw, dict):
            logger.warning("节假日日历格式不对，沿用内置休市表: {}", file)
            return None
        raw_years = raw.get("years") or {}
        if not isinstance(raw_years, dict):
            logger.warning("节假日日历 years 段不是映射，沿用内置休市表: {}", file)
            return None
        years: dict[int, frozenset[date]] = {}
        for key, items in raw_years.items():
            try:
                year = int(key)
            except (TypeError, ValueError):
                logger.warning("节假日日历里年份写错，已跳过: {}", key)
                continue
            days: set[date] = set()
            for item in _as_list(items):
                parsed = _parse_day(item)
                if parsed is None:
                    logger.warning("节假日日历里日期写错，已跳过: {}/{}", year, item)
                    continue
                if parsed.year != year:
                    logger.warning("节假日日历里 {} 的日期跨年了，已跳过: {}", year, parsed)
                    continue
                days.add(parsed)
            years[year] = frozenset(days)
        if not years:
            logger.warning("节假日日历没有有效年份，沿用内置休市表: {}", file)
            return None
        return cls(years=years, source=str(file))

    def is_closed(self, day: date) -> bool:
        """这一天配置里是不是休市。没配置该年份时按不休息处理（由覆盖检查另行拦截开仓）。"""
        return day in self.years.get(day.year, frozenset())

    def covers(self, day: date) -> bool:
        """该日期所在年份是否已有配置（空列表也算已确认）。"""
        return day.year in self.years

    def covered_until(self) -> date:
        """已配置年份的最后一天。"""
        if not self.years:
            return date.min
        return date(max(self.years), 12, 31)

    def years_text(self) -> str:
        """已配置的年份，给日志和界面看。"""
        return ",".join(str(year) for year in sorted(self.years))


def _as_list(items: Any) -> Iterable[Any]:
    """YAML 里某一年写成单个值或多个值都收成列表。"""
    if items is None:
        return ()
    if isinstance(items, (list, tuple, set)):
        return items
    return (items,)


def _parse_day(item: Any) -> date | None:
    """把 YAML 里的日期收成 date；坏数据返回 None。"""
    if isinstance(item, datetime):
        return item.date()
    if isinstance(item, date):
        return item
    text = str(item or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def calendar_from_config(cfg: dict[str, Any] | None, root: Path | str | None = None) -> HolidayCalendar | None:
    """按配置里的 schedule.holidays_file 读日历；相对路径按项目根目录解析。"""
    schedule = (cfg or {}).get("schedule") or {}
    raw = str(schedule.get("holidays_file") or DEFAULT_HOLIDAYS_FILE).strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = (Path(root) if root else _project_root()) / path
    return HolidayCalendar.load(path)


def _project_root() -> Path:
    """延迟导入，避免 utils 内部互相引用。"""
    from src.utils.paths import project_root

    return project_root()
