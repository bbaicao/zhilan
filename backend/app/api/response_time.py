"""Annotate naive database timestamps at the response boundary."""
from datetime import datetime, timezone

from app.agent.audit import db_clock_is_utc


def response_time(value: datetime | None, *, utc: bool | None = None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    if db_clock_is_utc() if utc is None else utc:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone()
