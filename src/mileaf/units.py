"""TeslaMate stores metric values, independently of its display preference."""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

import asyncpg
from fastapi import Request

Number = Decimal | int | float


@dataclass(frozen=True)
class UnitSettings:
    source_unit_of_length: Literal["km", "mi"]


async def load_units(pool: asyncpg.Pool) -> UnitSettings:
    rows = await pool.fetch("SELECT unit_of_length::text FROM settings")
    if len(rows) != 1 or rows[0][0] not in {"km", "mi"}:
        raise RuntimeError("settings.unit_of_length must contain exactly one value: km or mi")
    return UnitSettings(source_unit_of_length=rows[0][0])


async def get_units(request: Request) -> UnitSettings:
    # A temporarily unreachable DB retains the runtime's anonymous health behavior.
    # The first recovered request validates settings before serving any metric data.
    if request.app.state.units is None:
        async with request.app.state.units_lock:
            if request.app.state.units is None:
                request.app.state.units = await load_units(request.app.state.pool)
    return request.app.state.units


def rounded_decimal(value: Number | None, *, places: Literal[0, 1]) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def distance_km(value: Number | None) -> Decimal | None:
    return rounded_decimal(value, places=1)


def speed_kmh(value: Number | None) -> Decimal | None:
    return rounded_decimal(value, places=0)


def consumption_wh_km(value: Number | None) -> Decimal | None:
    return rounded_decimal(value, places=0)


def metric_sql(expression: str, *, places: Literal[0, 1]) -> str:
    """Round a trusted SQL expression at the DTO boundary, never before aggregation."""
    return f"round(({expression})::numeric, {places})"
