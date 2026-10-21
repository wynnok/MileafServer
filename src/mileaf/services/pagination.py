from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute


class InvalidCursorError(ValueError):
    pass


def encode_cursor(date: datetime, item_id: uuid.UUID) -> str:
    raw = json.dumps({"d": date.isoformat(), "i": str(item_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return datetime.fromisoformat(payload["d"]), uuid.UUID(payload["i"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise InvalidCursorError("invalid cursor") from exc


async def paginate_desc[T](
    session: AsyncSession,
    stmt: Select[T],
    *,
    date_column: InstrumentedAttribute[Any],
    id_column: InstrumentedAttribute[Any],
    limit: int,
    cursor: str | None,
) -> tuple[list[T], str | None]:
    """Apply keyset pagination ordered by (date_column, id_column) descending."""
    if cursor:
        cursor_date, cursor_id = decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                date_column < cursor_date,
                and_(date_column == cursor_date, id_column < cursor_id),
            )
        )

    stmt = stmt.order_by(date_column.desc(), id_column.desc()).limit(limit + 1)
    rows = list(await session.scalars(stmt))
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor(
            getattr(rows[-1], date_column.key),
            getattr(rows[-1], id_column.key),
        )
    return rows, next_cursor
