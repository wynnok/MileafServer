from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mileaf.models import Charge, Drive
from mileaf.schemas import StatsOut

VALID_PERIODS = {"7d", "30d", "90d", "month", "year", "all"}


def period_start(period: str, now: datetime | None = None) -> datetime | None:
    if period not in VALID_PERIODS:
        raise ValueError(f"unsupported period: {period}")

    current = now or datetime.now(UTC)
    if period == "all":
        return None
    if period.endswith("d"):
        return current - timedelta(days=int(period[:-1]))
    if period == "month":
        return current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == "year":
        return current.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"unsupported period: {period}")


async def compute_stats(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    vehicle_id: uuid.UUID,
    period: str,
    now: datetime | None = None,
) -> StatsOut:
    end = now or datetime.now(UTC)
    start = period_start(period, end)

    drive_conditions = [
        Drive.tenant_id == tenant_id,
        Drive.vehicle_id == vehicle_id,
        Drive.end_date.is_not(None),
        Drive.start_date <= end,
    ]
    charge_conditions = [
        Charge.tenant_id == tenant_id,
        Charge.vehicle_id == vehicle_id,
        Charge.end_date.is_not(None),
        Charge.start_date <= end,
    ]
    if start is not None:
        drive_conditions.append(Drive.start_date >= start)
        charge_conditions.append(Charge.start_date >= start)

    drive_row = (
        await session.execute(
            select(
                func.count(Drive.id),
                func.coalesce(func.sum(Drive.distance_km), 0.0),
                func.coalesce(func.sum(Drive.energy_kwh), 0.0),
            ).where(*drive_conditions)
        )
    ).one()
    charge_row = (
        await session.execute(
            select(
                func.count(Charge.id),
                func.coalesce(func.sum(Charge.energy_added_kwh), 0.0),
                func.coalesce(func.sum(Charge.cost), 0.0),
            ).where(*charge_conditions)
        )
    ).one()

    drive_count = int(drive_row[0] or 0)
    distance_km = float(drive_row[1] or 0.0)
    drive_energy_kwh = float(drive_row[2] or 0.0)
    charge_count = int(charge_row[0] or 0)
    charge_energy_kwh = float(charge_row[1] or 0.0)
    charge_cost = float(charge_row[2] or 0.0)

    efficiency = drive_energy_kwh * 1000 / distance_km if distance_km > 0 else None
    avg_distance = distance_km / drive_count if drive_count > 0 else None

    return StatsOut(
        period=period,
        period_start=start,
        period_end=end,
        drive_count=drive_count,
        distance_km=round(distance_km, 3),
        drive_energy_kwh=round(drive_energy_kwh, 3),
        efficiency_wh_km=round(efficiency, 1) if efficiency is not None else None,
        charge_count=charge_count,
        charge_energy_kwh=round(charge_energy_kwh, 3),
        charge_cost=round(charge_cost, 2),
        avg_drive_distance_km=round(avg_distance, 3) if avg_distance is not None else None,
    )
