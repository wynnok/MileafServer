"""Bounded charging location accounts over the same completed match set as stats."""

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

import asyncpg
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from starlette.responses import Response

from mileaf.cache import conditional_response
from mileaf.database import fetch_rows
from mileaf.dependencies import get_pool
from mileaf.queries.charging_metrics import ChargingMetrics, charging_metrics_sql
from mileaf.queries.charging_query import (
    ChargingQuery,
    ChargingQueryEcho,
    charging_location_parameters,
    decode_bound_cursor,
    encode_bound_cursor,
    invalid,
    match_source,
)
from mileaf.queries.locations import Location, location_sql
from mileaf.queries.parameters import ResourceRoute
from mileaf.vehicles import VehicleIdentity, get_vehicle_or_404

router = APIRouter(prefix="/v1", tags=["charging locations"], route_class=ResourceRoute)


class ChargingLocation(ChargingMetrics):
    location_id: str
    identity_kind: Literal["geofence", "address", "unlocated"]
    location: Location
    representative_session_id: int
    coordinate_source: Literal["latest_session_position"] | None


class ChargingLocationPage(BaseModel):
    items: list[ChargingLocation]
    next_cursor: str | None
    total_locations: int
    unlocated_sessions: int
    query: ChargingQueryEcho
    as_of: datetime


@router.get("/vehicles/{vehicle_id}/charging-locations", response_model=ChargingLocationPage)
async def charging_locations(
    request: Request,
    vehicle: Annotated[VehicleIdentity, Depends(get_vehicle_or_404)],
    params: Annotated[ChargingQuery, Depends(charging_location_parameters)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> Response:
    binding = params.binding(vehicle.id, view="locations")
    key = decode_bound_cursor(params.cursor, binding)
    value, identifier = None, None
    if key is not None:
        try:
            raw_value, identifier = key
            value = Decimal(raw_value)
            if not value.is_finite() or not isinstance(identifier, str) or len(identifier) > 64:
                raise ValueError
        except (TypeError, ValueError, InvalidOperation):
            raise invalid("Invalid location cursor key") from None
    source, args = match_source(params, vehicle.id, completed=True)
    metric_sql = ", ".join(
        f"{expression} AS {name}" for name, expression in charging_metrics_sql().items()
    )
    order = "energy_added_kwh" if params.sort == "energy_desc" else "sessions"
    query = (
        f"WITH matched AS ({source}), aggregated AS (SELECT location_id, {metric_sql} "
        "FROM matched GROUP BY location_id), representatives AS ("
        "SELECT DISTINCT ON (m.location_id) m.location_id,m.id AS representative_session_id, "
        "CASE WHEN m.geofence_id IS NOT NULL THEN 'geofence' WHEN m.address_id IS NOT NULL "
        "THEN 'address' ELSE 'unlocated' END AS identity_kind, "
        f"{location_sql()} AS location, CASE WHEN p.latitude IS NOT NULL "
        "AND p.longitude IS NOT NULL THEN 'latest_session_position' END AS coordinate_source "
        "FROM matched m LEFT JOIN positions p ON p.id=m.position_id "
        "LEFT JOIN addresses a ON a.id=m.address_id LEFT JOIN geofences g ON g.id=m.geofence_id "
        "ORDER BY m.location_id,(p.latitude IS NOT NULL AND p.longitude IS NOT NULL) DESC, "
        "m.started_at DESC,m.id DESC), page AS ("
        f"SELECT a.*,r.representative_session_id,r.identity_kind,r.location,r.coordinate_source, "
        f"a.{order} AS sort_value FROM aggregated a JOIN representatives r USING(location_id) "
        f"WHERE ($8::numeric IS NULL OR a.{order}<$8 OR (a.{order}=$8 AND a.location_id>$9)) "
        f"ORDER BY a.{order} DESC,a.location_id LIMIT $10) SELECT "
        "(SELECT count(*) FROM aggregated) AS total_locations, "
        "(SELECT count(*) FROM matched WHERE geofence_id IS NULL AND address_id IS NULL) "
        "AS unlocated_sessions, "
        f"(SELECT coalesce(jsonb_agg(page ORDER BY sort_value DESC,location_id),'[]'::jsonb) "
        "FROM page) AS items"
    )
    async with pool.acquire() as db:
        rows = await fetch_rows(db, query, *args, value, identifier, params.limit + 1)
    row = rows[0]
    items = json.loads(row["items"])
    next_cursor = None
    if len(items) > params.limit:
        last = items[params.limit - 1]
        next_cursor = encode_bound_cursor(binding, [str(last["sort_value"]), last["location_id"]])
    result = ChargingLocationPage(
        items=[ChargingLocation(**item) for item in items[: params.limit]],
        next_cursor=next_cursor,
        total_locations=row["total_locations"],
        unlocated_sessions=row["unlocated_sessions"],
        query=params.echo,
        as_of=params.as_of,
    )
    return conditional_response(
        request,
        result.model_dump_json(by_alias=True).encode(),
        ttl=30,
        validator_body=result.model_dump_json(by_alias=True, exclude={"as_of"}).encode(),
    )
