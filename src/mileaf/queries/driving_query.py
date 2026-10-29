"""One driving match set for evidence lists and full-query analysis."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from typing import Annotated, Any, Literal

from fastapi import Depends, Query, Request
from pydantic import BaseModel, Field, field_serializer

from mileaf.dependencies import get_now
from mileaf.errors import APIError, ErrorCode
from mileaf.queries.charging_query import decode_bound_cursor, encode_bound_cursor
from mileaf.queries.energy import energy_kwh_sql
from mileaf.queries.pagination import decode_cursor
from mileaf.queries.parameters import list_parameters
from mileaf.queries.time import validate_timezone

DRIVING_MATCH_METHOD: Literal["driving-match-v1"] = "driving-match-v1"

# The match rules read only columns the published 0.1/0.2 startup probe already
# requires (drives.distance, drives.outside_temp_avg and the energy sources), so
# this slice adds no optional column and cannot degrade to unsupported.


class DrivingFilters(BaseModel):
    distance_min_km: float | None = None
    distance_max_km: float | None = None
    outside_temp_min_c: float | None = None
    outside_temp_max_c: float | None = None
    energy_status: Literal["known", "unknown"] | None = None
    completed_only: bool = False
    destination_location_id: str | None = None


class DrivingQueryEcho(BaseModel):
    vehicle_id: int
    from_: datetime | None = Field(serialization_alias="from")
    to: datetime | None
    tz: str
    bucket: None = None
    time_basis: Literal["started_at"] = "started_at"
    filters: DrivingFilters

    @field_serializer("filters")
    def serialize_filters(self, value: DrivingFilters) -> dict[str, Any]:
        return value.model_dump(exclude_none=True, exclude_defaults=True)


@dataclass(frozen=True)
class DrivingQuery:
    echo: DrivingQueryEcho
    cursor: str | None
    limit: int
    as_of: datetime
    requested_to: datetime | None
    legacy_cursor_allowed: bool

    def binding(self) -> str:
        echo = self.echo.model_dump(mode="json", by_alias=True)
        echo["to"] = self.requested_to.isoformat() if self.requested_to else None
        return hashlib.sha256(
            json.dumps([DRIVING_MATCH_METHOD, "started_desc", echo], sort_keys=True).encode()
        ).hexdigest()


def driving_filters(
    distance_min_km: float | None = None,
    distance_max_km: float | None = None,
    outside_temp_min_c: float | None = None,
    outside_temp_max_c: float | None = None,
    energy_status: Literal["known", "unknown"] | None = None,
    completed_only: bool = False,
    destination_location_id: str | None = None,
) -> DrivingFilters:
    """One validation for every route that accepts the shared driving filters."""
    values = (distance_min_km, distance_max_km, outside_temp_min_c, outside_temp_max_c)
    if any(value is not None and not isfinite(value) for value in values):
        raise APIError(400, ErrorCode.INVALID_REQUEST, "Driving filter bounds must be finite")
    for lower, upper in (
        (distance_min_km, distance_max_km),
        (outside_temp_min_c, outside_temp_max_c),
    ):
        if lower is not None and upper is not None and lower >= upper:
            raise APIError(400, ErrorCode.INVALID_REQUEST, "Invalid driving filter range")
    return DrivingFilters(
        distance_min_km=distance_min_km,
        distance_max_km=distance_max_km,
        outside_temp_min_c=outside_temp_min_c,
        outside_temp_max_c=outside_temp_max_c,
        energy_status=energy_status,
        completed_only=completed_only,
        destination_location_id=destination_location_id,
    )


def driving_parameters(
    request: Request,
    now: Annotated[datetime, Depends(get_now)],
    vehicle_id: int,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    tz: str = "UTC",
    distance_min_km: float | None = None,
    distance_max_km: float | None = None,
    outside_temp_min_c: float | None = None,
    outside_temp_max_c: float | None = None,
    energy_status: Literal["known", "unknown"] | None = None,
    completed_only: bool = False,
    destination_location_id: str | None = None,
    cursor: str | None = None,
    limit: str = "20",
) -> DrivingQuery:
    bounds = list_parameters(from_, to, limit=limit)
    end = min(bounds.to, now) if bounds.to is not None else None
    if bounds.from_ is not None and end is not None and bounds.from_ >= end:
        raise APIError(400, ErrorCode.INVALID_REQUEST, "Invalid effective driving time range")
    filters = driving_filters(
        distance_min_km=distance_min_km,
        distance_max_km=distance_max_km,
        outside_temp_min_c=outside_temp_min_c,
        outside_temp_max_c=outside_temp_max_c,
        energy_status=energy_status,
        completed_only=completed_only,
        destination_location_id=destination_location_id,
    )
    return DrivingQuery(
        DrivingQueryEcho(
            vehicle_id=vehicle_id,
            from_=bounds.from_,
            to=end,
            tz=validate_timezone(tz),
            filters=filters,
        ),
        cursor,
        bounds.limit,
        now,
        bounds.to,
        not bool(set(request.query_params) & (set(DrivingFilters.model_fields) | {"tz"})),
    )


def match_source(
    echo: DrivingQueryEcho, vehicle_id: int, *, source: str | None = None
) -> tuple[str, list[Any]]:
    """Trusted source exposes drives columns plus started_at and energy_kwh aliases.

    The default projection is sufficient for analysis without joining position points;
    evidence may supply its existing detail projection to reuse the same match rules.
    Grouping routes build their own echo, so one implementation serves every caller.
    """
    if source is None:
        source = (
            "SELECT d.*, d.start_date AT TIME ZONE 'UTC' AS started_at, "
            "d.end_date AT TIME ZONE 'UTC' AS ended_at, "
            f"{energy_kwh_sql()} AS energy_kwh, s.preferred_range::text AS range_kind "
            "FROM drives d JOIN cars c ON c.id=d.car_id CROSS JOIN settings s"
        )
    filters = echo.filters
    args: list[Any] = [
        vehicle_id,
        echo.from_,
        echo.to,
        filters.distance_min_km,
        filters.distance_max_km,
        filters.outside_temp_min_c,
        filters.outside_temp_max_c,
        filters.energy_status,
        filters.completed_only,
        filters.destination_location_id,
    ]
    matched = (
        f"SELECT * FROM ({source}) driving WHERE car_id=$1 "
        "AND ($2::timestamptz IS NULL OR started_at >= $2) "
        "AND ($3::timestamptz IS NULL OR started_at < $3) "
        "AND ($4::float8 IS NULL OR (distance > '-Infinity'::float8 "
        "AND distance < 'Infinity'::float8 AND distance >= $4)) "
        "AND ($5::float8 IS NULL OR (distance > '-Infinity'::float8 "
        "AND distance < 'Infinity'::float8 AND distance < $5)) "
        "AND ($6::numeric IS NULL OR (outside_temp_avg > '-Infinity'::numeric "
        "AND outside_temp_avg < 'Infinity'::numeric AND outside_temp_avg >= $6)) "
        "AND ($7::numeric IS NULL OR (outside_temp_avg > '-Infinity'::numeric "
        "AND outside_temp_avg < 'Infinity'::numeric AND outside_temp_avg < $7)) "
        "AND ($8::text IS NULL OR CASE $8 WHEN 'known' "
        "THEN ended_at IS NOT NULL AND energy_kwh IS NOT NULL "
        "ELSE ended_at IS NULL OR energy_kwh IS NULL END) "
        "AND (NOT $9::boolean OR ended_at IS NOT NULL) "
        "AND ($10::text IS NULL OR rtrim(encode(convert_to(CASE "
        "WHEN end_geofence_id IS NOT NULL THEN 'geofence:'||end_geofence_id "
        "WHEN end_address_id IS NOT NULL THEN 'address:'||end_address_id "
        "ELSE 'unlocated' END,'UTF8'),'base64'),'=')=$10)"
    )
    return matched, args


def drive_cursor_key(query: DrivingQuery) -> tuple[datetime, int] | None:
    if query.cursor is None:
        return None
    try:
        key = decode_bound_cursor(query.cursor, query.binding())
    except APIError:
        if not query.legacy_cursor_allowed:
            raise
        return decode_cursor(query.cursor)
    try:
        assert key is not None
        timestamp, identifier = key
        time = datetime.fromisoformat(timestamp)
        if time.tzinfo is None or type(identifier) is not int or not 0 < identifier <= 2147483647:
            raise ValueError
        return time.astimezone(UTC), identifier
    except (ValueError, TypeError, OverflowError):
        raise APIError(400, ErrorCode.INVALID_REQUEST, "Invalid driving cursor key") from None


def encode_drive_cursor(query: DrivingQuery, timestamp: datetime, identifier: int) -> str:
    return encode_bound_cursor(query.binding(), [timestamp.astimezone(UTC).isoformat(), identifier])
