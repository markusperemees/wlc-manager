from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from wlc_manager.config import load_settings
from wlc_manager.scheduling import (
    ScheduleError,
    YearMonth,
    first_workday,
    generation_date,
    generation_is_due,
    password_application_is_due,
    wlan_should_be_enabled,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("period", "expected_first_workday", "expected_generation_date"),
    [
        (YearMonth(2026, 9), date(2026, 9, 1), date(2026, 8, 27)),
        (YearMonth(2026, 8), date(2026, 8, 3), date(2026, 7, 29)),
        (YearMonth(2027, 1), date(2027, 1, 1), date(2026, 12, 29)),
    ],
)
def test_generation_date_uses_weekdays_only(
    period: YearMonth,
    expected_first_workday: date,
    expected_generation_date: date,
) -> None:
    assert first_workday(period) == expected_first_workday
    assert generation_date(period) == expected_generation_date


def test_zero_lead_time_uses_first_workday() -> None:
    period = YearMonth(2026, 8)

    assert generation_date(period, lead_workdays=0) == date(2026, 8, 3)


def test_due_check_supports_catch_up_after_generation_date() -> None:
    period = YearMonth(2026, 9)

    assert not generation_is_due(date(2026, 8, 26), period)
    assert generation_is_due(date(2026, 8, 27), period)
    assert generation_is_due(date(2026, 9, 1), period)


def test_password_application_is_due_on_first_calendar_day() -> None:
    period = YearMonth(2026, 9)

    assert not password_application_is_due(date(2026, 8, 31), period)
    assert password_application_is_due(date(2026, 9, 1), period)
    assert password_application_is_due(date(2026, 9, 5), period)


def test_year_month_parse_and_next_cross_year_boundary() -> None:
    assert YearMonth.parse("2026-12").next() == YearMonth(2027, 1)
    assert str(YearMonth(2027, 1)) == "2027-01"


@pytest.mark.parametrize("value", ["2026-1", "2026-13", "26-01", "invalid"])
def test_invalid_period_is_rejected(value: str) -> None:
    with pytest.raises(ScheduleError, match="expected YYYY-MM"):
        YearMonth.parse(value)


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 8, 17, 4, 59, tzinfo=UTC), False),  # Monday 07:59 Tallinn
        (datetime(2026, 8, 17, 5, 0, tzinfo=UTC), True),  # Monday 08:00 Tallinn
        (datetime(2026, 8, 17, 13, 44, tzinfo=UTC), True),  # Monday 16:44 Tallinn
        (datetime(2026, 8, 17, 13, 45, tzinfo=UTC), False),  # Monday 16:45 Tallinn
        (datetime(2026, 8, 21, 12, 44, tzinfo=UTC), True),  # Friday 15:44 Tallinn
        (datetime(2026, 8, 21, 12, 45, tzinfo=UTC), False),  # Friday 15:45 Tallinn
        (datetime(2026, 8, 22, 8, 0, tzinfo=UTC), False),  # Saturday
    ],
)
def test_wlan_desired_state_uses_configured_local_schedule(
    moment: datetime, expected: bool
) -> None:
    settings = load_settings(PROJECT_ROOT / "config.example.yaml")

    assert (
        wlan_should_be_enabled(
            moment,
            timezone=settings.app.timezone,
            schedule=settings.scheduler,
        )
        is expected
    )


def test_wlan_state_rejects_naive_datetime() -> None:
    settings = load_settings(PROJECT_ROOT / "config.example.yaml")

    with pytest.raises(ScheduleError, match="timezone-aware"):
        wlan_should_be_enabled(
            datetime(2026, 8, 17, 8, 0),
            timezone=settings.app.timezone,
            schedule=settings.scheduler,
        )
