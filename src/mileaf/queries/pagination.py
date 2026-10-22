"""Keyset pages over trusted SELECTs exposing id and UTC started_at."""

import base64
import binascii
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import asyncpg

from mileaf.database import fetch_rows
from mileaf.errors import APIError, ErrorCode


@dataclass(frozen=True)
class Page:
    rows: list[asyncpg.Record]
    next_cursor: str | None


def bounded_integer(value: int | str, *, default_name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,10}", str(value)):
        raise APIError(400, ErrorCode.INVALID_REQUEST, f"Invalid {default_name}")
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise APIError(400, ErrorCode.INVALID_REQUEST, f"Invalid {default_name}")
    return parsed


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        if len(cursor) > 256 or not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", cursor):
            raise ValueError
        decoded = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        timestamp, identifier = decoded.decode("ascii").split("|")
        started_at = datetime.fromisoformat(timestamp)
        if started_at.tzinfo is None or not re.fullmatch(r"[0-9]{1,19}", identifier):
            raise ValueError
        row_id = int(identifier)
        if not 0 < row_id <= 9223372036854775807:
            raise ValueError
        return started_at.astimezone(UTC), row_id
    except (ValueError, OverflowError, UnicodeError, binascii.Error):
        raise APIError(400, ErrorCode.INVALID_REQUEST, "Invalid cursor") from None


async def fetch_page(
    db: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    *,
    source: str,
    args: tuple[object, ...] = (),
    limit: int | str = 20,
    cursor: str | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
) -> Page:
    """source is developer-owned SQL; all request values are bound parameters.

    Source must expose unique id and aware started_at (timestamp AT TIME ZONE 'UTC').
    Omitted bounds are unbounded; in-progress rows are not excluded.
    """
    size = bounded_integer(limit, default_name="limit", minimum=1, maximum=100)
    key = decode_cursor(cursor) if cursor is not None else (None, None)
    offset = len(args)
    start, end, time, identifier, count = (f"${offset + i}" for i in range(1, 6))
    rows = await fetch_rows(
        db,
        f"WITH source AS ({source}) SELECT * FROM source "
        f"WHERE ({start}::timestamptz IS NULL OR started_at >= {start}) "
        f"AND ({end}::timestamptz IS NULL OR started_at < {end}) "
        f"AND ({time}::timestamptz IS NULL OR "
        f"(started_at,id) < ({time}, {identifier}::bigint)) "
        f"ORDER BY started_at DESC,id DESC LIMIT {count}",
        *args,
        from_,
        to,
        *key,
        size + 1,
    )
    next_cursor = None
    if len(rows) > size:
        last = rows[size - 1]
        content = f"{last['started_at'].astimezone(UTC).isoformat()}|{last['id']}"
        next_cursor = base64.urlsafe_b64encode(content.encode("ascii")).decode("ascii").rstrip("=")
    return Page(rows[:size], next_cursor)
