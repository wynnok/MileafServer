"""Authenticated site metadata."""

from datetime import datetime
from importlib.metadata import version
from typing import Annotated, Literal

import asyncpg
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from mileaf.dependencies import get_pool
from mileaf.units import UnitSettings, get_units

router = APIRouter(prefix="/v1")


class Freshness(BaseModel):
    last_position_at: datetime | None
    last_state_at: datetime | None


class Meta(BaseModel):
    version: str
    teslamate_schema_version: int | None
    schema_ok: bool = True
    tested: bool
    units: Literal["metric"] = "metric"
    source_unit_of_length: Literal["km", "mi"]
    freshness: Freshness


@router.get("/meta")
async def meta(
    request: Request,
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
    units: Annotated[UnitSettings, Depends(get_units)],
) -> Meta:
    # Recovery from an unreachable startup must run the same probes first.
    compatibility = request.app.state.compatibility
    row = await pool.fetchrow(
        "SELECT (SELECT max(date) AT TIME ZONE 'UTC' FROM positions) AS last_position_at, "
        "(SELECT max(start_date) AT TIME ZONE 'UTC' FROM states) AS last_state_at"
    )
    assert row is not None
    return Meta(
        version=version("mileaf-server"),
        teslamate_schema_version=compatibility.teslamate_schema_version,
        tested=compatibility.tested,
        source_unit_of_length=units.source_unit_of_length,
        freshness=Freshness(**dict(row)),
    )
