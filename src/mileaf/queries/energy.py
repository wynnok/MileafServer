"""Unrounded energy and net consumption expressions for aggregation and drive DTOs."""

from mileaf.probes import CAPABILITIES
from mileaf.units import distance_km_sql

CAPABILITIES["drive_energy"] = {
    "drives": {
        "start_rated_range_km": ("numeric",),
        "end_rated_range_km": ("numeric",),
        "start_ideal_range_km": ("numeric",),
        "end_ideal_range_km": ("numeric",),
        "distance": ("float8",),
    },
    "cars": {"efficiency": ("float8",)},
    "settings": {"preferred_range": ("range", "preferred_range")},
}


def energy_kwh_sql(*, drive: str = "d", car: str = "c", settings: str = "s") -> str:
    """Arguments are trusted aliases, never values taken from query parameters."""
    difference = (
        f"(CASE WHEN {settings}.preferred_range::text='ideal' "
        f"THEN {drive}.start_ideal_range_km - {drive}.end_ideal_range_km "
        f"ELSE {drive}.start_rated_range_km - {drive}.end_rated_range_km END)"
    )
    return (
        "(/* TeslaMate v4.3.0 drives / overview: preferred range delta (km) * "
        "cars.efficiency (kWh/km); non-positive delta or missing efficiency => NULL. "
        "Independently rewritten, net energy; no rounding before aggregation. */ "
        f"CASE WHEN {difference} > 0 AND {car}.efficiency IS NOT NULL "
        f"THEN {difference} * {car}.efficiency END)"
    )


def consumption_wh_km_sql(*, drive: str = "d", car: str = "c", settings: str = "s") -> str:
    energy = energy_kwh_sql(drive=drive, car=car, settings=settings)
    distance = distance_km_sql(f"{drive}.distance")
    return (
        "(/* TeslaMate drives: net kWh / normalized km * 1000 => Wh/km; "
        "non-positive distance => NULL. */ "
        f"CASE WHEN {distance} > 0 THEN {energy} / {distance} * 1000 END)"
    )
