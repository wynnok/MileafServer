"""Vehicle list and the shared vehicle lookup dependency."""

from datetime import datetime
from typing import Annotated, Literal

import asyncpg
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from starlette.responses import Response

from mileaf.cache import conditional_response
from mileaf.dependencies import VehicleID, get_pool
from mileaf.errors import APIError, ErrorCode
from mileaf.meta import Freshness
from mileaf.probes import CAPABILITIES
from mileaf.units import UnitSettings, distance_km, get_units

CAPABILITIES["vehicles"] = {
    "cars": {
        "id": ("int2",),
        "name": ("varchar",),
        "model": ("varchar",),
        "vin": ("text",),
        "inserted_at": ("timestamp",),
    },
    "states": {
        "id": ("int4",),
        "car_id": ("int2",),
        "state": ("states_status",),
        "start_date": ("timestamp",),
    },
    "positions": {
        "id": ("int4",),
        "car_id": ("int2",),
        "date": ("timestamp",),
        "battery_level": ("int2",),
        "rated_battery_range_km": ("numeric",),
        "ideal_battery_range_km": ("numeric",),
    },
    "charging_processes": {
        "id": ("int4",),
        "car_id": ("int2",),
        "start_date": ("timestamp",),
        "end_date": ("timestamp",),
    },
    "settings": {"preferred_range": ("range", "preferred_range")},
}
router = APIRouter(prefix="/v1")


class VehicleIdentity(BaseModel):
    id: int
    name: str | None
    model: str | None
    vin_suffix: str


class StateSegment(BaseModel):
    value: Literal["online", "asleep", "offline", "charging"]
    since: datetime


class Battery(BaseModel):
    level: int | None
    range_km: float | None
    range_kind: Literal["rated", "ideal"]


class Vehicle(VehicleIdentity):
    state: StateSegment | None
    battery: Battery | None
    freshness: Freshness


async def get_vehicle_or_404(
    vehicle_id: VehicleID,
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    units: Annotated[UnitSettings, Depends(get_units)],
) -> VehicleIdentity:
    # cars.id is int2: out-of-range IDs are absent, not upstream failures.
    if vehicle_id > 32767:
        raise APIError(404, ErrorCode.NOT_FOUND, "Resource not found")
    row = await pool.fetchrow(
        "SELECT id, name, model, right(vin,4) AS vin_suffix FROM cars WHERE id=$1", vehicle_id
    )
    if row is None:
        raise APIError(404, ErrorCode.NOT_FOUND, "Resource not found")
    return VehicleIdentity(**dict(row))


@router.get("/vehicles", response_model=list[Vehicle])
async def vehicles(
    request: Request,
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    units: Annotated[UnitSettings, Depends(get_units)],
) -> Response:
    rows = await pool.fetch("""
        SELECT c.id, c.name, c.model, right(c.vin,4) AS vin_suffix,
            CASE WHEN cp.id IS NOT NULL THEN 'charging' ELSE s.state::text END AS state,
            (CASE WHEN cp.id IS NOT NULL THEN cp.start_date ELSE s.start_date END)
                AT TIME ZONE 'UTC' AS since,
            p.id AS position_id, p.battery_level,
            CASE WHEN settings.preferred_range::text='ideal'
                 THEN p.ideal_battery_range_km ELSE p.rated_battery_range_km END AS range_km,
            settings.preferred_range::text AS range_kind,
            p.date AT TIME ZONE 'UTC' AS last_position_at,
            s.start_date AT TIME ZONE 'UTC' AS last_state_at
        FROM cars c CROSS JOIN settings
        LEFT JOIN LATERAL (
            SELECT id, state, start_date FROM states WHERE car_id=c.id
            ORDER BY start_date DESC, id DESC LIMIT 1
        ) s ON true
        LEFT JOIN LATERAL (
            SELECT id, date, battery_level, rated_battery_range_km, ideal_battery_range_km
            FROM positions WHERE car_id=c.id ORDER BY date DESC, id DESC LIMIT 1
        ) p ON true
        LEFT JOIN LATERAL (
            SELECT id, start_date FROM charging_processes
            WHERE car_id=c.id AND end_date IS NULL
            ORDER BY start_date DESC, id DESC LIMIT 1
        ) cp ON true
        ORDER BY c.inserted_at, c.id
    """)
    result = [
        Vehicle(
            id=row["id"],
            name=row["name"],
            model=row["model"],
            vin_suffix=row["vin_suffix"],
            state=StateSegment(value=row["state"], since=row["since"]) if row["state"] else None,
            battery=Battery(
                level=row["battery_level"],
                range_km=distance_km(row["range_km"]),
                range_kind=row["range_kind"],
            )
            if row["position_id"]
            else None,
            freshness=Freshness(
                last_position_at=row["last_position_at"], last_state_at=row["last_state_at"]
            ),
        )
        for row in rows
    ]
    body = b"[" + b",".join(vehicle.model_dump_json().encode() for vehicle in result) + b"]"
    return conditional_response(request, body, ttl=request.app.state.settings.cache_ttl_seconds)
