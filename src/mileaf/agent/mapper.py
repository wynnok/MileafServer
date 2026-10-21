from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from mileaf.schemas import AgentChargeIn, AgentDriveIn, AgentStatusIn, AgentVehicleIn

DEFAULT_EFFICIENCY_KWH_PER_KM = 0.153


def map_vehicle(row: Mapping[str, Any]) -> AgentVehicleIn:
    model = str(row["model"])
    name = row["name"] or row["marketing_name"] or f"Model {model}"
    return AgentVehicleIn(
        external_id=int(row["id"]),
        name=_text(name, 120) or f"Model {model}",
        model=_text(model, 40) or "unknown",
        vin=_text(row["vin"], 32),
        marketing_name=_text(row["marketing_name"], 120),
        trim_badging=_text(row["trim_badging"], 80),
        exterior_color=_text(row["exterior_color"], 80),
        wheel_type=_text(row["wheel_type"], 80),
        efficiency=_float(row["efficiency"]),
        display_priority=_int(row["display_priority"]),
    )


def map_status(row: Mapping[str, Any]) -> AgentStatusIn:
    range_km = _first_float(
        row["est_battery_range_km"],
        row["rated_battery_range_km"],
    )
    power = _float(row["power"])
    return AgentStatusIn(
        vehicle_external_id=int(row["car_id"]),
        captured_at=_datetime(row["captured_at"]),
        state=row["state"],
        battery_level=_int(row["battery_level"]),
        usable_battery_level=_int(row["usable_battery_level"]),
        range_km=range_km,
        rated_range_km=_float(row["rated_battery_range_km"]),
        odometer_km=_float(row["odometer"]),
        latitude=_float(row["latitude"]),
        longitude=_float(row["longitude"]),
        speed_kmh=_int(row["speed"]),
        power_kw=round(power / 1000, 3) if power is not None else None,
        is_charging=row["charging_process_id"] is not None,
        charger_power_kw=_float(row["charger_power_kw"]),
        outside_temp_c=_float(row["outside_temp"]),
        inside_temp_c=_float(row["inside_temp"]),
    )


def map_drive(row: Mapping[str, Any], car_efficiency: float | None = None) -> AgentDriveIn:
    start_range = _first_float(row["start_rated_range_km"], row["start_ideal_range_km"])
    end_range = _first_float(row["end_rated_range_km"], row["end_ideal_range_km"])
    distance = _float(row["distance"])
    efficiency = car_efficiency or DEFAULT_EFFICIENCY_KWH_PER_KM
    energy_kwh: float | None = None
    efficiency_wh_km: float | None = None
    if start_range is not None and end_range is not None and distance and distance > 0:
        range_diff = start_range - end_range
        if range_diff > 0:
            energy_kwh = range_diff * efficiency
            efficiency_wh_km = energy_kwh * 1000 / distance

    return AgentDriveIn(
        external_id=int(row["id"]),
        vehicle_external_id=int(row["car_id"]),
        start_date=_datetime(row["start_date"]),
        end_date=_datetime(row["end_date"]),
        distance_km=distance,
        duration_min=_int(row["duration_min"]),
        start_address=_text(row["start_address"], 500),
        end_address=_text(row["end_address"], 500),
        start_geofence=_text(row["start_geofence"], 120),
        end_geofence=_text(row["end_geofence"], 120),
        start_latitude=_float(row["start_latitude"]),
        start_longitude=_float(row["start_longitude"]),
        end_latitude=_float(row["end_latitude"]),
        end_longitude=_float(row["end_longitude"]),
        start_battery_level=_int(row["start_battery_level"]),
        end_battery_level=_int(row["end_battery_level"]),
        start_range_km=start_range,
        end_range_km=end_range,
        outside_temp_avg_c=_float(row["outside_temp_avg"]),
        inside_temp_avg_c=_float(row["inside_temp_avg"]),
        speed_max_kmh=_int(row["speed_max"]),
        power_max_kw=_kilowatts(row["power_max"]),
        power_min_kw=_kilowatts(row["power_min"]),
        ascent_m=_int(row["ascent"]),
        descent_m=_int(row["descent"]),
        energy_kwh=round(energy_kwh, 3) if energy_kwh is not None else None,
        efficiency_wh_km=round(efficiency_wh_km, 1) if efficiency_wh_km is not None else None,
    )


def map_charge(row: Mapping[str, Any]) -> AgentChargeIn:
    start_range = _first_float(row["start_rated_range_km"], row["start_ideal_range_km"])
    end_range = _first_float(row["end_rated_range_km"], row["end_ideal_range_km"])
    fast_charge = row["fast_charge"]
    charger_type = "DC" if fast_charge else ("AC" if fast_charge is False else None)
    geofence = row["geofence"]
    location = geofence or row["location"]

    return AgentChargeIn(
        external_id=int(row["id"]),
        vehicle_external_id=int(row["car_id"]),
        start_date=_datetime(row["start_date"]),
        end_date=_datetime(row["end_date"]),
        duration_min=_int(row["duration_min"]),
        energy_added_kwh=_float(row["charge_energy_added"]),
        energy_used_kwh=_float(row["charge_energy_used"]),
        start_battery_level=_int(row["start_battery_level"]),
        end_battery_level=_int(row["end_battery_level"]),
        start_range_km=start_range,
        end_range_km=end_range,
        cost=_float(row["cost"]),
        location=_text(location, 500),
        geofence=_text(geofence, 120),
        latitude=_float(row["latitude"]),
        longitude=_float(row["longitude"]),
        charger_type=charger_type,
        max_power_kw=_float(row["max_power_kw"]),
        outside_temp_avg_c=_float(row["outside_temp_avg"]),
    )


def _datetime(value: datetime | None) -> datetime:
    if value is None:
        raise ValueError("required datetime is missing")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)  # type: ignore[arg-type]


def _int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)  # type: ignore[call-overload]


def _first_float(*values: object) -> float | None:
    for value in values:
        if value is not None:
            return float(value)  # type: ignore[arg-type]
    return None


def _kilowatts(value: object) -> float | None:
    if value is None:
        return None
    return round(float(value) / 1000, 3)  # type: ignore[arg-type]


def _text(value: object, limit: int) -> str | None:
    if value is None:
        return None
    return str(value)[:limit]
