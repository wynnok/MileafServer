"""Optional TeslaMate columns and the versioned charging capability declaration."""

from typing import Literal

from pydantic import BaseModel

# API field -> (source column, accepted PostgreSQL types).
SAMPLE_FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "power": ("charger_power", ("int2",)),
    "soc": ("battery_level", ("int2",)),
    "voltage_v": ("charger_voltage", ("int2",)),
    "actual_current_a": ("charger_actual_current", ("int2",)),
    "pilot_current_a": ("charger_pilot_current", ("int2",)),
    "outside_temp_c": ("outside_temp", ("numeric",)),
    "rated_range_km": ("rated_battery_range_km", ("numeric",)),
    "ideal_range_km": ("ideal_battery_range_km", ("numeric",)),
    "charge_energy_added_kwh": ("charge_energy_added", ("numeric",)),
    **{
        name: (name, ("bool",))
        for name in (
            "battery_heater",
            "battery_heater_on",
            "battery_heater_no_power",
            "not_enough_power_to_heat",
        )
    },
}
HEATER_FIELDS = tuple(name for name, (_, types) in SAMPLE_FIELDS.items() if types == ("bool",))
CHARGER_FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "fast_charger_present": ("fast_charger_present", ("bool",)),
    **{
        name: (name, ("varchar", "text"))
        for name in ("fast_charger_brand", "fast_charger_type", "conn_charge_cable")
    },
}
RANGE_FIELDS = (
    "start_rated_range_km",
    "end_rated_range_km",
    "start_ideal_range_km",
    "end_ideal_range_km",
)
OPTIONAL_CHARGING_COLUMNS: dict[str, dict[str, tuple[str, ...]]] = {
    "charges": {
        column: types for column, types in (*SAMPLE_FIELDS.values(), *CHARGER_FIELDS.values())
    },
    "charging_processes": {name: ("numeric",) for name in RANGE_FIELDS},
}


class ChargingCapabilities(BaseModel):
    contract_version: Literal["charging-experience-v1"] = "charging-experience-v1"
    minimum_server_version: Literal["0.2.0"] = "0.2.0"
    tested_schema_version: int = 20260808090000
    fields: dict[str, bool]


def charging_capabilities(columns: frozenset[tuple[str, str]]) -> ChargingCapabilities:
    fields = {
        name: ("charges", column) in columns
        for name, (column, _) in (SAMPLE_FIELDS | CHARGER_FIELDS).items()
    }
    fields.update({name: ("charging_processes", name) in columns for name in RANGE_FIELDS})
    return ChargingCapabilities(fields=fields)
