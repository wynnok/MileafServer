"""One charging match set and query-bound keyset cursors for every charging view."""

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from fastapi import Depends, Query, Request
from pydantic import BaseModel, Field

from mileaf.dependencies import get_now
from mileaf.errors import APIError, ErrorCode
from mileaf.queries.charging import is_dc_aggregate_sql
from mileaf.queries.parameters import list_parameters
from mileaf.queries.time import validate_timezone

TimeBasis = Literal["started_at", "ended_at"]


def invalid(message: str) -> APIError:
    return APIError(400, ErrorCode.INVALID_REQUEST, message)


class ChargingFilters(BaseModel):
    charging_type: Literal["ac", "dc", "unknown"] | None = None
    location_id: str | None = None
    query: str | None = None
    cost_status: Literal["known", "unknown", "zero"] | None = None


class ChargingQueryEcho(BaseModel):
    from_: datetime | None = Field(serialization_alias="from")
    to: datetime | None
    tz: str
    time_basis: TimeBasis
    filters: ChargingFilters


@dataclass(frozen=True)
class ChargingQuery:
    echo: ChargingQueryEcho
    sort: str
    cursor: str | None
    limit: int
    as_of: datetime
    requested_to: datetime | None

    def binding(self, vehicle_id: int, *, view: str) -> str:
        echo = self.echo.model_dump(mode="json", by_alias=True)
        echo["to"] = self.requested_to.isoformat() if self.requested_to else None
        data = [vehicle_id, view, self.sort, echo]
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def charging_parameters(
    request: Request,
    now: Annotated[datetime, Depends(get_now)],
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    tz: str = "UTC",
    time_basis: TimeBasis | None = None,
    charging_type: Literal["ac", "dc", "unknown"] | None = None,
    location_id: str | None = None,
    query: str | None = None,
    cost_status: Literal["known", "unknown", "zero"] | None = None,
    sort: str = "started_desc",
    cursor: str | None = None,
    limit: str = "20",
) -> ChargingQuery:
    bounds = list_parameters(from_, to, limit=limit)
    locations = request.url.path.endswith("charging-locations")
    stats = request.url.path.endswith("/stats")
    allowed = (
        {"energy_desc", "sessions_desc"}
        if locations
        else {"started_desc", "ended_desc", "unit_cost_added_asc", "duration_desc"}
    )
    if locations and "sort" not in request.query_params:
        sort = "energy_desc"
    if sort not in allowed:
        raise invalid("Invalid charging sort")
    text = " ".join(query.split()) if query is not None else ""
    if len(text) > 100:
        raise invalid("Search exceeds 100 characters")
    if location_id is not None:
        try:
            if len(location_id) > 64:
                raise ValueError
            raw = base64.b64decode(location_id + "=" * (-len(location_id) % 4), validate=True)
            identity = raw.decode()
            kind, _, identifier = identity.partition(":")
            if identity != "unlocated" and (
                kind not in {"geofence", "address"}
                or not identifier.isdigit()
                or not 0 < int(identifier) <= 2147483647
            ):
                raise ValueError
            if base64.b64encode(raw).decode().rstrip("=") != location_id:
                raise ValueError
        except (ValueError, UnicodeError, binascii.Error):
            raise invalid("Invalid location_id") from None
    if locations and (bounds.from_ is None or bounds.to is None):
        raise invalid("Location groups require from and to")
    if (
        locations
        and bounds.to is not None
        and bounds.from_ is not None
        and bounds.to - bounds.from_ > timedelta(days=730)
    ):
        raise invalid("Location range exceeds 730 days")
    # No implicit bound for legacy unbounded lists. Explicit future upper bounds are clipped.
    end = min(bounds.to, now) if bounds.to is not None else None
    return ChargingQuery(
        ChargingQueryEcho(
            from_=bounds.from_,
            to=end,
            tz=validate_timezone(tz),
            time_basis=time_basis or ("ended_at" if locations or stats else "started_at"),
            filters=ChargingFilters(
                charging_type=charging_type,
                location_id=location_id,
                query=text or None,
                cost_status=cost_status,
            ),
        ),
        sort,
        cursor,
        bounds.limit,
        now,
        bounds.to,
    )


def location_id_sql() -> str:
    return (
        "rtrim(encode(convert_to(CASE WHEN cp.geofence_id IS NOT NULL THEN "
        "'geofence:' || cp.geofence_id WHEN cp.address_id IS NOT NULL THEN "
        "'address:' || cp.address_id ELSE 'unlocated' END, 'UTF8'),'base64'),'=')"
    )


def match_source(
    query: ChargingQuery, vehicle_id: int, *, completed: bool
) -> tuple[str, list[Any]]:
    """Expose cp fields and shared derived aliases; all external values are bound."""
    time = "start_date" if query.echo.time_basis == "started_at" else "end_date"
    filters = query.echo.filters
    args: list[Any] = [
        vehicle_id,
        query.echo.from_,
        query.echo.to,
        filters.charging_type,
        filters.location_id,
        filters.query,
        filters.cost_status,
    ]
    source = (
        "WITH candidates AS (SELECT cp.* FROM charging_processes cp WHERE cp.car_id=$1 "
        f"AND ($2::timestamptz IS NULL OR cp.{time} AT TIME ZONE 'UTC'>=$2) "
        f"AND ($3::timestamptz IS NULL OR cp.{time} AT TIME ZONE 'UTC'<$3) "
        + ("AND cp.end_date IS NOT NULL " if completed or time == "end_date" else "")
        + "), classifications AS (SELECT sample.charging_process_id, "
        f"{is_dc_aggregate_sql()} AS is_dc FROM charges sample "
        "JOIN candidates cp ON cp.id=sample.charging_process_id "
        "GROUP BY sample.charging_process_id), classified AS ("
        "SELECT cp.*, cp.start_date AT TIME ZONE 'UTC' AS started_at, "
        "cp.end_date AT TIME ZONE 'UTC' AS ended_at, cp.car_id AS vehicle_id, "
        "cp.charge_energy_added AS energy, cp.charge_energy_used AS used, "
        "cp.duration_min AS duration, classes.is_dc, "
        f"{location_id_sql()} AS location_id, "
        "concat_ws(' ',g.name,a.name,a.road,a.house_number,a.city,a.neighbourhood) AS search_text "
        "FROM candidates cp LEFT JOIN classifications classes ON classes.charging_process_id=cp.id "
        "LEFT JOIN geofences g ON g.id=cp.geofence_id LEFT JOIN addresses a ON a.id=cp.address_id) "
        "SELECT * FROM classified WHERE "
        "($4::text IS NULL OR CASE $4 WHEN 'ac' THEN is_dc IS FALSE "
        "WHEN 'dc' THEN is_dc IS TRUE ELSE is_dc IS NULL END) "
        "AND ($5::text IS NULL OR location_id=$5) "
        "AND ($6::text IS NULL OR strpos(lower(search_text),lower($6))>0) "
        "AND ($7::text IS NULL OR CASE $7 WHEN 'known' THEN cost IS NOT NULL "
        "WHEN 'unknown' THEN cost IS NULL ELSE cost=0 END)"
    )
    return source, args


def encode_bound_cursor(binding: str, key: list[Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps([1, binding, key]).encode()).decode().rstrip("=")


def decode_bound_cursor(cursor: str | None, binding: str) -> list[Any] | None:
    if cursor is None:
        return None
    try:
        if len(cursor) > 2048:
            raise ValueError
        decoded = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        version, actual, key = json.loads(decoded)
        if version != 1 or actual != binding or not isinstance(key, list):
            raise ValueError
        return key
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        raise invalid("Invalid cursor or query conditions changed") from None


def session_cursor_key(
    cursor: str | None, binding: str
) -> tuple[Decimal | None, datetime | None, int | None]:
    key = decode_bound_cursor(cursor, binding)
    if key is None:
        return None, None, None
    try:
        value, timestamp, identifier = key
        number = Decimal(value) if value is not None else None
        time = datetime.fromisoformat(timestamp)
        if (number is not None and not number.is_finite()) or time.tzinfo is None:
            raise ValueError
        if type(identifier) is not int or not 0 < identifier <= 2147483647:
            raise ValueError
        return number, time.astimezone(UTC), identifier
    except (ValueError, TypeError, OverflowError, InvalidOperation):
        raise invalid("Invalid cursor key") from None


def charging_location_parameters(
    request: Request,
    now: Annotated[datetime, Depends(get_now)],
    from_: Annotated[str, Query(alias="from")],
    to: str,
    tz: str = "UTC",
    time_basis: TimeBasis = "ended_at",
    charging_type: Literal["ac", "dc", "unknown"] | None = None,
    location_id: str | None = None,
    query: str | None = None,
    cost_status: Literal["known", "unknown", "zero"] | None = None,
    sort: Literal["energy_desc", "sessions_desc"] = "energy_desc",
    cursor: str | None = None,
    limit: str = "20",
) -> ChargingQuery:
    """Declare this endpoint's required bounds and defaults in its OpenAPI contract."""
    return charging_parameters(
        request=request,
        now=now,
        from_=from_,
        to=to,
        tz=tz,
        time_basis=time_basis,
        charging_type=charging_type,
        location_id=location_id,
        query=query,
        cost_status=cost_status,
        sort=sort,
        cursor=cursor,
        limit=limit,
    )
