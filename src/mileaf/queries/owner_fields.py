"""Optional, independently probed owner experience sources (TeslaMate metric storage)."""

from typing import Literal

from pydantic import BaseModel

POSITION_FIELDS = {
    "soc_pct": ("battery_level", "int2"),
    "usable_soc_pct": ("usable_battery_level", "int2"),
    "rated_range_km": ("rated_battery_range_km", "numeric"),
    "ideal_range_km": ("ideal_battery_range_km", "numeric"),
    "inside_temp_c": ("inside_temp", "numeric"),
    "outside_temp_c": ("outside_temp", "numeric"),
    "driver_temp_setting_c": ("driver_temp_setting", "numeric"),
    **{
        f"tpms_{wheel}_bar": (f"tpms_pressure_{wheel}", "numeric")
        for wheel in ("fl", "fr", "rl", "rr")
    },
    "power_kw": ("power", "int2"),
}
OWNER_REQUIREMENTS = {
    "battery_history": {
        "positions": {
            column: (kind,)
            for name, (column, kind) in POSITION_FIELDS.items()
            if name in ("soc_pct", "usable_soc_pct", "rated_range_km", "ideal_range_km")
        }
    },
    "environment_history": {
        "positions": {"inside_temp": ("numeric",), "outside_temp": ("numeric",)}
    },
    "vehicle_observations": {
        "positions": {
            column: (kind,)
            for name, (column, kind) in POSITION_FIELDS.items()
            if name.endswith(("_c", "_bar"))
        }
    },
    "activity_timeline": {"states": {"end_date": ("timestamp",)}},
    "driving_locations": {"drives": {"end_geofence_id": ("int4",)}},
    "visited_areas": {"drives": {"end_position_id": ("int4",)}},
    "charging_habits": {
        "charging_processes": {"start_battery_level": ("int2",), "end_battery_level": ("int2",)}
    },
    "software_updates": {
        "updates": {
            "id": ("int4",),
            "car_id": ("int2",),
            "start_date": ("timestamp",),
            "end_date": ("timestamp",),
            "version": ("varchar",),
        }
    },
    "drive_series": {
        "positions": {
            column: (kind,)
            for name, (column, kind) in POSITION_FIELDS.items()
            if name in {"power_kw", "inside_temp_c", "outside_temp_c"} or name.endswith("_bar")
        },
    },
    "drive_elevation": {"drives": {"ascent": ("int2",), "descent": ("int2",)}},
    "speed_histogram": {"positions": {"speed": ("int2",)}},
}


def owner_supported(columns: frozenset[tuple[str, str]], feature: str) -> bool:
    return all(
        (table, column) in columns
        for table, fields in OWNER_REQUIREMENTS[feature].items()
        for column in fields
    )


class OwnerCapabilities(BaseModel):
    contract_version: Literal["owner-experience-v1"] = "owner-experience-v1"
    minimum_server_version: Literal["0.4.0"] = "0.4.0"
    features: dict[str, bool]


def owner_capabilities(columns: frozenset[tuple[str, str]]) -> OwnerCapabilities:
    return OwnerCapabilities(
        features={name: owner_supported(columns, name) for name in OWNER_REQUIREMENTS}
    )
