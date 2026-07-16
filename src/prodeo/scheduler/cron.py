"""Standard 5-field cron expressions, implemented in-process.

Supports ``minute hour day-of-month month day-of-week`` with ``*``, lists,
ranges, steps, month/weekday names, and the common ``@hourly``-style aliases.
Day-of-month and day-of-week combine with OR when both are restricted, per
Vixie cron. A deliberately small dependency-free implementation: the scheduler
needs "when does this fire next," nothing more.

All computation happens at minute resolution in the schedule's timezone.
During a DST gap the Python ``zoneinfo`` fold rules apply (a nonexistent local
time resolves to the post-transition instant), which is acceptable drift for
agent launches.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

_ALIASES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

_MONTH_NAMES = {
    name: i + 1
    for i, name in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    )
}
_DOW_NAMES = {name: i for i, name in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}

#: How far ahead ``next_fire`` searches before declaring the expression
#: unsatisfiable (e.g. ``0 0 30 2 *``). Four years covers leap days.
_SEARCH_LIMIT_DAYS = 366 * 4


@dataclass(frozen=True)
class CronSpec:
    """A parsed cron expression (sets of matching values per field)."""

    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]  # 0 = Sunday
    dom_restricted: bool
    dow_restricted: bool

    def matches_day(self, day: datetime) -> bool:
        """Whether the date part (month/dom/dow) matches, per Vixie OR rule."""
        if day.month not in self.months:
            return False
        dom_ok = day.day in self.days_of_month
        # datetime.weekday(): Monday=0 .. Sunday=6; cron: Sunday=0 .. Saturday=6
        dow_ok = (day.weekday() + 1) % 7 in self.days_of_week
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok


def parse_cron(expression: str) -> CronSpec:
    """Parse a cron expression; raises ``ValueError`` with a usable message."""
    expr = expression.strip().lower()
    expr = _ALIASES.get(expr, expr)
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(
            f"cron expression must have 5 fields (minute hour day month weekday), "
            f"got {len(fields)}: {expression!r}"
        )
    minutes = _parse_field(fields[0], 0, 59, {})
    hours = _parse_field(fields[1], 0, 23, {})
    dom = _parse_field(fields[2], 1, 31, {})
    months = _parse_field(fields[3], 1, 12, _MONTH_NAMES)
    dow = _parse_field(fields[4], 0, 7, _DOW_NAMES)
    if 7 in dow:  # both 0 and 7 mean Sunday
        dow = (dow | {0}) - {7}
    return CronSpec(
        expression=expression.strip(),
        minutes=frozenset(minutes),
        hours=frozenset(hours),
        days_of_month=frozenset(dom),
        months=frozenset(months),
        days_of_week=frozenset(dow),
        dom_restricted=fields[2] != "*",
        dow_restricted=fields[4] != "*",
    )


def next_fire(spec: CronSpec, after: datetime, tz: tzinfo) -> datetime | None:
    """The first instant strictly after ``after`` matching ``spec``, in ``tz``.

    Returns an aware datetime (in ``tz``), or ``None`` when nothing matches
    within the search horizon.
    """
    local = after.astimezone(tz)
    # Start at the next whole minute.
    candidate = (local + timedelta(minutes=1)).replace(second=0, microsecond=0)
    hours = sorted(spec.hours)
    minutes = sorted(spec.minutes)
    day = candidate.replace(hour=0, minute=0)
    for offset in range(_SEARCH_LIMIT_DAYS):
        if offset > 0:
            day = (day + timedelta(days=1)).replace(hour=0, minute=0)
        if not spec.matches_day(day):
            continue
        first_day = day.date() == candidate.date()
        for hour in hours:
            if first_day and hour < candidate.hour:
                continue
            for minute in minutes:
                if first_day and hour == candidate.hour and minute < candidate.minute:
                    continue
                fire = day.replace(hour=hour, minute=minute)
                if fire > local:
                    return fire
    return None


def _parse_field(field: str, lo: int, hi: int, names: dict[str, int]) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        values |= _parse_part(part, lo, hi, names)
    if not values:
        raise ValueError(f"cron field {field!r} matches nothing")
    return values


def _parse_part(part: str, lo: int, hi: int, names: dict[str, int]) -> set[int]:
    if not part:
        raise ValueError("empty cron field")
    body, _, step_s = part.partition("/")
    step = 1
    if step_s:
        step = _int(step_s, part)
        if step < 1:
            raise ValueError(f"cron step must be >= 1 in {part!r}")
    if body == "*":
        start, end = lo, hi
    elif "-" in body:
        start_s, _, end_s = body.partition("-")
        start = _value(start_s, names, part)
        end = _value(end_s, names, part)
        if start > end:
            raise ValueError(f"inverted cron range in {part!r}")
    else:
        value = _value(body, names, part)
        # A bare value with a step ("3/5") behaves like "3-hi/5", per cron.
        start, end = (value, hi) if step_s else (value, value)
    if start < lo or end > hi:
        raise ValueError(f"cron value out of range [{lo}, {hi}] in {part!r}")
    return set(range(start, end + 1, step))


def _value(token: str, names: dict[str, int], context: str) -> int:
    if token in names:
        return names[token]
    return _int(token, context)


def _int(token: str, context: str) -> int:
    try:
        return int(token)
    except ValueError:
        raise ValueError(f"invalid cron token {token!r} in {context!r}") from None
