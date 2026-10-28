"""Charging coverage and paired denominators, shared by buckets and location groups."""

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, field_validator


class UnitCost(BaseModel):
    value: float | None
    numerator_cost: float
    denominator_kwh: float
    eligible_sessions: int
    excluded_cost_sessions: int


def session_unit_cost(cost: Decimal | None, energy: Decimal | None) -> UnitCost:
    if cost is not None and energy is not None and energy > 0:
        return UnitCost(
            value=float(cost / energy),
            numerator_cost=float(cost),
            denominator_kwh=float(energy),
            eligible_sessions=1,
            excluded_cost_sessions=0,
        )
    return UnitCost(
        value=None,
        numerator_cost=0,
        denominator_kwh=0,
        eligible_sessions=0,
        excluded_cost_sessions=int(cost is not None),
    )


class ChargingMetrics(BaseModel):
    sessions: int
    energy_added_kwh: float
    cost: float | None
    cost_per_kwh: float | None
    ac_energy_kwh: float
    dc_energy_kwh: float
    dc_energy_share: float | None
    energy_known_sessions: int
    cost_known_sessions: int
    cost_known_energy_kwh: float
    duration_min_total: float
    duration_known_sessions: int
    classification_known_sessions: int
    unknown_energy_kwh: float
    ac_duration_min: float
    dc_duration_min: float
    unknown_duration_min: float
    unit_cost_added: UnitCost
    unit_cost_grid: UnitCost

    @field_validator("unit_cost_added", "unit_cost_grid", mode="before")
    @classmethod
    def parse_unit_cost(cls, value: Any) -> Any:
        return UnitCost.model_validate_json(value) if isinstance(value, str) else value


def charging_metrics_sql() -> dict[str, str]:
    # cost_per_kwh deliberately retains the previously published unpaired numerator.
    metrics = {
        "sessions": "count(*)",
        "energy_added_kwh": "coalesce(sum(energy),0)",
        "cost": "CASE WHEN count(*)=0 THEN 0 ELSE sum(cost) END",
        "cost_per_kwh": "sum(cost) / nullif(sum(energy) FILTER (WHERE cost IS NOT NULL),0)",
        "dc_energy_share": (
            "coalesce(sum(energy) FILTER (WHERE is_dc IS TRUE),0) / "
            "nullif(sum(energy) FILTER (WHERE is_dc IS NOT NULL),0)"
        ),
        "energy_known_sessions": "count(energy)",
        "cost_known_sessions": "count(cost)",
        "cost_known_energy_kwh": "coalesce(sum(energy) FILTER (WHERE cost IS NOT NULL),0)",
        "duration_min_total": "coalesce(sum(duration),0)",
        "duration_known_sessions": "count(duration)",
        "classification_known_sessions": "count(is_dc)",
    }
    for name, condition in (("ac", "IS FALSE"), ("dc", "IS TRUE"), ("unknown", "IS NULL")):
        metrics[f"{name}_energy_kwh"] = f"coalesce(sum(energy) FILTER (WHERE is_dc {condition}),0)"
        metrics[f"{name}_duration_min"] = (
            f"coalesce(sum(duration) FILTER (WHERE is_dc {condition}),0)"
        )
    for kind, energy in (("added", "energy"), ("grid", "used")):
        condition = f"cost IS NOT NULL AND {energy}>0"
        numerator = f"coalesce(sum(cost) FILTER (WHERE {condition}),0)"
        denominator = f"coalesce(sum({energy}) FILTER (WHERE {condition}),0)"
        metrics[f"unit_cost_{kind}"] = (
            f"jsonb_build_object('value', {numerator}/nullif({denominator},0), "
            f"'numerator_cost', {numerator}, 'denominator_kwh', {denominator}, "
            f"'eligible_sessions', count(*) FILTER (WHERE {condition}), "
            f"'excluded_cost_sessions', count(*) FILTER "
            f"(WHERE cost IS NOT NULL AND ({energy} IS NULL OR {energy}<=0)))"
        )
    return metrics
