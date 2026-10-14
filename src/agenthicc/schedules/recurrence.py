"""Validated one-shot, interval, and numeric five-field cron recurrence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agenthicc.schedules.model import RecurrenceKind


_DURATION = re.compile(r"^(?P<n>[1-9][0-9]*)(?P<u>[smhd])$", re.IGNORECASE)
_MULTIPLIER = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_recurrence(value: str) -> tuple[RecurrenceKind, str]:
    """Parse the CLI/TUI ``kind:spec`` recurrence shorthand."""

    kind_text, separator, spec = value.partition(":")
    if not separator or not spec:
        raise ValueError(
            "recurrence must be once:<ISO-8601>, interval:<duration>, or cron:<5 fields>"
        )
    if kind_text.casefold() == "every":
        kind = RecurrenceKind.INTERVAL
    else:
        try:
            kind = RecurrenceKind(kind_text.casefold())
        except ValueError as exc:
            raise ValueError("recurrence kind must be once, interval, or cron") from exc
    return kind, spec


def validate_timezone(name: str) -> ZoneInfo:
    """Return a valid IANA timezone; fixed UTC offsets are not accepted."""

    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown IANA timezone: {name!r}") from exc


def parse_interval(spec: str) -> int:
    """Parse an integer duration such as ``15m`` or ``2d``."""

    match = _DURATION.fullmatch(spec.strip())
    if match is None:
        raise ValueError("interval must be a positive integer followed by s, m, h, or d")
    seconds = int(match.group("n")) * _MULTIPLIER[match.group("u").lower()]
    if seconds > 2**31 - 1:
        raise ValueError("interval exceeds the supported maximum")
    return seconds


@dataclass(frozen=True, slots=True)
class CronExpression:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_wildcard: bool
    weekday_wildcard: bool

    @classmethod
    def parse(cls, expression: str) -> CronExpression:
        fields = expression.split()
        if len(fields) != 5:
            raise ValueError(
                "cron expression must contain five fields: minute hour day month weekday"
            )
        bounds = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
        parsed: list[frozenset[int]] = []
        wildcard: list[bool] = []
        for raw, (minimum, maximum) in zip(fields, bounds, strict=True):
            parsed.append(_parse_field(raw, minimum, maximum))
            wildcard.append(raw == "*")
        weekdays = frozenset(0 if day == 7 else day for day in parsed[4])
        return cls(parsed[0], parsed[1], parsed[2], parsed[3], weekdays, wildcard[2], wildcard[4])

    def matches_date(self, value: date) -> bool:
        if value.month not in self.months:
            return False
        dom = value.day in self.days
        # Standard cron semantics: when both day fields are restricted, either
        # may match. If only one is restricted, that field controls the date.
        cron_weekday = (value.weekday() + 1) % 7  # cron Sunday=0
        dow = cron_weekday in self.weekdays
        if self.day_wildcard and self.weekday_wildcard:
            return True
        if self.day_wildcard:
            return dow
        if self.weekday_wildcard:
            return dom
        return dom or dow

    def next_after(self, timestamp: float, timezone: ZoneInfo) -> float:
        instant = datetime.fromtimestamp(timestamp, UTC)
        local_now = instant.astimezone(timezone)
        first_date = local_now.date()
        hours = sorted(self.hours)
        minutes = sorted(self.minutes)
        # A leap-day-only schedule may need four years; eight years also covers
        # odd month/day combinations while keeping validation bounded.
        for offset in range(0, 366 * 8 + 2):
            day = first_date + timedelta(days=offset)
            if not self.matches_date(day):
                continue
            for hour in hours:
                for minute in minutes:
                    local_naive = datetime.combine(day, time(hour, minute))
                    if offset == 0 and local_naive <= local_now.replace(tzinfo=None):
                        continue
                    candidate = local_naive.replace(tzinfo=timezone, fold=0)
                    # A spring-forward wall time is nonexistent. Do not
                    # normalize it to a different local minute.
                    roundtrip = candidate.astimezone(UTC).astimezone(timezone)
                    if roundtrip.replace(tzinfo=None) != local_naive:
                        continue
                    candidate_ts = candidate.timestamp()
                    # For ambiguous fall-back minutes, fold=0 is the earlier
                    # instant; fold=1 is intentionally not emitted a second time.
                    if candidate_ts > timestamp:
                        return candidate_ts
        raise ValueError("cron expression has no occurrence within the next eight years")


def _parse_field(raw: str, minimum: int, maximum: int) -> frozenset[int]:
    values: set[int] = set()
    for component in raw.split(","):
        if not component:
            raise ValueError("cron fields cannot contain empty list items")
        base, slash, step_raw = component.partition("/")
        if slash:
            if not step_raw.isdigit() or int(step_raw) <= 0:
                raise ValueError("cron step must be a positive integer")
            step = int(step_raw)
        else:
            step = 1
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_raw, sep, end_raw = base.partition("-")
            if not sep or not start_raw.isdigit() or not end_raw.isdigit():
                raise ValueError(f"invalid cron range: {component!r}")
            start, end = int(start_raw), int(end_raw)
            if start > end:
                raise ValueError(f"descending cron ranges are not supported: {component!r}")
        elif base.isdigit():
            start = int(base)
            end = maximum if slash else start
        else:
            raise ValueError(f"invalid cron field: {component!r}")
        if start < minimum or end > maximum:
            raise ValueError(f"cron value {component!r} is outside {minimum}..{maximum}")
        values.update(range(start, end + 1, step))
    if not values:
        raise ValueError("cron field selects no values")
    return frozenset(values)


def validate_recurrence(kind: RecurrenceKind, spec: str, timezone_name: str) -> None:
    timezone = validate_timezone(timezone_name)
    if kind is RecurrenceKind.INTERVAL:
        parse_interval(spec)
    elif kind is RecurrenceKind.CRON:
        CronExpression.parse(spec)
    elif kind is RecurrenceKind.ONCE:
        _parse_one_shot(spec, timezone)
    else:
        raise ValueError(f"unsupported recurrence kind: {kind}")


def _parse_one_shot(spec: str, timezone: ZoneInfo) -> float:
    try:
        parsed = datetime.fromisoformat(spec.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("one-shot time must be an ISO-8601 date/time") from exc
    if parsed.tzinfo is None:
        candidate = parsed.replace(tzinfo=timezone, fold=0)
        if candidate.astimezone(UTC).astimezone(timezone).replace(tzinfo=None) != parsed:
            raise ValueError("one-shot local time does not exist because of a DST transition")
        return candidate.timestamp()
    return parsed.timestamp()


def first_run_at(kind: RecurrenceKind, spec: str, timezone_name: str, now: float) -> float:
    timezone = validate_timezone(timezone_name)
    if kind is RecurrenceKind.ONCE:
        result = _parse_one_shot(spec, timezone)
        if result <= now:
            raise ValueError("one-shot schedule must be in the future")
        return result
    if kind is RecurrenceKind.INTERVAL:
        return now + parse_interval(spec)
    if kind is RecurrenceKind.CRON:
        return CronExpression.parse(spec).next_after(now, timezone)
    raise ValueError(f"unsupported recurrence kind: {kind}")


def next_run_after(
    kind: RecurrenceKind, spec: str, timezone_name: str, *, previous: float, now: float
) -> float | None:
    """Advance after a due tick, coalescing downtime to the next future tick."""

    if kind is RecurrenceKind.ONCE:
        return None
    timezone = validate_timezone(timezone_name)
    if kind is RecurrenceKind.INTERVAL:
        interval = parse_interval(spec)
        steps = max(1, int((now - previous) // interval) + 1)
        return previous + steps * interval
    if kind is RecurrenceKind.CRON:
        cron = CronExpression.parse(spec)
        return cron.next_after(now, timezone)
    raise ValueError(f"unsupported recurrence kind: {kind}")


def format_timestamp(timestamp: float | None, timezone_name: str) -> str:
    if timestamp is None:
        return "—"
    zone = validate_timezone(timezone_name)
    # Seconds matter for short interval schedules and for the exact
    # confirmation token required before a paused schedule is enabled.
    return datetime.fromtimestamp(timestamp, UTC).astimezone(zone).isoformat(timespec="seconds")
