from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from mileaf.deps import DbSession, TenantCtx
from mileaf.models import Charge, Drive, Vehicle
from mileaf.schemas import (
    ChargeOut,
    DriveOut,
    Page,
    StatsOut,
    SummaryOut,
    VehicleOut,
)
from mileaf.services.pagination import InvalidCursorError, paginate_desc
from mileaf.services.stats import compute_stats

router = APIRouter(prefix="/v1/vehicles", tags=["vehicles"])


@router.get("", response_model=list[VehicleOut])
async def list_vehicles(ctx: TenantCtx, session: DbSession) -> list[VehicleOut]:
    result = await session.scalars(
        select(Vehicle)
        .options(selectinload(Vehicle.status))
        .where(Vehicle.tenant_id == ctx.tenant.id)
        .order_by(Vehicle.display_priority, Vehicle.name)
    )
    return [VehicleOut.model_validate(vehicle) for vehicle in result]


@router.get("/{vehicle_id}", response_model=VehicleOut)
async def get_vehicle(vehicle_id: uuid.UUID, ctx: TenantCtx, session: DbSession) -> VehicleOut:
    vehicle = await _vehicle_or_404(session, ctx.tenant.id, vehicle_id)
    return VehicleOut.model_validate(vehicle)


@router.get("/{vehicle_id}/summary", response_model=SummaryOut)
async def get_summary(
    vehicle_id: uuid.UUID,
    ctx: TenantCtx,
    session: DbSession,
    period: str = Query("30d"),
) -> SummaryOut:
    vehicle = await _vehicle_or_404(session, ctx.tenant.id, vehicle_id)
    stats = await _stats_or_400(session, ctx.tenant.id, vehicle_id, period)
    vehicle_out = VehicleOut.model_validate(vehicle)
    return SummaryOut(
        vehicle=vehicle_out,
        status=vehicle_out.status,
        stats=stats,
    )


@router.get("/{vehicle_id}/stats", response_model=StatsOut)
async def get_stats(
    vehicle_id: uuid.UUID,
    ctx: TenantCtx,
    session: DbSession,
    period: str = Query("30d"),
) -> StatsOut:
    await _vehicle_or_404(session, ctx.tenant.id, vehicle_id)
    return await _stats_or_400(session, ctx.tenant.id, vehicle_id, period)


@router.get("/{vehicle_id}/drives", response_model=Page[DriveOut])
async def list_drives(
    vehicle_id: uuid.UUID,
    ctx: TenantCtx,
    session: DbSession,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None),
    from_date: datetime | None = Query(None, alias="from"),
    to_date: datetime | None = Query(None, alias="to"),
) -> Page[DriveOut]:
    await _vehicle_or_404(session, ctx.tenant.id, vehicle_id)
    stmt = select(Drive).where(
        Drive.tenant_id == ctx.tenant.id,
        Drive.vehicle_id == vehicle_id,
    )
    if from_date is not None:
        stmt = stmt.where(Drive.start_date >= from_date)
    if to_date is not None:
        stmt = stmt.where(Drive.start_date <= to_date)
    try:
        rows, next_cursor = await paginate_desc(
            session,
            stmt,
            date_column=Drive.start_date,
            id_column=Drive.id,
            limit=limit,
            cursor=cursor,
        )
    except InvalidCursorError as exc:
        raise HTTPException(status_code=400, detail="invalid cursor") from exc
    return Page(items=[DriveOut.model_validate(row) for row in rows], next_cursor=next_cursor)


@router.get("/{vehicle_id}/drives/{drive_id}", response_model=DriveOut)
async def get_drive(
    vehicle_id: uuid.UUID, drive_id: uuid.UUID, ctx: TenantCtx, session: DbSession
) -> DriveOut:
    await _vehicle_or_404(session, ctx.tenant.id, vehicle_id)
    drive = await session.scalar(
        select(Drive).where(
            Drive.id == drive_id,
            Drive.tenant_id == ctx.tenant.id,
            Drive.vehicle_id == vehicle_id,
        )
    )
    if drive is None:
        raise HTTPException(status_code=404, detail="drive not found")
    return DriveOut.model_validate(drive)


@router.get("/{vehicle_id}/charges", response_model=Page[ChargeOut])
async def list_charges(
    vehicle_id: uuid.UUID,
    ctx: TenantCtx,
    session: DbSession,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None),
    from_date: datetime | None = Query(None, alias="from"),
    to_date: datetime | None = Query(None, alias="to"),
) -> Page[ChargeOut]:
    await _vehicle_or_404(session, ctx.tenant.id, vehicle_id)
    stmt = select(Charge).where(
        Charge.tenant_id == ctx.tenant.id,
        Charge.vehicle_id == vehicle_id,
    )
    if from_date is not None:
        stmt = stmt.where(Charge.start_date >= from_date)
    if to_date is not None:
        stmt = stmt.where(Charge.start_date <= to_date)
    try:
        rows, next_cursor = await paginate_desc(
            session,
            stmt,
            date_column=Charge.start_date,
            id_column=Charge.id,
            limit=limit,
            cursor=cursor,
        )
    except InvalidCursorError as exc:
        raise HTTPException(status_code=400, detail="invalid cursor") from exc
    return Page(items=[ChargeOut.model_validate(row) for row in rows], next_cursor=next_cursor)


@router.get("/{vehicle_id}/charges/{charge_id}", response_model=ChargeOut)
async def get_charge(
    vehicle_id: uuid.UUID, charge_id: uuid.UUID, ctx: TenantCtx, session: DbSession
) -> ChargeOut:
    await _vehicle_or_404(session, ctx.tenant.id, vehicle_id)
    charge = await session.scalar(
        select(Charge).where(
            Charge.id == charge_id,
            Charge.tenant_id == ctx.tenant.id,
            Charge.vehicle_id == vehicle_id,
        )
    )
    if charge is None:
        raise HTTPException(status_code=404, detail="charge not found")
    return ChargeOut.model_validate(charge)


async def _vehicle_or_404(
    session: DbSession, tenant_id: uuid.UUID, vehicle_id: uuid.UUID
) -> Vehicle:
    vehicle = await session.scalar(
        select(Vehicle)
        .options(selectinload(Vehicle.status))
        .where(Vehicle.id == vehicle_id, Vehicle.tenant_id == tenant_id)
    )
    if vehicle is None:
        raise HTTPException(status_code=404, detail="vehicle not found")
    return vehicle


async def _stats_or_400(
    session: DbSession, tenant_id: uuid.UUID, vehicle_id: uuid.UUID, period: str
) -> StatsOut:
    try:
        return await compute_stats(
            session, tenant_id=tenant_id, vehicle_id=vehicle_id, period=period
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
