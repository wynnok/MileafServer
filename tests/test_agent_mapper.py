from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from mileaf.agent.mapper import map_charge, map_drive, map_status, map_vehicle


def test_map_vehicle_uses_fallbacks() -> None:
    vehicle = map_vehicle(
        {
            "id": 7,
            "name": None,
            "marketing_name": "Model Y",
            "model": "Y",
            "vin": "VIN",
            "trim_badging": None,
            "exterior_color": None,
            "wheel_type": None,
            "efficiency": Decimal("0.153"),
            "display_priority": 2,
        }
    )
    assert vehicle.external_id == 7
    assert vehicle.name == "Model Y"
    assert vehicle.efficiency == 0.153


def test_map_status_normalizes_units_and_naive_timestamps() -> None:
    status = map_status(
        {
            "car_id": 1,
            "captured_at": datetime(2026, 10, 20, 10, 0, 0),
            "battery_level": 80,
            "usable_battery_level": 78,
            "est_battery_range_km": Decimal("350.5"),
            "rated_battery_range_km": Decimal("400.0"),
            "odometer": 12345.6,
            "latitude": Decimal("31.2304"),
            "longitude": Decimal("121.4737"),
            "speed": 0,
            "power": -11000,
            "outside_temp": Decimal("22.5"),
            "inside_temp": Decimal("23.0"),
            "state": "charging",
            "charging_process_id": 9,
            "charger_power_kw": 11.0,
        }
    )
    assert status.captured_at.tzinfo == UTC
    assert status.range_km == 350.5
    assert status.power_kw == -11.0
    assert status.is_charging is True
    assert status.outside_temp_c == 22.5


def test_map_drive_computes_energy_and_efficiency() -> None:
    drive = map_drive(
        {
            "id": 101,
            "car_id": 1,
            "start_date": datetime(2026, 10, 19, 8, 0, 0),
            "end_date": datetime(2026, 10, 19, 8, 30, 0),
            "distance": Decimal("25.0"),
            "duration_min": 30,
            "outside_temp_avg": Decimal("18.0"),
            "inside_temp_avg": Decimal("20.0"),
            "speed_max": 110,
            "power_max": 150000,
            "power_min": -30000,
            "start_ideal_range_km": Decimal("370.0"),
            "end_ideal_range_km": Decimal("340.0"),
            "start_rated_range_km": Decimal("360.0"),
            "end_rated_range_km": Decimal("328.0"),
            "start_km": 100.0,
            "end_km": 125.0,
            "ascent": 30,
            "descent": 10,
            "start_geofence": "Home",
            "end_geofence": None,
            "start_address": "Home",
            "end_address": "Office",
            "start_latitude": Decimal("31.2"),
            "start_longitude": Decimal("121.4"),
            "end_latitude": Decimal("31.3"),
            "end_longitude": Decimal("121.5"),
            "start_battery_level": 90,
            "end_battery_level": 82,
        },
        0.153,
    )
    assert drive.start_date.tzinfo == UTC
    assert drive.energy_kwh == 4.896
    assert drive.efficiency_wh_km == 195.8
    assert drive.power_max_kw == 150.0
    assert drive.power_min_kw == -30.0
    assert drive.start_range_km == 360.0


def test_map_charge_prefers_geofence_and_detects_dc() -> None:
    charge = map_charge(
        {
            "id": 201,
            "car_id": 1,
            "start_date": datetime(2026, 10, 18, 22, 0, 0),
            "end_date": datetime(2026, 10, 18, 23, 0, 0),
            "duration_min": 60,
            "charge_energy_added": Decimal("10.0"),
            "charge_energy_used": Decimal("10.5"),
            "start_battery_level": 60,
            "end_battery_level": 80,
            "start_ideal_range_km": Decimal("250.0"),
            "end_ideal_range_km": Decimal("330.0"),
            "start_rated_range_km": Decimal("240.0"),
            "end_rated_range_km": Decimal("320.0"),
            "outside_temp_avg": Decimal("21.0"),
            "cost": Decimal("12.50"),
            "geofence": "Supercharger",
            "location": "Some address",
            "latitude": Decimal("31.2"),
            "longitude": Decimal("121.4"),
            "max_power_kw": 120.0,
            "fast_charge": True,
        }
    )
    assert charge.start_date.tzinfo == UTC
    assert charge.location == "Supercharger"
    assert charge.charger_type == "DC"
    assert charge.cost == 12.5
    assert charge.energy_added_kwh == 10.0
