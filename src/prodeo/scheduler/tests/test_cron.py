"""Cron parser and next-fire computation tests."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from prodeo.scheduler.cron import next_fire, parse_cron


def _next(expr: str, after: str, tz: str = "UTC") -> str | None:
    zone = ZoneInfo(tz)
    result = next_fire(parse_cron(expr), datetime.fromisoformat(after), zone)
    return result.isoformat() if result is not None else None


class TestParsing:
    def test_wildcards(self) -> None:
        spec = parse_cron("* * * * *")
        assert spec.minutes == frozenset(range(60))
        assert spec.hours == frozenset(range(24))
        assert not spec.dom_restricted
        assert not spec.dow_restricted

    def test_lists_ranges_steps(self) -> None:
        spec = parse_cron("0,30 9-17 */2 1,6 mon-fri")
        assert spec.minutes == frozenset({0, 30})
        assert spec.hours == frozenset(range(9, 18))
        assert spec.days_of_month == frozenset(range(1, 32, 2))
        assert spec.months == frozenset({1, 6})
        assert spec.days_of_week == frozenset({1, 2, 3, 4, 5})

    def test_range_with_step(self) -> None:
        assert parse_cron("10-30/10 * * * *").minutes == frozenset({10, 20, 30})

    def test_month_and_day_names(self) -> None:
        spec = parse_cron("0 0 * jan,jul sun")
        assert spec.months == frozenset({1, 7})
        assert spec.days_of_week == frozenset({0})

    def test_seven_means_sunday(self) -> None:
        assert parse_cron("0 0 * * 7").days_of_week == frozenset({0})

    def test_aliases(self) -> None:
        assert parse_cron("@daily").hours == frozenset({0})
        assert parse_cron("@hourly").minutes == frozenset({0})
        assert parse_cron("@weekly").days_of_week == frozenset({0})

    @pytest.mark.parametrize(
        "bad",
        [
            "* * * *",  # 4 fields
            "* * * * * *",  # 6 fields
            "60 * * * *",  # out of range
            "* 24 * * *",
            "* * 0 * *",
            "* * * 13 *",
            "* * * * 8",
            "5-1 * * * *",  # inverted range
            "*/0 * * * *",  # zero step
            "a * * * *",  # not a number
            "",  # empty
        ],
    )
    def test_invalid_expressions_raise(self, bad: str) -> None:
        with pytest.raises(ValueError, match="cron"):
            parse_cron(bad)


class TestNextFire:
    def test_every_minute(self) -> None:
        assert _next("* * * * *", "2026-07-16T12:00:30+00:00") == "2026-07-16T12:01:00+00:00"

    def test_strictly_after(self) -> None:
        # Exactly on a match: the next fire is the following slot.
        assert _next("0 * * * *", "2026-07-16T12:00:00+00:00") == "2026-07-16T13:00:00+00:00"

    def test_daily_rolls_to_next_day(self) -> None:
        assert _next("30 8 * * *", "2026-07-16T09:00:00+00:00") == "2026-07-17T08:30:00+00:00"

    def test_weekday_match(self) -> None:
        # 2026-07-16 is a Thursday; next Monday is 2026-07-20.
        assert _next("0 9 * * mon", "2026-07-16T00:00:00+00:00") == "2026-07-20T09:00:00+00:00"

    def test_dom_dow_or_semantics(self) -> None:
        # Both restricted: fires on the 15th OR on Fridays (Vixie cron).
        # After Thu 2026-07-16, the next Friday (July 17) wins over Aug 15.
        assert _next("0 0 15 * fri", "2026-07-16T01:00:00+00:00") == "2026-07-17T00:00:00+00:00"

    def test_dom_only_when_dow_wild(self) -> None:
        assert _next("0 0 15 * *", "2026-07-16T01:00:00+00:00") == "2026-08-15T00:00:00+00:00"

    def test_month_boundary(self) -> None:
        assert _next("0 0 1 * *", "2026-07-16T00:00:00+00:00") == "2026-08-01T00:00:00+00:00"

    def test_leap_day(self) -> None:
        assert _next("0 12 29 2 *", "2026-07-16T00:00:00+00:00") == "2028-02-29T12:00:00+00:00"

    def test_unsatisfiable_returns_none(self) -> None:
        assert _next("0 0 30 2 *", "2026-07-16T00:00:00+00:00") is None

    def test_timezone_local_wall_clock(self) -> None:
        # 08:00 in New York on 2026-07-16 (EDT, UTC-4) is 12:00 UTC.
        result = next_fire(
            parse_cron("0 8 * * *"),
            datetime(2026, 7, 16, 0, 0, tzinfo=UTC),
            ZoneInfo("America/New_York"),
        )
        assert result is not None
        assert result.astimezone(UTC) == datetime(2026, 7, 16, 12, 0, tzinfo=UTC)

    def test_expression_preserved(self) -> None:
        assert parse_cron(" @daily ").expression == "@daily"
