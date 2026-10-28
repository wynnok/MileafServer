"""Read-only charging sessions with added and grid-side energy kept distinct."""

from datetime import datetime
from typing import Annotated, Literal

import asyncpg
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from starlette.responses import Response

from mileaf.cache import TTLCache, cached_response
from mileaf.database import fetch_rows
from mileaf.dependencies import get_cache, get_now, get_pool
from mileaf.errors import APIError, ErrorCode
from mileaf.probes import CAPABILITIES
from mileaf.queries.charging import is_dc_sql
from mileaf.queries.charging_events import StateEvents, state_events
from mileaf.queries.charging_fields import (
    CHARGER_FIELDS,
    HEATER_FIELDS,
    RANGE_FIELDS,
    SAMPLE_FIELDS,
    ChargingCapabilities,
    charging_capabilities,
)
from mileaf.queries.charging_metrics import UnitCost, session_unit_cost
from mileaf.queries.charging_process import ProcessMetrics, process_metrics
from mileaf.queries.charging_query import (
    ChargingQuery,
    ChargingQueryEcho,
    charging_parameters,
    encode_bound_cursor,
    location_id_sql,
    match_source,
    session_cursor_key,
)
from mileaf.queries.locations import Location, LocationRef, fetch_locations
from mileaf.queries.parameters import ResourceID, ResourceRoute
from mileaf.queries.series import fetch_series
from mileaf.units import UnitSettings, get_units
from mileaf.vehicles import VehicleIdentity, get_vehicle_or_404

router = APIRouter(prefix="/v1", tags=["charging sessions"], route_class=ResourceRoute)


class ChargingSession(BaseModel):
    id: int
    started_at: datetime
    ended_at: datetime | None
    duration_min: int | None
    energy_added_kwh: float | None = Field(description="Energy added to the battery, kWh")
    energy_used_kwh: float | None = Field(description="Grid-side energy consumed, kWh")
    start_soc: int | None
    end_soc: int | None
    cost: float | None
    is_dc: bool | None
    location: Location
    is_active: bool
    location_id: str


class ChargerFacts(BaseModel):
    fast_charger_present: bool | None
    fast_charger_brand: str | None
    fast_charger_type: str | None
    conn_charge_cable: str | None
    source_sample_at: datetime | None
    source: Literal["latest_charge_sample"] = "latest_charge_sample"


class ChargingSessionDetail(ChargingSession):
    vehicle_id: int
    outside_temp_c_avg: float | None
    start_battery_level: int | None
    end_battery_level: int | None
    charger_phases_mode: int | None
    unit_cost_added: UnitCost | None
    unit_cost_grid: UnitCost | None
    process_metrics: ProcessMetrics | None = None
    start_rated_range_km: float | None = None
    end_rated_range_km: float | None = None
    start_ideal_range_km: float | None = None
    end_ideal_range_km: float | None = None
    charger: ChargerFacts | None = None
    capabilities: ChargingCapabilities | None = None
    as_of: datetime | None = None


SESSION_SOURCE = f"""
    SELECT cp.*, cp.car_id AS vehicle_id,
        cp.start_date AT TIME ZONE 'UTC' AS started_at,
        cp.end_date AT TIME ZONE 'UTC' AS ended_at,
        /* TeslaMate fills the process aggregate on completion; an active session keeps
           its current cumulative value in the latest charge sample. */
        CASE WHEN cp.end_date IS NULL THEN coalesce(
            (SELECT c.charge_energy_added FROM charges c
             WHERE c.charging_process_id=cp.id
             ORDER BY c.date DESC,c.id DESC LIMIT 1), cp.charge_energy_added)
        ELSE cp.charge_energy_added END AS energy_added_kwh,
        {is_dc_sql()} AS is_dc, {location_id_sql()} AS location_id,
        (SELECT mode() WITHIN GROUP (ORDER BY charger_phases)
         FROM charges WHERE charging_process_id=cp.id) AS charger_phases_mode
    FROM charging_processes cp JOIN cars c ON c.id=cp.car_id
"""


async def session_models(
    db: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    rows: list[asyncpg.Record],
) -> list[ChargingSessionDetail]:
    locations = await fetch_locations(
        db, [LocationRef(row["position_id"], row["address_id"], row["geofence_id"]) for row in rows]
    )
    result = []
    for row, location in zip(rows, locations, strict=True):
        active = row["ended_at"] is None
        result.append(
            ChargingSessionDetail(
                id=row["id"],
                vehicle_id=row["vehicle_id"],
                started_at=row["started_at"],
                ended_at=row["ended_at"],
                duration_min=None if active else row["duration_min"],
                energy_added_kwh=row["energy_added_kwh"],
                energy_used_kwh=row["charge_energy_used"],
                start_soc=row["start_battery_level"],
                end_soc=None if active else row["end_battery_level"],
                cost=row["cost"],
                is_dc=row["is_dc"],
                location=location,
                is_active=active,
                location_id=row["location_id"],
                outside_temp_c_avg=row["outside_temp_avg"],
                start_battery_level=row["start_battery_level"],
                end_battery_level=None if active else row["end_battery_level"],
                charger_phases_mode=row["charger_phases_mode"],
                unit_cost_added=None
                if active
                else session_unit_cost(row["cost"], row["energy_added_kwh"]),
                unit_cost_grid=None
                if active
                else session_unit_cost(row["cost"], row["charge_energy_used"]),
            )
        )
    return result


@router.get("/charging-sessions/{session_id}", response_model=ChargingSessionDetail)
async def charging_session_detail(
    now: Annotated[datetime, Depends(get_now)],
    session_id: ResourceID,
    request: Request,
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    units: Annotated[UnitSettings, Depends(get_units)],
    cache: Annotated[TTLCache, Depends(get_cache)],
) -> Response:
    async def render() -> bytes:
        async with pool.acquire() as db:
            rows = await fetch_rows(db, SESSION_SOURCE + " WHERE cp.id=$1", session_id)
            if not rows:
                raise APIError(404, ErrorCode.NOT_FOUND, "Resource not found")
            result = (await session_models(db, rows))[0]
            result.process_metrics = await process_metrics(db, session_id, now)
            caps = charging_capabilities(request.app.state.compatibility.charging_columns)
            for name in RANGE_FIELDS:
                value = rows[0].get(name) if caps.fields[name] else None
                if result.is_active and name.startswith("end_"):
                    value = None
                setattr(result, name, value)
            projection = ",".join(
                f"{column if caps.fields[name] else 'NULL'} AS {name}"
                for name, (column, _) in CHARGER_FIELDS.items()
            )
            facts = await fetch_rows(
                db,
                f"SELECT {projection},date AT TIME ZONE 'UTC' AS source_sample_at "
                "FROM charges WHERE charging_process_id=$1 ORDER BY date DESC,id DESC LIMIT 1",
                session_id,
            )
            result.charger = (
                ChargerFacts(**dict(facts[0]))
                if facts
                else ChargerFacts(**dict.fromkeys(CHARGER_FIELDS), source_sample_at=None)
            )
            result.capabilities = caps
            result.as_of = now
            return result.model_dump_json().encode()

    return await cached_response(
        request, cache=cache, key=(request.url.path, request.url.query), render=render, ttl=30
    )


class ChargingSessionPage(BaseModel):
    items: list[ChargingSession]
    next_cursor: str | None
    matched_sessions: int
    query: ChargingQueryEcho
    as_of: datetime


@router.get("/vehicles/{vehicle_id}/charging-sessions", response_model=ChargingSessionPage)
async def charging_session_list(
    request: Request,
    vehicle: Annotated[VehicleIdentity, Depends(get_vehicle_or_404)],
    params: Annotated[ChargingQuery, Depends(charging_parameters)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    cache: Annotated[TTLCache, Depends(get_cache)],
) -> Response:
    async def render() -> bytes:
        async with pool.acquire() as db:
            source, args = match_source(params, vehicle.id, completed=False)
            binding = params.binding(vehicle.id, view="sessions")
            value, time, identifier = session_cursor_key(params.cursor, binding)
            sorts = {
                "started_desc": "extract(epoch FROM started_at)",
                "ended_desc": "extract(epoch FROM ended_at)",
                "duration_desc": "CASE WHEN ended_at IS NOT NULL THEN duration END",
                "unit_cost_added_asc": "CASE WHEN cost IS NOT NULL AND energy>0 "
                "AND ended_at IS NOT NULL THEN -cost/energy END",
            }
            rows = await fetch_rows(
                db,
                f"WITH matched AS ({source}), sorted AS (SELECT *, "
                f"{sorts[params.sort]} AS sort_value FROM matched) SELECT * FROM sorted "
                "WHERE ($9::timestamptz IS NULL OR "
                "($8::numeric IS NOT NULL AND (sort_value<$8 OR sort_value IS NULL)) OR "
                "(sort_value IS NOT DISTINCT FROM $8::numeric AND "
                "(started_at,id)<($9,$10::integer))) "
                "ORDER BY sort_value DESC NULLS LAST,started_at DESC,id DESC LIMIT $11",
                *args,
                value,
                time,
                identifier,
                params.limit + 1,
            )
            count = await fetch_rows(db, f"SELECT count(*) AS n FROM ({source}) matched", *args)
            next_cursor = None
            if len(rows) > params.limit:
                last = rows[params.limit - 1]
                next_cursor = encode_bound_cursor(
                    binding,
                    [
                        str(last["sort_value"]) if last["sort_value"] is not None else None,
                        last["started_at"].isoformat(),
                        last["id"],
                    ],
                )
            # Reuse the existing detail projection only for this bounded page.
            ids = [row["id"] for row in rows[: params.limit]]
            details = await fetch_rows(db, SESSION_SOURCE + " WHERE cp.id=ANY($1::integer[])", ids)
            models = {item.id: item for item in await session_models(db, details)}
            return (
                ChargingSessionPage(
                    items=[models[identifier] for identifier in ids],
                    next_cursor=next_cursor,
                    matched_sessions=count[0]["n"],
                    query=params.echo,
                    as_of=params.as_of,
                )
                .model_dump_json(by_alias=True)
                .encode()
            )

    return await cached_response(
        request, cache=cache, key=(request.url.path, request.url.query), render=render, ttl=30
    )


class ChargePoint(BaseModel):
    t: datetime
    power: float | None = None
    soc: int | None = None
    voltage_v: float | None = None
    actual_current_a: float | None = None
    pilot_current_a: float | None = None
    outside_temp_c: float | None = None
    rated_range_km: float | None = None
    ideal_range_km: float | None = None
    charge_energy_added_kwh: float | None = None
    battery_heater: bool | None = None
    battery_heater_on: bool | None = None
    battery_heater_no_power: bool | None = None
    not_enough_power_to_heat: bool | None = None


class ChargingSessionSeries(BaseModel):
    session_id: int
    resolution: Literal["empty", "server_sampled"]
    points: int
    series: list[ChargePoint]
    capabilities: ChargingCapabilities
    as_of: datetime
    state_events: StateEvents | None = None
    sampling_rule: Literal["first-field-extrema-then-multifield-lttb-v1"] = (
        "first-field-extrema-then-multifield-lttb-v1"
    )


@router.get(
    "/charging-sessions/{session_id}/series",
    response_model=ChargingSessionSeries,
    response_model_exclude_unset=True,
)
async def charging_session_series(
    now: Annotated[datetime, Depends(get_now)],
    session_id: ResourceID,
    request: Request,
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    units: Annotated[UnitSettings, Depends(get_units)],
    cache: Annotated[TTLCache, Depends(get_cache)],
    fields: str = "power,soc",
    points: str = "200",
) -> Response:
    async def render() -> bytes:
        async with pool.acquire() as db:
            exists = await fetch_rows(
                db,
                "SELECT cp.id FROM charging_processes cp JOIN cars c ON c.id=cp.car_id "
                "WHERE cp.id=$1",
                session_id,
            )
            if not exists:
                raise APIError(404, ErrorCode.NOT_FOUND, "Resource not found")
            caps = charging_capabilities(request.app.state.compatibility.charging_columns)
            columns = {
                name: column
                if caps.fields[name]
                else ("NULL::boolean" if name in HEATER_FIELDS else "NULL::numeric")
                for name, (column, _) in SAMPLE_FIELDS.items()
            }
            sampled = await fetch_series(
                db,
                table="charges",
                time_column="date",
                columns=columns,
                where="charging_process_id=$1",
                args=(session_id,),
                fields=fields,
                points=points,
            )
            events = {}
            requested = tuple(fields.split(","))
            if any(name in HEATER_FIELDS for name in requested):
                events["state_events"] = await state_events(db, session_id, requested, caps.fields)
        return (
            ChargingSessionSeries(
                session_id=session_id,
                resolution=sampled.resolution,
                points=len(sampled.series),
                series=[ChargePoint(**row) for row in sampled.series],
                capabilities=caps,
                as_of=now,
                sampling_rule="first-field-extrema-then-multifield-lttb-v1",
                **events,
            )
            .model_dump_json(exclude_unset=True)
            .encode()
        )

    return await cached_response(
        request, cache=cache, key=(request.url.path, request.url.query), render=render, ttl=30
    )


CAPABILITIES["charging_session_details"] = {
    "charging_processes": {
        **{name: ("int4",) for name in ("position_id", "address_id", "geofence_id")},
        **{
            name: ("int2",) for name in ("duration_min", "start_battery_level", "end_battery_level")
        },
        **{name: ("numeric",) for name in ("charge_energy_used", "outside_temp_avg", "cost")},
    },
    "charges": {
        "id": ("int4",),
        "date": ("timestamp",),
        "charge_energy_added": ("numeric",),
        "charger_power": ("int2",),
        "battery_level": ("int2",),
    },
}
