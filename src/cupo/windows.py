"""Window arithmetic.

A limit is always scoped to a window ("500 messages per month"). Cupo stores
counters keyed by the *start* of the window the request falls into, which means
a window rollover requires no cron job: the next window has no row yet, so its
counter starts at zero by construction.

All windows are computed in UTC. This is deliberate: billing periods that follow
a customer's local timezone are a v0.2 concern, and getting them wrong silently
is worse than not offering them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

Window = str

VALID_WINDOWS = ("minute", "hour", "day", "month")


class InvalidWindow(ValueError):
    pass


def window_start(window: Window, now: datetime | None = None) -> datetime:
    """Return the UTC start of the window that `now` falls into."""
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        raise InvalidWindow("now must be timezone-aware")
    else:
        now = now.astimezone(timezone.utc)

    if window == "minute":
        return now.replace(second=0, microsecond=0)
    if window == "hour":
        return now.replace(minute=0, second=0, microsecond=0)
    if window == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    raise InvalidWindow(
        f"unknown window {window!r}; expected one of {', '.join(VALID_WINDOWS)}"
    )


def window_end(window: Window, now: datetime | None = None) -> datetime:
    """Return the UTC instant at which the current window resets."""
    start = window_start(window, now)

    if window == "minute":
        return start + timedelta(minutes=1)
    if window == "hour":
        return start + timedelta(hours=1)
    if window == "day":
        return start + timedelta(days=1)
    if window == "month":
        if start.month == 12:
            return start.replace(year=start.year + 1, month=1)
        return start.replace(month=start.month + 1)

    raise InvalidWindow(f"unknown window {window!r}")
