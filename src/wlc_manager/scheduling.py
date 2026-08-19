from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from wlc_manager.config import Weekday

if TYPE_CHECKING:
    from wlc_manager.config import SchedulerConfig

_MONTH_PATTERN = re.compile(r"^(?P<year>[0-9]{4})-(?P<month>0[1-9]|1[0-2])$")


class ScheduleError(ValueError):
    """Raised when a requested schedule period is invalid."""


@dataclass(frozen=True, order=True, slots=True)
class YearMonth:
    year: int
    month: int

    def __post_init__(self) -> None:
        if self.year < 1 or not 1 <= self.month <= 12:
            raise ScheduleError(f"invalid year and month: {self.year:04d}-{self.month:02d}")

    @classmethod
    def parse(cls, value: str) -> YearMonth:
        match = _MONTH_PATTERN.fullmatch(value)
        if match is None:
            raise ScheduleError(f"invalid month {value!r}; expected YYYY-MM")
        return cls(year=int(match.group("year")), month=int(match.group("month")))

    @classmethod
    def from_date(cls, value: date) -> YearMonth:
        return cls(value.year, value.month)

    def first_date(self) -> date:
        return date(self.year, self.month, 1)

    def next(self) -> YearMonth:
        if self.month == 12:
            return YearMonth(self.year + 1, 1)
        return YearMonth(self.year, self.month + 1)

    def __str__(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


def is_workday(value: date) -> bool:
    """Return whether the date is Monday through Friday; holidays are not considered."""
    return value.weekday() < 5


def first_workday(period: YearMonth) -> date:
    value = period.first_date()
    while not is_workday(value):
        value += timedelta(days=1)
    return value


def generation_date(period: YearMonth, *, lead_workdays: int = 3) -> date:
    """Calculate the date that is N weekdays before the period's first weekday."""
    if lead_workdays < 0:
        raise ScheduleError("lead_workdays cannot be negative")

    value = first_workday(period)
    remaining = lead_workdays
    while remaining:
        value -= timedelta(days=1)
        if is_workday(value):
            remaining -= 1
    return value


def generation_is_due(today: date, period: YearMonth, *, lead_workdays: int = 3) -> bool:
    """Return true once the generation date has been reached, including catch-up runs."""
    return today >= generation_date(period, lead_workdays=lead_workdays)


def password_application_is_due(today: date, period: YearMonth) -> bool:
    """Return true on and after the first calendar day of the target month."""
    return today >= period.first_date()


def wlan_should_be_enabled(
    moment: datetime,
    *,
    timezone: str,
    schedule: SchedulerConfig,
) -> bool:
    """Calculate desired WLAN state for an aware moment in the configured timezone."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ScheduleError("WLAN state calculation requires a timezone-aware datetime")

    local = moment.astimezone(ZoneInfo(timezone))
    weekday = tuple(Weekday)[local.weekday()]
    window = schedule.work_hours[weekday]
    if window is None:
        return False
    local_time = local.timetz().replace(tzinfo=None)
    return window.start <= local_time < window.end
