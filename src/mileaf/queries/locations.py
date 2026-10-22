"""One place-name fallback for inline SELECTs and batched location references."""

from dataclasses import dataclass

import asyncpg
from pydantic import BaseModel

from mileaf.database import fetch_rows
from mileaf.probes import CAPABILITIES

CAPABILITIES["locations"] = {
    "positions": {"id": ("int4",), "latitude": ("numeric",), "longitude": ("numeric",)},
    "addresses": {
        "id": ("int4",),
        **{
            name: ("varchar",)
            for name in (
                "name",
                "road",
                "house_number",
                "city",
                "neighbourhood",
            )
        },
    },
    "geofences": {"id": ("int4",), "name": ("varchar",)},
}


class Location(BaseModel):
    display_name: str | None
    geofence: str | None
    address: str | None
    lat: float | None
    lon: float | None


@dataclass(frozen=True)
class LocationRef:
    position_id: int | None = None
    address_id: int | None = None
    geofence_id: int | None = None


def location_sql(*, position: str = "p", address: str = "a", geofence: str = "g") -> str:
    """Return structured JSONB. Aliases must be trusted SQL, never request values."""

    def text(expression: str) -> str:
        return f"nullif(btrim({expression}), '')"

    name = text(f"{geofence}.name")
    address_name = (
        f"coalesce({text(f'{address}.name')}, "
        f"nullif(concat_ws(' ', {text(f'{address}.road')}, "
        f"{text(f'{address}.house_number')}), ''), "
        f"{text(f'{address}.city')}, {text(f'{address}.neighbourhood')})"
    )
    return (
        "(/* TeslaMate v4.3.0 drives place semantics, independently rewritten: "
        "geofence -> address name -> road + house_number -> city/neighbourhood -> NULL. "
        "Mileaf returns the first available label, no empty placeholders. Coordinates "
        "come only from the associated position (degrees), never address/geofence centres. */ "
        f"jsonb_build_object('display_name', coalesce({name}, {address_name}), "
        f"'geofence', {name}, 'address', {address_name}, "
        f"'lat', {position}.latitude, 'lon', {position}.longitude))"
    )


async def fetch_locations(
    db: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy, refs: list[LocationRef]
) -> list[Location]:
    """One SELECT for a page's start/end or session references, preserving order and duplicates."""
    if not refs:
        return []
    rows = await fetch_rows(
        db,
        "SELECT " + location_sql() + " AS location FROM "
        "unnest($1::integer[], $2::integer[], $3::integer[]) WITH ORDINALITY "
        "AS refs(position_id,address_id,geofence_id,ordinal) "
        "LEFT JOIN positions p ON p.id=refs.position_id "
        "LEFT JOIN addresses a ON a.id=refs.address_id "
        "LEFT JOIN geofences g ON g.id=refs.geofence_id ORDER BY refs.ordinal",
        [ref.position_id for ref in refs],
        [ref.address_id for ref in refs],
        [ref.geofence_id for ref in refs],
    )
    return [Location.model_validate_json(row["location"]) for row in rows]
