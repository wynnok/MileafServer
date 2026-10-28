"""ST08 closed parking windows: SOC change, observed hours and state coverage.

Read-only over TeslaMate. Two bounded business queries (events+endpoints, states) feed a
server-side construction of every candidate window across the complete match set, so totals and
buckets never depend on page size or cursor. Method ``closed-window-soc-v1``; conservative exact
endpoints only, no extrapolation, no kWh derivation, no sentinel/HVAC/fault inference.
"""

import hashlib
import json
import math
from bisect import bisect_left
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
from mileaf.queries.parking import parking_events_sql, parking_states_sql
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
# Public fixed priority: the first triggered reason is the single counted exclusion.
_REASON_ORDER: tuple[ExclusionReason, ...] = (
    "same_moment_gap",
    "invalid_time_order",
    "event_overlap",
    "ambiguous_states",
    "missing_soc",
    "missing_odometer",
    "odometer_change",
    "insufficient_duration",
)
BUCKET_CAPS = {"day": 732, "week": 106, "month": 25}
MIN_DURATION_SECONDS = 3600.0
MAX_ABS_ODOMETER_CHANGE_KM = 1.0


def _invalid(message: str) -> APIError:
    return APIError(400, ErrorCode.INVALID_REQUEST, message)


def _float(value: Decimal | float | None) -> float | None:
    return None if value is None else float(value)


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


# --- Server-side window construction over the complete match set -------------------------------


@dataclass(frozen=True)
class _Event:
    type: str
    id: int
    start: datetime
    end: datetime | None
    start_soc: int | None
    start_odometer: float | None
    start_rated: float | None
    start_position_id: int | None
    end_soc: int | None
    end_odometer: float | None
    end_rated: float | None
    end_position_id: int | None


@dataclass(frozen=True)
class _State:
    value: str
    start: datetime
    end: datetime | None


def _parse_events(rows: list[asyncpg.Record]) -> list[_Event]:
    events = [
        _Event(
            type=row["event_type"],
            id=row["event_id"],
            start=row["event_start"],
            end=row["event_end"],
            start_soc=row["start_soc"],
            start_odometer=_float(row["start_odometer"]),
            start_rated=_float(row["start_rated_range"]),
            start_position_id=row["start_position_id"],
            end_soc=row["end_soc"],
            end_odometer=_float(row["end_odometer"]),
            end_rated=_float(row["end_rated_range"]),
            end_position_id=row["end_position_id"],
        )
        for row in rows
    ]
    events.sort(key=lambda event: (event.start, event.type, event.id))
    return events


def _parse_states(rows: list[asyncpg.Record]) -> list[_State]:
    states = [_State(value=row["state"], start=row["start_at"], end=row["end_at"]) for row in rows]
    states.sort(key=lambda state: (state.start, state.end is None))
    return states


def _soc_ok(soc: int | None) -> bool:
    return soc is not None and 0 <= soc <= 100


def _endpoint(event: _Event, side: Literal["start", "end"], boundary: datetime) -> ParkingEndpoint:
    """Build the window endpoint contributed by an event boundary (prev end / next start)."""
    drive = event.type == "drive"
    if side == "end":  # window_start endpoint comes from the previous event's END.
        soc, odometer, rated, position = (
            event.end_soc,
            event.end_odometer,
            event.end_rated,
            event.end_position_id,
        )
        soc_source: SocSource = "drive_end_position" if drive else "charging_end_battery_level"
        odo_source: OdometerSource = "drive_end_position" if drive else "charging_end_position"
    else:  # window_end endpoint comes from the next event's START.
        soc, odometer, rated, position = (
            event.start_soc,
            event.start_odometer,
            event.start_rated,
            event.start_position_id,
        )
        soc_source = "drive_start_position" if drive else "charging_start_battery_level"
        odo_source = "drive_start_position" if drive else "charging_start_position"
    return ParkingEndpoint(
        soc=soc,
        odometer_km=odometer,
        rated_range_km=rated,
        soc_source=soc_source if soc is not None else None,
        odometer_source=odo_source if odometer is not None else None,
        position_id=position,
        raw_time=boundary,
    )


def _state_hours(
    states: list[_State], starts: list[datetime], ws: datetime, we: datetime
) -> tuple[float, float, float, bool]:
    """Clip closed segments to the window; open segments and gaps stay unknown.

    Returns (online, asleep, offline, ambiguous). Ambiguous is true when two clipped segments
    overlap with positive length.
    """
    if we <= ws:
        return 0.0, 0.0, 0.0, False
    lo_idx = 0  # Earlier long segments may overlap the window past later short segments.
    hi_idx = bisect_left(starts, we)
    hours = {"online": 0.0, "asleep": 0.0, "offline": 0.0}
    spans: list[tuple[datetime, datetime]] = []
    for state in states[lo_idx:hi_idx]:
        seg_lo = max(state.start, ws)
        seg_hi = min(state.end, we) if state.end is not None else we
        if seg_hi <= seg_lo:
            continue
        spans.append((seg_lo, seg_hi))
        if state.end is not None and state.value in hours:
            hours[state.value] += (seg_hi - seg_lo).total_seconds() / 3600.0
    spans.sort()
    ambiguous = False
    max_hi = spans[0][1] if spans else None
    for seg_lo, seg_hi in spans[1:]:
        if max_hi is not None and seg_lo < max_hi:
            ambiguous = True
            break
        max_hi = seg_hi if max_hi is None else max(max_hi, seg_hi)
    return hours["online"], hours["asleep"], hours["offline"], ambiguous


@dataclass(frozen=True)
class _Built:
    item: ParkingWindow
    bucket_start: datetime
    tiebreak: tuple[str, int, str, int]


def _window_id(start: BoundaryEvent, end: BoundaryEvent | None) -> str:
    tail = f"{end.type}:{end.id}" if end is not None else "open"
    return f"{start.type}:{start.id}~{tail}"


def _build_windows(
    events: list[_Event],
    states: list[_State],
    params: ParkingParams,
) -> list[_Built]:
    zone = ZoneInfo(params.tz)
    as_of = params.as_of
    starts = [event.start for event in events]
    state_starts = [state.start for state in states]
    effective_end = [event.end if event.end is not None else as_of for event in events]
    prefix_max: list[datetime] = []
    for value in effective_end:
        prefix_max.append(value if not prefix_max else max(prefix_max[-1], value))
    built: list[_Built] = []

    def emit(item: ParkingWindow) -> None:
        bucket_start = _local_bucket_start(item.window_start, params.bucket, zone)
        built.append(
            _Built(
                item=item,
                bucket_start=bucket_start,
                tiebreak=(
                    item.start_event.type,
                    item.start_event.id,
                    item.end_event.type if item.end_event else "",
                    item.end_event.id if item.end_event else 0,
                ),
            )
        )

    for index in range(len(events) - 1):
        prev, nxt = events[index], events[index + 1]
        if prev.end is None:
            continue  # In-progress previous event: never fabricate a window from its open span.
        ws, we = prev.end, nxt.start
        if not (params.from_ <= ws < params.to) or we > as_of:
            continue
        count = bisect_left(starts, we)
        overlap = count > 0 and prefix_max[count - 1] > ws
        start_ep = _endpoint(prev, "end", ws)
        end_ep = _endpoint(nxt, "start", we)
        duration_seconds = (we - ws).total_seconds()
        online, asleep, offline, ambiguous = _state_hours(states, state_starts, ws, we)
        duration_hours = duration_seconds / 3600.0
        covered = online + asleep + offline
        unknown = max(0.0, duration_hours - covered) if duration_seconds > 0 else 0.0
        soc_change = (
            end_ep.soc - start_ep.soc
            if end_ep.soc is not None and start_ep.soc is not None
            else None
        )
        odo_change = (
            end_ep.odometer_km - start_ep.odometer_km
            if end_ep.odometer_km is not None and start_ep.odometer_km is not None
            else None
        )
        reason: ExclusionReason | None
        if ws == we:
            reason = "same_moment_gap"
        elif ws > we:
            reason = "invalid_time_order"
        elif overlap:
            reason = "event_overlap"
        elif ambiguous:
            reason = "ambiguous_states"
        elif not (_soc_ok(start_ep.soc) and _soc_ok(end_ep.soc)):
            reason = "missing_soc"
        elif start_ep.odometer_km is None or end_ep.odometer_km is None:
            reason = "missing_odometer"
        elif (
            odo_change is None
            or not math.isfinite(odo_change)
            or abs(odo_change) >= MAX_ABS_ODOMETER_CHANGE_KM
        ):
            reason = "odometer_change"
        elif duration_seconds < MIN_DURATION_SECONDS:
            reason = "insufficient_duration"
        else:
            reason = None
        valid = reason is None
        start_event = BoundaryEvent(type=prev.type, id=prev.id)
        end_event = BoundaryEvent(type=nxt.type, id=nxt.id)
        emit(
            ParkingWindow(
                window_id=_window_id(start_event, end_event),
                start_event=start_event,
                end_event=end_event,
                window_start=ws,
                window_end=we,
                duration_hours=duration_hours,
                start=start_ep,
                end=end_ep,
                soc_change_pp=soc_change,
                soc_change_pp_per_24h=(
                    soc_change / duration_hours * 24.0
                    if valid and soc_change is not None and duration_hours > 0
                    else None
                ),
                odometer_change_km=odo_change,
                negative_odometer_change=bool(valid and odo_change is not None and odo_change < 0),
                online_hours=online,
                asleep_hours=asleep,
                offline_hours=offline,
                unknown_hours=unknown,
                quality=("partial_state_coverage" if unknown > 0 else "complete")
                if valid
                else "excluded",
                exclusion_reason=reason,
                included=valid,
            )
        )

    # Open tail: the car is parked after its last completed event, observed up to as_of.
    if events:
        last = events[-1]
        if (
            last.end is not None
            and params.from_ <= last.end < params.to
            and last.end < as_of
            and all(event.end is not None for event in events)
        ):
            ws = last.end
            start_ep = _endpoint(last, "end", ws)
            online, asleep, offline, _ = _state_hours(states, state_starts, ws, as_of)
            duration_hours = (as_of - ws).total_seconds() / 3600.0
            covered = online + asleep + offline
            start_event = BoundaryEvent(type=last.type, id=last.id)
            emit(
                ParkingWindow(
                    window_id=_window_id(start_event, None),
                    start_event=start_event,
                    end_event=None,
                    window_start=ws,
                    window_end=as_of,
                    duration_hours=duration_hours,
                    start=start_ep,
                    end=ParkingEndpoint(),
                    soc_change_pp=None,
                    soc_change_pp_per_24h=None,
                    odometer_change_km=None,
                    negative_odometer_change=False,
                    online_hours=online,
                    asleep_hours=asleep,
                    offline_hours=offline,
                    unknown_hours=max(0.0, duration_hours - covered),
                    quality="open_window",
                    exclusion_reason=None,
                    included=False,
                )
            )
    return built


def _aggregate(
    built: list[_Built], calendar: tuple[datetime, ...]
) -> tuple[ParkingAggregate, list[ParkingBucket]]:
    per_bucket: dict[datetime, list[_Built]] = {start: [] for start in calendar}
    for entry in built:
        per_bucket.setdefault(entry.bucket_start, []).append(entry)

    def summarize(entries: list[_Built]) -> ParkingAggregate:
        aggregate = ParkingAggregate()
        reasons = ExcludedByReason()
        observed = online = asleep = offline = unknown = 0.0
        soc_total = 0
        valid = 0
        for entry in entries:
            item = entry.item
            if item.end_event is None:
                aggregate.open_windows += 1
                continue
            aggregate.candidate_windows += 1
            if item.included:
                valid += 1
                observed += item.duration_hours or 0.0
                online += item.online_hours
                asleep += item.asleep_hours
                offline += item.offline_hours
                unknown += item.unknown_hours
                soc_total += item.soc_change_pp or 0
            elif item.exclusion_reason is not None:
                setattr(reasons, item.exclusion_reason, getattr(reasons, item.exclusion_reason) + 1)
        aggregate.valid_windows = valid
        aggregate.excluded_windows = aggregate.candidate_windows - valid
        aggregate.observed_hours = observed
        aggregate.online_hours = online
        aggregate.asleep_hours = asleep
        aggregate.offline_hours = offline
        aggregate.unknown_hours = unknown
        aggregate.soc_change_pp = soc_total if valid else None
        if observed > 0:
            aggregate.soc_change_pp_per_24h = soc_total / observed * 24.0
            aggregate.online_share = online / observed
            aggregate.asleep_share = asleep / observed
            aggregate.offline_share = offline / observed
            aggregate.unknown_share = unknown / observed
        aggregate.excluded_by_reason = reasons
        return aggregate

    totals = summarize(built)
    buckets = [
        ParkingBucket(bucket_start=start, **summarize(per_bucket[start]).model_dump())
        for start in calendar
    ]
    return totals, buckets


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


def _is_after(entry: _Built, key: tuple[datetime, tuple[str, int, str, int]]) -> bool:
    window_start, tiebreak = key
    if entry.item.window_start != window_start:
        return entry.item.window_start < window_start
    return entry.tiebreak > tiebreak


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

    args: tuple[object, ...] = (vehicle.id, params.as_of, params.from_)
    async with pool.acquire() as db:
        event_rows = await fetch_rows(db, parking_events_sql(), *args)
        state_rows = await fetch_rows(db, parking_states_sql(), *args)
    events = _parse_events(event_rows)
    states = _parse_states(state_rows)
    built = _build_windows(events, states, params)
    built.sort(key=lambda entry: entry.tiebreak)
    built.sort(key=lambda entry: entry.item.window_start, reverse=True)

    key = _decode_key(params.cursor, _binding(vehicle.id, params))
    start_index = 0
    if key is not None:
        start_index = len(built)
        for index, entry in enumerate(built):
            if _is_after(entry, key):
                start_index = index
                break
    page = built[start_index : start_index + params.limit]
    next_cursor = None
    if start_index + params.limit < len(built):
        last = page[-1].item
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
    totals, buckets = _aggregate(built, params.calendar)
    if totals.valid_windows > 0:
        status: Status = "available"
    else:
        status = "insufficient_data"
    return respond(
        status,
        totals,
        buckets,
        [entry.item for entry in page],
        len(built),
        next_cursor,
    )
