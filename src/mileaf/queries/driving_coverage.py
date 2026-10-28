"""Independent coverage sets for the legacy driving statistics match window."""

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from mileaf.queries.energy import energy_kwh_sql
from mileaf.queries.statistics_fields import STATISTICS_REQUIREMENTS
from mileaf.queries.time import Bucket, bucket_sql

# Declares the source columns this feature reads. drives.duration_min is the only one the
# legacy statistics query did not need; it stays a required column of the startup probe
# (ADR-0002 fails closed), so this declaration documents the dependency and keeps the
# unsupported path available to future optional columns instead of relaxing the probe.
STATISTICS_REQUIREMENTS["driving_coverage"] = {
    "drives": {"duration_min": ("int2", "int4")},
}

Status = Literal["available", "no_records", "insufficient_data", "unsupported"]

# One deterministic reason per excluded record, so the counts stay additive.
EXCLUSION_REASONS: dict[str, tuple[str, ...]] = {
    "distance": ("missing_distance", "invalid_distance"),
    "net_energy": ("missing_energy", "invalid_energy"),
    "duration": ("missing_duration", "invalid_duration"),
    "paired": (
        "missing_distance",
        "invalid_distance",
        "non_positive_distance",
        "missing_energy",
        "invalid_energy",
    ),
}


class MetricCoverage(BaseModel):
    candidate_count: int
    valid_count: int
    excluded_count: int
    candidate_distance_km: float | None
    valid_distance_km: float | None
    candidate_duration_min: float | None
    valid_duration_min: float | None
    unknown_distance_count: int
    unknown_duration_count: int
    excluded_by_reason: dict[str, int]


class DrivingCoverage(BaseModel):
    status: Status
    method: Literal["driving-coverage-v1"] = "driving-coverage-v1"
    version: int = 1
    thresholds: dict[str, float] = Field(default_factory=dict)
    completed_drives: int
    ongoing_drives: int
    distance_known_drives: int
    distance_known_km: float | None
    net_energy_known_drives: int
    net_energy_known_distance_km: float | None
    duration_known_drives: int
    duration_known_min: float | None
    distance: MetricCoverage
    net_energy: MetricCoverage
    duration: MetricCoverage
    excluded_by_reason: dict[str, dict[str, int]]


class PairedNetConsumption(BaseModel):
    status: Status
    method: Literal["paired-net-consumption-v1"] = "paired-net-consumption-v1"
    version: int = 1
    thresholds: dict[str, float] = Field(default_factory=lambda: {"distance_exclusive_min_km": 0.0})
    value_wh_km: float | None
    numerator_energy_kwh: float | None
    denominator_distance_km: float | None
    eligible_drives: int
    excluded_drives: int
    coverage: MetricCoverage


class ComparisonQuality(BaseModel):
    status: Status
    sample_status: Literal["adequate", "insufficient_data", "unsupported"]
    method: Literal["paired-comparison-quality-v1"] = "paired-comparison-quality-v1"
    version: int = 1
    thresholds: dict[str, float] = Field(
        default_factory=lambda: {"minimum_drives": 5, "minimum_distance_coverage": 0.8}
    )
    validation: Literal["provisional"] = "provisional"
    distance_coverage: float | None
    eligible_drives: int
    unknown_distance_drives: int
    invalid_distance_drives: int


class DrivingQueryEcho(BaseModel):
    vehicle_id: int
    from_: datetime = Field(serialization_alias="from")
    to: datetime
    tz: str
    bucket: Bucket
    filters: dict[str, str] = Field(default_factory=dict)
    time_basis: Literal["started_at"] = "started_at"


def finite_sql(value: str) -> str:
    """Trusted SQL expression; PostgreSQL orders NaN above infinity."""
    return f"({value} > '-Infinity'::float8 AND {value} < 'Infinity'::float8)"


def driving_coverage_sql(bucket: Bucket) -> str:
    """Aggregate in PostgreSQL; only one bounded row per bucket plus total is returned."""
    local_bucket = bucket_sql(bucket, timestamp="d.start_date AT TIME ZONE 'UTC'", tz="$4")
    valid_distance = f"({finite_sql('distance')} AND distance>=0)"
    valid_energy = finite_sql("energy")
    rules = {
        "distance": "CASE WHEN distance IS NULL THEN 'missing_distance' "
        f"WHEN NOT {valid_distance} THEN 'invalid_distance' END",
        "net_energy": "CASE WHEN energy IS NULL THEN 'missing_energy' "
        f"WHEN NOT {valid_energy} THEN 'invalid_energy' END",
        "duration": "CASE WHEN duration IS NULL THEN 'missing_duration' "
        "WHEN duration<0 THEN 'invalid_duration' END",
        "paired": "CASE WHEN distance IS NULL THEN 'missing_distance' "
        f"WHEN NOT {valid_distance} THEN 'invalid_distance' "
        "WHEN distance<=0 THEN 'non_positive_distance' "
        "WHEN energy IS NULL THEN 'missing_energy' "
        f"WHEN NOT {valid_energy} THEN 'invalid_energy' END",
    }
    fields = {
        "completed_drives": "count(*) FILTER (WHERE completed)",
        "ongoing_drives": "count(*) FILTER (WHERE NOT completed)",
        # Known means non-null and finite; validity is counted separately so negative
        # source facts stay visible instead of being mistaken for comparable coverage.
        "distance_known_drives": f"count(*) FILTER (WHERE completed AND {finite_sql('distance')})",
        "distance_known_km": f"sum(distance) FILTER (WHERE completed AND {finite_sql('distance')})",
        "net_energy_known_drives": f"count(*) FILTER (WHERE completed AND {valid_energy})",
        "net_energy_known_distance_km": "sum(distance) FILTER "
        f"(WHERE completed AND {valid_energy} AND {finite_sql('distance')})",
        "duration_known_drives": "count(*) FILTER (WHERE completed AND duration IS NOT NULL)",
        "duration_known_min": "sum(duration) FILTER (WHERE completed)",
        "unknown_distance_count": "count(*) FILTER (WHERE completed AND NOT "
        f"coalesce({finite_sql('distance')}, false))",
        "unknown_duration_count": "count(*) FILTER (WHERE completed AND duration IS NULL)",
    }
    for metric, rule in rules.items():
        valid = f"completed AND ({rule}) IS NULL"
        fields[f"{metric}_valid_count"] = f"count(*) FILTER (WHERE {valid})"
        fields[f"{metric}_distance_km"] = (
            f"sum(distance) FILTER (WHERE {valid} AND {valid_distance})"
        )
        fields[f"{metric}_duration_min"] = f"sum(duration) FILTER (WHERE {valid} AND duration>=0)"
        counts = ", ".join(
            f"'{reason}',count(*) FILTER (WHERE completed AND ({rule})='{reason}')"
            for reason in EXCLUSION_REASONS[metric]
        )
        fields[f"{metric}_excluded_by_reason"] = f"jsonb_build_object({counts})"
    fields["paired_energy_kwh"] = (
        f"sum(energy) FILTER (WHERE completed AND ({rules['paired']}) IS NULL)"
    )
    projected = ", ".join(f"{expr} AS {name}" for name, expr in fields.items())
    return (
        "WITH source AS (SELECT "
        f"{local_bucket} AS bucket_start, d.end_date IS NOT NULL AS completed, "
        f"d.distance, d.duration_min AS duration, {energy_kwh_sql()} AS energy "
        "FROM drives d JOIN cars c ON c.id=d.car_id CROSS JOIN settings s "
        "WHERE d.car_id=$1 AND d.start_date AT TIME ZONE 'UTC'>=$2::timestamptz "
        "AND d.start_date AT TIME ZONE 'UTC'<$3::timestamptz) "
        "SELECT bucket_start, grouping(bucket_start)=1 AS is_total, "
        "(SELECT preferred_range::text FROM settings LIMIT 1) AS range_kind, "
        f"{projected} FROM source GROUP BY GROUPING SETS ((bucket_start),())"
    )


def driving_quality(row: dict[str, Any], *, supported: bool) -> dict[str, Any]:
    """Map one aggregate row onto the three additive quality families of a period."""
    count = row.get("completed_drives", 0)

    def summed(name: str) -> float | None:
        value = row.get(name)
        return float(value) if value is not None else (0 if count == 0 else None)

    def reasons(metric: str) -> dict[str, int]:
        raw = row.get(f"{metric}_excluded_by_reason")
        counts = json.loads(raw) if isinstance(raw, str) else (raw or {})
        return {name: int(counts.get(name, 0)) for name in EXCLUSION_REASONS[metric]}

    def coverage(metric: str) -> MetricCoverage:
        valid = row.get(f"{metric}_valid_count", 0)
        return MetricCoverage(
            candidate_count=count,
            valid_count=valid,
            excluded_count=count - valid,
            candidate_distance_km=summed("distance_known_km"),
            valid_distance_km=summed(f"{metric}_distance_km"),
            candidate_duration_min=summed("duration_known_min"),
            valid_duration_min=summed(f"{metric}_duration_min"),
            unknown_distance_count=row.get("unknown_distance_count", 0),
            unknown_duration_count=row.get("unknown_duration_count", 0),
            excluded_by_reason=reasons(metric),
        )

    status: Status = "unsupported" if not supported else "available" if count else "no_records"
    distance = coverage("distance")
    energy = coverage("net_energy")
    duration = coverage("duration")
    paired = coverage("paired")
    eligible = paired.valid_count
    numerator = summed("paired_energy_kwh")
    denominator = paired.valid_distance_km
    # Paired distance over the valid known distance of the same candidates; a null or
    # zero denominator can never reach the threshold.
    comparable = distance.valid_distance_km
    distance_coverage = (
        denominator / comparable
        if denominator is not None and comparable is not None and comparable > 0
        else None
    )
    adequate = eligible >= 5 and distance_coverage is not None and distance_coverage >= 0.8
    return {
        "driving_coverage": DrivingCoverage(
            status=status,
            completed_drives=count,
            ongoing_drives=row.get("ongoing_drives", 0),
            distance_known_drives=row.get("distance_known_drives", 0),
            distance_known_km=summed("distance_known_km"),
            net_energy_known_drives=row.get("net_energy_known_drives", 0),
            net_energy_known_distance_km=summed("net_energy_known_distance_km"),
            duration_known_drives=row.get("duration_known_drives", 0),
            duration_known_min=summed("duration_known_min"),
            distance=distance,
            net_energy=energy,
            duration=duration,
            excluded_by_reason={
                "distance": distance.excluded_by_reason,
                "net_energy": energy.excluded_by_reason,
                "duration": duration.excluded_by_reason,
            },
        ),
        "net_consumption_paired": PairedNetConsumption(
            status=(
                status
                if not supported or not count
                else "available"
                if eligible
                else "insufficient_data"
            ),
            value_wh_km=(
                numerator * 1000 / denominator if numerator is not None and denominator else None
            ),
            numerator_energy_kwh=numerator,
            denominator_distance_km=denominator,
            eligible_drives=eligible,
            excluded_drives=count - eligible,
            coverage=paired,
        ),
        "comparison_quality": ComparisonQuality(
            status=status,
            sample_status=(
                "unsupported" if not supported else "adequate" if adequate else "insufficient_data"
            ),
            distance_coverage=distance_coverage,
            eligible_drives=eligible,
            unknown_distance_drives=row.get("unknown_distance_count", 0),
            invalid_distance_drives=distance.excluded_by_reason.get("invalid_distance", 0),
        ),
    }
