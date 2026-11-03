"""The sole opt-in TeslaMate write: fill a completed session's missing cost."""

from decimal import Decimal
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from mileaf.dependencies import get_pool
from mileaf.errors import APIError, ErrorCode
from mileaf.queries.parameters import ResourceID, ResourceRoute

router = APIRouter(prefix="/v1", tags=["charging sessions"], route_class=ResourceRoute)


class ChargingCostInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unit_price: Decimal = Field(ge=0, le=999999, max_digits=12, decimal_places=6)


class ChargingCostResult(BaseModel):
    session_id: int
    unit_price: Decimal
    billing_energy_kwh: Decimal
    cost: Decimal


@router.patch("/charging-sessions/{session_id}/cost", response_model=ChargingCostResult)
async def fill_charging_cost(
    session_id: ResourceID,
    body: ChargingCostInput,
    request: Request,
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> ChargingCostResult:
    if not request.app.state.settings.charging_cost_writes_enabled:
        raise APIError(403, ErrorCode.INVALID_REQUEST, "Charging cost writes are disabled")
    try:
        async with pool.acquire() as db, db.transaction(readonly=False):
            await db.execute("SET LOCAL statement_timeout = '5s'")
            # Atomic predicate is rechecked after concurrent writers release their row lock.
            # TeslaMate ChargeLive.Cost per_kwh uses the larger available energy value.
            row = await db.fetchrow(
                "UPDATE charging_processes SET cost=round($2::numeric * "
                "greatest(charge_energy_added,charge_energy_used),2) "
                "WHERE id=$1 AND cost IS NULL AND end_date IS NOT NULL "
                "AND greatest(charge_energy_added,charge_energy_used)>0 "
                "AND greatest(charge_energy_added,charge_energy_used)<1000000 "
                "RETURNING cost,greatest(charge_energy_added,charge_energy_used) AS energy",
                session_id,
                body.unit_price,
            )
            if row is None:
                exists = await db.fetchval(
                    "SELECT id FROM charging_processes WHERE id=$1", session_id
                )
                if exists is None:
                    raise APIError(404, ErrorCode.NOT_FOUND, "Resource not found")
                raise APIError(
                    409, ErrorCode.INVALID_REQUEST, "Session is not eligible for cost entry"
                )
    except asyncpg.InsufficientPrivilegeError:
        raise APIError(
            503, ErrorCode.DB_UNAVAILABLE, "Charging cost write permission unavailable"
        ) from None
    except (TimeoutError, asyncpg.QueryCanceledError):
        raise APIError(503, ErrorCode.UPSTREAM_TIMEOUT, "Database query timed out") from None
    request.app.state.cache.invalidate()
    return ChargingCostResult(
        session_id=session_id,
        unit_price=body.unit_price,
        billing_energy_kwh=row["energy"],
        cost=row["cost"],
    )
