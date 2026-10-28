"""ST08 closed parking windows: SOC change, observed hours and state coverage.

Read-only over TeslaMate. A bounded database query constructs windows, intersects states,
aggregates the complete set and returns one evidence page. Method ``closed-window-soc-v1``;
conservative exact endpoints, no extrapolation, no kWh or vehicle-behavior inference.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import asyncpg
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from starlette.responses import Response

from mileaf.cache import conditional_response
from mileaf.database import fetch_rows
from mileaf.dependencies import get_now, get_pool
from mileaf.errors import APIError, ErrorCode
from mileaf.queries.charging_query import decode_bound_cursor, encode_bound_cursor
from mileaf.queries.parameters import ResourceRoute, bounded_integer
from mileaf.queries.parking_aggregate import parking_observations_sql
from mileaf.queries.statistics_fields import statistics_feature_supported
from mileaf.queries.time import validate_timezone
from mileaf.vehicles import VehicleIdentity, get_vehicle_or_404

router = APIRouter(prefix="/v1", tags=["parking observations"], route_class=ResourceRoute)

Bucket = Literal["day", "week", "month"]
Status = Literal["available", "no_records", "insufficient_data", "unsupported"]
EventType = Literal["drive", "charging_session"]
Quality = Literal["complete", "partial_state_coverage", "open_window", "excluded"]
ExclusionReason = Literal[
    "same_moment_gap",
    "invalid_time_order",
    "event_overlap",
    "ambiguous_states",
    "missing_soc",
    "missing_odometer",
    "odometer_change",
    "insufficient_duration",
]
SocSource = Literal[
    "drive_start_position",
    "drive_end_position",
    "charging_start_battery_level",
    "charging_end_battery_level",
]
OdometerSource = Literal[
    "drive_start_position",
    "drive_end_position",
    "charging_start_position",
    "charging_end_position",
]
BUCKET_CAPS = {"day": 732, "week": 106, "month": 25}
MIN_DURATION_SECONDS = 3600.0
MAX_ABS_ODOMETER_CHANGE_KM = 1.0


def _invalid(message: str) -> APIError:
    return APIError(400, ErrorCode.INVALID_REQUEST, message)


# --- Contract models -------------------------------------------------------------------------


class ParkingFilters(BaseModel):
    """ST08 takes no evidence filters; echoed as an explicit empty object."""


class ParkingQueryEcho(BaseModel):
    vehicle_id: int
    from_: datetime = Field(serialization_alias="from")
    to: datetime
    tz: str
    bucket: Bucket
    filters: ParkingFilters = Field(default_factory=ParkingFilters)
    time_basis: Literal["window_start"] = "window_start"


class ParkingThresholds(BaseModel):
    min_duration_hours: float = 1.0
    max_abs_odometer_change_km: float = 1.0
    odometer_change_bound: Literal["exclusive"] = "exclusive"
    soc_min_pp: int = 0
    soc_max_pp: int = 100
    endpoint_rule: Literal["exact_event_boundary_position"] = "exact_event_boundary_position"
    state_rule: Literal["closed_segment_intersection_open_unknown"] = (
        "closed_segment_intersection_open_unknown"
    )


class ExcludedByReason(BaseModel):
    same_moment_gap: int = 0
    invalid_time_order: int = 0
    event_overlap: int = 0
    ambiguous_states: int = 0
    missing_soc: int = 0
    missing_odometer: int = 0
    odometer_change: int = 0
    insufficient_duration: int = 0


class ParkingAggregate(BaseModel):
    candidate_windows: int = 0
    valid_windows: int = 0
    excluded_windows: int = 0
    open_windows: int = 0
    observed_hours: float = 0.0
    soc_change_pp: int | None = None
    soc_change_pp_per_24h: float | None = None
    online_hours: float = 0.0
    asleep_hours: float = 0.0
    offline_hours: float = 0.0
    unknown_hours: float = 0.0
    online_share: float | None = None
    asleep_share: float | None = None
    offline_share: float | None = None
    unknown_share: float | None = None
    excluded_by_reason: ExcludedByReason = Field(default_factory=ExcludedByReason)


class ParkingBucket(ParkingAggregate):
    bucket_start: datetime


class BoundaryEvent(BaseModel):
    type: EventType
    id: int


class ParkingEndpoint(BaseModel):
    soc: int | None = None
    odometer_km: float | None = None
    rated_range_km: float | None = None
    soc_source: SocSource | None = None
    odometer_source: OdometerSource | None = None
    position_id: int | None = None
    raw_time: datetime | None = None


class ParkingWindow(BaseModel):
    window_id: str
    start_event: BoundaryEvent
    end_event: BoundaryEvent | None
    window_start: datetime
    window_end: datetime | None
    duration_hours: float | None
    start: ParkingEndpoint
    end: ParkingEndpoint
    soc_change_pp: int | None
    soc_change_pp_per_24h: float | None
    odometer_change_km: float | None
    negative_odometer_change: bool
    online_hours: float
    asleep_hours: float
    offline_hours: float
    unknown_hours: float
    quality: Quality
    exclusion_reason: ExclusionReason | None
    included: bool


class ParkingObservations(BaseModel):
    status: Status
    method: Literal["closed-window-soc-v1"] = "closed-window-soc-v1"
    version: Literal[1] = 1
    validation: Literal["provisional"] = "provisional"
    thresholds: ParkingThresholds = Field(default_factory=ParkingThresholds)
    query: ParkingQueryEcho
    as_of: datetime
    totals: ParkingAggregate
    buckets: list[ParkingBucket]
    items: list[ParkingWindow]
    matched_windows: int
    next_cursor: str | None


# --- Request parameters ------------------------------------------------------------------------


@dataclass(frozen=True)
class ParkingParams:
    from_: datetime
    to: datetime
    requested_to: datetime
    tz: str
    bucket: Bucket
    limit: int
    cursor: str | None
    as_of: datetime
    calendar: tuple[datetime, ...]


def _local_bucket_start(instant: datetime, bucket: Bucket, zone: ZoneInfo) -> datetime:
    local = instant.astimezone(zone)
    if bucket == "day":
        wall = local.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    elif bucket == "week":
        wall = (local - timedelta(days=local.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None
        )
    else:
        wall = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    return wall.replace(tzinfo=zone).astimezone(UTC)


def _next_wall(wall: datetime, bucket: Bucket) -> datetime:
    if bucket == "day":
        return wall + timedelta(days=1)
    if bucket == "week":
        return wall + timedelta(days=7)
    year, month = divmod(wall.month, 12)
    return wall.replace(year=wall.year + year, month=month + 1)


def _calendar(from_: datetime, to: datetime, bucket: Bucket, tz: str) -> tuple[datetime, ...]:
    zone = ZoneInfo(tz)
    starts: list[datetime] = []
    current = _local_bucket_start(from_, bucket, zone)
    while current < to:
        starts.append(current)
        wall = current.astimezone(zone).replace(tzinfo=None)
        current = _next_wall(wall, bucket).replace(tzinfo=zone).astimezone(UTC)
    return tuple(starts)


def parking_parameters(
    now: Annotated[datetime, Depends(get_now)],
    from_: Annotated[str, Query(alias="from")],
    to: Annotated[str, Query()],
    tz: str = "UTC",
    bucket: Bucket = "day",
    cursor: str | None = None,
    limit: str = "50",
) -> ParkingParams:
    try:
        start, end = datetime.fromisoformat(from_), datetime.fromisoformat(to)
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError
        start, end = start.astimezone(UTC), end.astimezone(UTC)
        if not timedelta(0) < end - start <= timedelta(days=730):
            raise ValueError
    except (ValueError, OverflowError):
        raise _invalid("Invalid parking time range") from None
    zone = validate_timezone(tz)
    effective_to = min(end, now)
    if start >= effective_to:
        raise _invalid("Parking range is empty after clipping to as_of")
    size = bounded_integer(limit, default_name="limit", minimum=1, maximum=100)
    calendar = _calendar(start, effective_to, bucket, zone)
    if len(calendar) > BUCKET_CAPS[bucket]:
        raise _invalid(f"Bucket count exceeds {BUCKET_CAPS[bucket]}; narrow the range")
    return ParkingParams(start, effective_to, end, zone, bucket, size, cursor, now, calendar)


# --- Route --------------------------------------------------------------------------------------


def _binding(vehicle_id: int, params: ParkingParams) -> str:
    data = [
        vehicle_id,
        "parking_observations",
        params.from_.isoformat(),
        params.requested_to.isoformat(),
        params.tz,
        params.bucket,
    ]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _decode_key(
    cursor: str | None, binding: str
) -> tuple[datetime, tuple[str, int, str, int]] | None:
    key = decode_bound_cursor(cursor, binding)
    if key is None:
        return None
    try:
        raw_start, start_type, start_id, end_type, end_id = key
        window_start = datetime.fromisoformat(raw_start)
        if window_start.tzinfo is None:
            raise ValueError
        parsed = (
            window_start.astimezone(UTC),
            (str(start_type), int(start_id), str(end_type), int(end_id)),
        )
        if not isinstance(start_type, str) or not isinstance(end_type, str):
            raise ValueError
        return parsed
    except (ValueError, TypeError) as exc:
        raise _invalid("Invalid parking cursor key") from exc


@router.get("/vehicles/{vehicle_id}/parking-observations", response_model=ParkingObservations)
async def parking_observations(
    request: Request,
    vehicle: Annotated[VehicleIdentity, Depends(get_vehicle_or_404)],
    params: Annotated[ParkingParams, Depends(parking_parameters)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> Response:
    echo = ParkingQueryEcho(
        vehicle_id=vehicle.id,
        from_=params.from_,
        to=params.to,
        tz=params.tz,
        bucket=params.bucket,
    )

    def respond(
        status: Status,
        totals: ParkingAggregate,
        buckets: list[ParkingBucket],
        items: list[ParkingWindow],
        matched: int,
        next_cursor: str | None,
    ) -> Response:
        result = ParkingObservations(
            status=status,
            query=echo,
            as_of=params.as_of,
            totals=totals,
            buckets=buckets,
            items=items,
            matched_windows=matched,
            next_cursor=next_cursor,
        )
        body = result.model_dump_json(by_alias=True).encode()
        return conditional_response(
            request,
            body,
            ttl=30,
            validator_body=result.model_dump_json(by_alias=True, exclude={"as_of"}).encode(),
        )

    compatibility = request.app.state.compatibility
    if not statistics_feature_supported(compatibility.statistics_columns, "parking_observations"):
        empty_buckets = [ParkingBucket(bucket_start=start) for start in params.calendar]
        return respond("unsupported", ParkingAggregate(), empty_buckets, [], 0, None)

    key = _decode_key(params.cursor, _binding(vehicle.id, params))
    cursor_start, tie = key if key else (None, ("", 0, "", 0))
    args = (
        vehicle.id,
        params.as_of,
        params.from_,
        params.to,
        params.tz,
        params.bucket,
        params.limit + 1,
        cursor_start,
        *tie,
    )
    async with pool.acquire() as db:
        row = (await fetch_rows(db, parking_observations_sql(), *args))[0]
    totals = ParkingAggregate(**json.loads(row["totals"]))
    grouped = {
        datetime.fromisoformat(item["bucket_start"]): item["result"]
        for item in json.loads(row["buckets"])
    }
    buckets = [
        ParkingBucket(bucket_start=start, **grouped.get(start, {})) for start in params.calendar
    ]
    items = [ParkingWindow(**item) for item in json.loads(row["items"])]
    has_more = len(items) > params.limit
    items = items[: params.limit]
    next_cursor = None
    if has_more:
        last = items[-1]
        next_cursor = encode_bound_cursor(
            _binding(vehicle.id, params),
            [
                last.window_start.isoformat(),
                last.start_event.type,
                last.start_event.id,
                last.end_event.type if last.end_event else "",
                last.end_event.id if last.end_event else 0,
            ],
        )
    return respond(
        "available" if totals.valid_windows else "insufficient_data",
        totals,
        buckets,
        items,
        row["matched"],
        next_cursor,
    )
