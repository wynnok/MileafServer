"""Calendar windows over UTC instants; PostgreSQL owns timezone conversion."""

from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mileaf.errors import APIError, ErrorCode

Bucket = Literal["day", "week", "month", "year"]


def validate_timezone(tz: str = "UTC") -> str:
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise APIError(400, ErrorCode.INVALID_REQUEST, "Invalid IANA timezone") from None
    return tz


def bucket_sql(bucket: Bucket, *, timestamp: str, tz: str) -> str:
    """timestamp must be a timestamptz expression (source timestamp AT TIME ZONE 'UTC')."""
    if bucket not in {"day", "week", "month", "year"}:
        raise ValueError("Unsupported calendar bucket")
    return f"(date_trunc('{bucket}', ({timestamp}) AT TIME ZONE ({tz})) AT TIME ZONE ({tz}))"


def window_sql(period: Literal["today", "month"], *, now: str, tz: str) -> tuple[str, str]:
    """Return UTC [start, end); bind an aware now and validated timezone as SQL parameters."""
    if period not in {"today", "month"}:
        raise ValueError("Unsupported calendar window")
    bucket = "day" if period == "today" else "month"
    local_start = f"date_trunc('{bucket}', ({now})::timestamptz AT TIME ZONE ({tz}))"
    return (
        f"({local_start} AT TIME ZONE ({tz}))",
        f"(({local_start} + interval '1 {bucket}') AT TIME ZONE ({tz}))",
    )
