from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mileaf.models import Charge, DataSource, Drive, Vehicle, VehicleStatus
from mileaf.schemas import (
    AgentChargeIn,
    AgentDriveIn,
    AgentSyncIn,
    AgentSyncOut,
)


class IngestError(Exception):
    """Raised when an agent batch references data that cannot be accepted."""


async def ingest_batch(
    session: AsyncSession, source: DataSource, payload: AgentSyncIn
) -> AgentSyncOut:
    now = datetime.now(UTC)
    referenced_ids = (
        {item.external_id for item in payload.vehicles}
        | {item.vehicle_external_id for item in payload.statuses}
        | {item.vehicle_external_id for item in payload.drives}
        | {item.vehicle_external_id for item in payload.charges}
    )

    existing_vehicles: dict[int, Vehicle] = {}
    if referenced_ids:
        result = await session.scalars(
            select(Vehicle).where(
                Vehicle.data_source_id == source.id,
                Vehicle.external_id.in_(referenced_ids),
            )
        )
        existing_vehicles = {vehicle.external_id: vehicle for vehicle in result}

    for vehicle_item in payload.vehicles:
        vehicle = existing_vehicles.get(vehicle_item.external_id)
        if vehicle is None:
            vehicle = Vehicle(
                tenant_id=source.tenant_id,
                data_source_id=source.id,
                external_id=vehicle_item.external_id,
                name=vehicle_item.name,
                model=vehicle_item.model,
            )
            session.add(vehicle)
            existing_vehicles[vehicle_item.external_id] = vehicle
        _apply_non_none(
            vehicle,
            {
                "name": vehicle_item.name,
                "model": vehicle_item.model,
                "vin": vehicle_item.vin,
                "marketing_name": vehicle_item.marketing_name,
                "trim_badging": vehicle_item.trim_badging,
                "exterior_color": vehicle_item.exterior_color,
                "wheel_type": vehicle_item.wheel_type,
                "efficiency": vehicle_item.efficiency,
                "display_priority": vehicle_item.display_priority,
            },
        )

    await session.flush()

    missing = referenced_ids - existing_vehicles.keys()
    if missing:
        raise IngestError(f"unknown vehicle external ids: {sorted(missing)}")

    for status_item in payload.statuses:
        vehicle = existing_vehicles[status_item.vehicle_external_id]
        status = await session.get(VehicleStatus, vehicle.id)
        if status is None:
            status = VehicleStatus(
                vehicle_id=vehicle.id,
                tenant_id=source.tenant_id,
                captured_at=status_item.captured_at,
            )
            session.add(status)
        elif status.captured_at > status_item.captured_at:
            continue

        _apply(
            status,
            {
                "captured_at": status_item.captured_at,
                "state": status_item.state,
                "battery_level": status_item.battery_level,
                "usable_battery_level": status_item.usable_battery_level,
                "range_km": status_item.range_km,
                "rated_range_km": status_item.rated_range_km,
                "odometer_km": status_item.odometer_km,
                "latitude": status_item.latitude,
                "longitude": status_item.longitude,
                "speed_kmh": status_item.speed_kmh,
                "power_kw": status_item.power_kw,
                "is_charging": status_item.is_charging,
                "charger_power_kw": status_item.charger_power_kw,
                "outside_temp_c": status_item.outside_temp_c,
                "inside_temp_c": status_item.inside_temp_c,
            },
        )

    existing_drives = await _load_existing(Drive, session, source.id, payload.drives)
    for drive_item in payload.drives:
        vehicle = existing_vehicles[drive_item.vehicle_external_id]
        drive = existing_drives.get(drive_item.external_id)
        if drive is None:
            drive = Drive(
                tenant_id=source.tenant_id,
                data_source_id=source.id,
                vehicle_id=vehicle.id,
                external_id=drive_item.external_id,
                start_date=drive_item.start_date,
            )
            session.add(drive)
            existing_drives[drive_item.external_id] = drive
        drive.vehicle_id = vehicle.id
        _apply(drive, _drive_values(drive_item))

    existing_charges = await _load_existing(Charge, session, source.id, payload.charges)
    for charge_item in payload.charges:
        vehicle = existing_vehicles[charge_item.vehicle_external_id]
        charge = existing_charges.get(charge_item.external_id)
        if charge is None:
            charge = Charge(
                tenant_id=source.tenant_id,
                data_source_id=source.id,
                vehicle_id=vehicle.id,
                external_id=charge_item.external_id,
                start_date=charge_item.start_date,
            )
            session.add(charge)
            existing_charges[charge_item.external_id] = charge
        charge.vehicle_id = vehicle.id
        _apply(charge, _charge_values(charge_item))

    source.last_seen_at = now
    if payload.source_version:
        source.source_version = payload.source_version
    if payload.schema_version:
        source.schema_version = payload.schema_version

    await session.commit()

    return AgentSyncOut(
        vehicles=len(payload.vehicles),
        statuses=len(payload.statuses),
        drives=len(payload.drives),
        charges=len(payload.charges),
        server_time=now,
    )


async def _load_existing[ModelT: (Drive, Charge)](
    model: type[ModelT],
    session: AsyncSession,
    source_id: uuid.UUID,
    items: Sequence[Any],
) -> dict[int, ModelT]:
    external_ids = {item.external_id for item in items}
    if not external_ids:
        return {}
    result = await session.scalars(
        select(model).where(
            model.data_source_id == source_id,
            model.external_id.in_(external_ids),
        )
    )
    return {row.external_id: row for row in result}


def _drive_values(item: AgentDriveIn) -> dict[str, object]:
    return {
        "start_date": item.start_date,
        "end_date": item.end_date,
        "distance_km": item.distance_km,
        "duration_min": item.duration_min,
        "start_address": item.start_address,
        "end_address": item.end_address,
        "start_geofence": item.start_geofence,
        "end_geofence": item.end_geofence,
        "start_latitude": item.start_latitude,
        "start_longitude": item.start_longitude,
        "end_latitude": item.end_latitude,
        "end_longitude": item.end_longitude,
        "start_battery_level": item.start_battery_level,
        "end_battery_level": item.end_battery_level,
        "start_range_km": item.start_range_km,
        "end_range_km": item.end_range_km,
        "outside_temp_avg_c": item.outside_temp_avg_c,
        "inside_temp_avg_c": item.inside_temp_avg_c,
        "speed_max_kmh": item.speed_max_kmh,
        "power_max_kw": item.power_max_kw,
        "power_min_kw": item.power_min_kw,
        "ascent_m": item.ascent_m,
        "descent_m": item.descent_m,
        "energy_kwh": item.energy_kwh,
        "efficiency_wh_km": item.efficiency_wh_km,
    }


def _charge_values(item: AgentChargeIn) -> dict[str, object]:
    return {
        "start_date": item.start_date,
        "end_date": item.end_date,
        "duration_min": item.duration_min,
        "energy_added_kwh": item.energy_added_kwh,
        "energy_used_kwh": item.energy_used_kwh,
        "start_battery_level": item.start_battery_level,
        "end_battery_level": item.end_battery_level,
        "start_range_km": item.start_range_km,
        "end_range_km": item.end_range_km,
        "cost": item.cost,
        "location": item.location,
        "geofence": item.geofence,
        "latitude": item.latitude,
        "longitude": item.longitude,
        "charger_type": item.charger_type,
        "max_power_kw": item.max_power_kw,
        "outside_temp_avg_c": item.outside_temp_avg_c,
    }


def _apply(instance: object, values: dict[str, object]) -> None:
    for key, value in values.items():
        setattr(instance, key, value)


def _apply_non_none(instance: object, values: dict[str, object]) -> None:
    for key, value in values.items():
        if value is not None:
            setattr(instance, key, value)
