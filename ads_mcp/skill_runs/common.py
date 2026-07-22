"""Shared deterministic helpers for hosted GMA specialist runs."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def resolve_analysis_window(
    *,
    analysis_start: str | None,
    analysis_end: str | None,
    account_time_zone: str,
    now: datetime,
) -> tuple[date, date]:
    """Return an inclusive account-timezone window.

    Omitting both dates means the last 30 complete account days. Supplying only
    one date is rejected so a caller can never silently widen the user's scope.
    """

    if not analysis_start and not analysis_end:
        try:
            zone = ZoneInfo(account_time_zone)
        except ZoneInfoNotFoundError:
            zone = timezone.utc
        end = now.astimezone(zone).date() - timedelta(days=1)
        return end - timedelta(days=29), end
    if not analysis_start or not analysis_end:
        raise ValueError(
            "Provide both analysis_start and analysis_end, or neither for last 30 days"
        )
    try:
        start = date.fromisoformat(analysis_start)
        end = date.fromisoformat(analysis_end)
    except ValueError as error:
        raise ValueError("Dates must be YYYY-MM-DD") from error
    if end < start:
        raise ValueError("Analysis end cannot be before start")
    return start, end


def trailing_complete_days(
    *, account_time_zone: str, now: datetime, days: int
) -> tuple[date, date]:
    """Return a fixed trailing complete-day window in the account timezone."""

    if days < 1:
        raise ValueError("days must be positive")
    try:
        zone = ZoneInfo(account_time_zone)
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    end = now.astimezone(zone).date() - timedelta(days=1)
    return end - timedelta(days=days - 1), end
