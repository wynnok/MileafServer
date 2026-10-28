"""ST03 usage distribution over the single driving match set (#34 §5)."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import Depends, Query
from pydantic import BaseModel, Field

from mileaf.dependencies import get_now
from mileaf.errors import APIError, ErrorCode
from mileaf.queries.driving_query import (
    DRIVING_MATCH_METHOD,
    DrivingFilters,
    DrivingQueryEcho,
    driving_filters,
    match_source,
)
from mileaf.queries.parameters import list_parameters
from mileaf.queries.statistics_fields import STATISTICS_REQUIREMENTS
from mileaf.queries.time import validate_timezone

USAGE_METHOD: Literal["usage-distribution-v1"] = "usage-distribution-v1"
AnalysisStatus = Literal["available", "no_records", "insufficient_data", "unsupported"]
AnalysisView = Literal["usage"]

# Fixed [0,10) / [10,50) / [50,+inf) km groups: three bins, never data derived.
DISTANCE_BIN_EDGES_KM: tuple[float, ...] = (10.0, 50.0)
DISTANCE_BIN_COUNT = len(DISTANCE_BIN_EDGES_KM) + 1
WEEKDAY_COUNT = 7
MAX_RANGE_DAYS = 730
PERCENTILES: tuple[float, ...] = (0.5, 0.9)

# Declares every source column this feature reads. All of them also stay required by the
# legacy startup probe (ADR-0002 fails closed), so the declaration documents the dependency
# and keeps the unsupported path available to future optional columns instead of relaxing
# the probe; a schema without them refuses to start rather than reporting no_records.
STATISTICS_REQUIREMENTS["driving_analysis"] = {
    "drives": {
        "car_id": ("int2",),
        "start_date": ("timestamp",),
        "end_date": ("timestamp",),
        "distance": ("float8",),
        "duration_min": ("int2",),
        "outside_temp_avg": ("numeric",),
        "start_rated_range_km": ("numeric",),
        "end_rated_range_km": ("numeric",),
        "start_ideal_range_km": ("numeric",),
        "end_ideal_range_km": ("numeric",),
    },
    "cars": {"efficiency": ("float8",)},
    "settings": {"preferred_range": ("range", "preferred_range")},
}


def finite_sql(expression: str) -> str:
    """Trusted SQL expression; PostgreSQL orders NaN above infinity, so both fail."""
    return f"({expression} > '-Infinity'::float8 AND {expression} < 'Infinity'::float8)"


# One deterministic reason per excluded record, in priority order, so counts stay additive.
_DISTANCE_STEPS: tuple[tuple[str, str], ...] = (
    ("m.distance IS NULL", "missing_distance"),
    (f"NOT {finite_sql('m.distance')}", "invalid_distance"),
)
_DURATION_STEPS: tuple[tuple[str, str], ...] = (
    ("m.duration_min IS NULL", "missing_duration"),
    ("m.duration_min < 0", "negative_duration"),
)
DISTANCE_REASONS = (*_DISTANCE_STEPS, ("m.distance < 0", "negative_distance"))
DURATION_REASONS = _DURATION_STEPS
# Speed pairs a valid distance with a strictly positive recorded duration (#34 §2).
SPEED_REASONS = (*DISTANCE_REASONS, *_DURATION_STEPS, ("m.duration_min = 0", "zero_duration"))
# Paired consumption needs distance > 0 and an existing finite estimated net energy.
PAIRED_REASONS = (
    *_DISTANCE_STEPS,
    ("m.distance <= 0", "non_positive_distance"),
    ("m.energy_kwh IS NULL", "missing_energy"),
    (f"NOT {finite_sql('m.energy_kwh')}", "invalid_energy"),
)
USAGE_SETS: dict[str, tuple[tuple[str, str], ...]] = {
    "distance": DISTANCE_REASONS,
    "duration": DURATION_REASONS,
    "speed": SPEED_REASONS,
    "paired": PAIRED_REASONS,
}
USAGE_THRESHOLDS: dict[str, float] = {
    **{
        f"distance_bin_edge_{index + 1}_km": edge
        for index, edge in enumerate(DISTANCE_BIN_EDGES_KM)
    },
    "distance_bins": float(DISTANCE_BIN_COUNT),
    "weekday_bins": float(WEEKDAY_COUNT),
    "percentile_low": PERCENTILES[0] * 100,
    "percentile_high": PERCENTILES[1] * 100,
    "speed_duration_exclusive_min": 0.0,
    "paired_distance_exclusive_min_km": 0.0,
}


def reason_names(steps: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
    return tuple(reason for _, reason in steps)


def reason_counts_sql(steps: tuple[tuple[str, str], ...], column: str, alias: str) -> str:
    """Count every declared reason, including zeros, in one additive JSON object."""
    entries = ", ".join(
        f"'{reason}', count(*) FILTER (WHERE {column}='{reason}')" for _, reason in steps
    )
    return f"jsonb_build_object({entries}) AS {alias}"


@dataclass(frozen=True)
class DrivingAnalysisQuery:
    view: AnalysisView
    echo: DrivingQueryEcho
    start: datetime
    end: datetime
    as_of: datetime

    def evidence_filter(self, *, lower: float, upper: float | None) -> "DriveEvidenceFilter | None":
        """Inherit the whole analysis query plus one group's half-open distance bounds.

        Returns None when the inherited bounds already exclude the group, because the
        evidence list rejects a non-empty-looking range whose minimum reaches its maximum.
        """
        filters = self.echo.filters
        requested_minimum = filters.distance_min_km
        requested_maximum = filters.distance_max_km
        minimum = lower if requested_minimum is None else max(lower, requested_minimum)
        if upper is None:
            maximum = requested_maximum
        elif requested_maximum is None:
            maximum = upper
        else:
            maximum = min(upper, requested_maximum)
        if maximum is not None and minimum >= maximum:
            return None
        return DriveEvidenceFilter(
            from_=self.start,
            to=self.end,
            tz=self.echo.tz,
            distance_min_km=minimum,
            distance_max_km=maximum,
            outside_temp_min_c=filters.outside_temp_min_c,
            outside_temp_max_c=filters.outside_temp_max_c,
            energy_status=filters.energy_status,
        )


class DriveEvidenceFilter(BaseModel):
    """Query parameters accepted verbatim by GET /v1/vehicles/{id}/drives."""

    from_: datetime = Field(serialization_alias="from")
    to: datetime
    tz: str
    distance_min_km: float | None = None
    distance_max_km: float | None = None
    outside_temp_min_c: float | None = None
    outside_temp_max_c: float | None = None
    energy_status: Literal["known", "unknown"] | None = None
    completed_only: Literal[True] = True
    method: Literal["driving-match-v1"] = DRIVING_MATCH_METHOD
    version: int = 1


def driving_analysis_parameters(
    now: Annotated[datetime, Depends(get_now)],
    vehicle_id: int,
    view: AnalysisView,
    from_: Annotated[str, Query(alias="from")],
    to: str,
    tz: str = "UTC",
    distance_min_km: float | None = None,
    distance_max_km: float | None = None,
    outside_temp_min_c: float | None = None,
    outside_temp_max_c: float | None = None,
    energy_status: Literal["known", "unknown"] | None = None,
) -> DrivingAnalysisQuery:
    """Analysis windows are explicit, bounded and always clipped to the request moment."""
    bounds = list_parameters(from_, to)
    if bounds.from_ is None or bounds.to is None:
        raise APIError(400, ErrorCode.INVALID_REQUEST, "Driving analysis requires from and to")
    if bounds.to - bounds.from_ > timedelta(days=MAX_RANGE_DAYS):
        raise APIError(
            400,
            ErrorCode.INVALID_REQUEST,
            f"Driving analysis range exceeds {MAX_RANGE_DAYS} days",
        )
    end = min(bounds.to, now)
    if bounds.from_ >= end:
        raise APIError(400, ErrorCode.INVALID_REQUEST, "Invalid effective driving analysis range")
    filters: DrivingFilters = driving_filters(
        distance_min_km=distance_min_km,
        distance_max_km=distance_max_km,
        outside_temp_min_c=outside_temp_min_c,
        outside_temp_max_c=outside_temp_max_c,
        energy_status=energy_status,
        # Grouping only ever describes finished drives; in-progress rows stay evidence.
        completed_only=True,
    )
    return DrivingAnalysisQuery(
        view,
        DrivingQueryEcho(
            vehicle_id=vehicle_id,
            from_=bounds.from_,
            to=end,
            tz=validate_timezone(tz),
            filters=filters,
        ),
        bounds.from_,
        end,
        now,
    )


def scoped_sql(query: DrivingAnalysisQuery, vehicle_id: int) -> tuple[str, list[Any]]:
    """Classify the shared match set once; $10 is the validated IANA timezone."""
    matched, args = match_source(query.echo, vehicle_id)
    local = "(m.started_at AT TIME ZONE ($10))"
    reasons = ", ".join(
        "CASE "
        + " ".join(f"WHEN {condition} THEN '{reason}'" for condition, reason in steps)
        + f" END AS {name}_reason"
        for name, steps in USAGE_SETS.items()
    )
    bin_edges = "".join(
        f"WHEN c.distance < {edge} THEN {index} "
        for index, edge in enumerate(DISTANCE_BIN_EDGES_KM)
    )
    sql = (
        f"""/* ST03 usage distribution, independently written over the driving match set:
   local calendar attribution by started_at, fixed distance groups and ISO weekdays,
   recorded duration_min only (never the timestamp span), percentile_cont for P50/P90. */
WITH matched AS ({matched}), classified AS (
SELECT m.*, {local}::date AS local_date, extract(isodow FROM {local})::int AS weekday, {reasons}
FROM matched m), scoped AS (
SELECT c.*, (c.distance_reason IS NULL) AS valid_distance,
(c.duration_reason IS NULL) AS valid_duration, (c.speed_reason IS NULL) AS valid_speed,
(c.paired_reason IS NULL) AS valid_paired,
CASE WHEN c.distance_reason IS NOT NULL THEN NULL {bin_edges}"""
        f"ELSE {len(DISTANCE_BIN_EDGES_KM)} END AS distance_bin FROM classified c)"
    )
    return sql, [*args, query.echo.tz]


def usage_summary_sql(query: DrivingAnalysisQuery, vehicle_id: int) -> tuple[str, list[Any]]:
    scoped, args = scoped_sql(query, vehicle_id)
    percentiles = "".join(
        f"percentile_cont({fraction}) WITHIN GROUP (ORDER BY distance) "
        "FILTER (WHERE valid_distance) AS "
        f"distance_p{int(fraction * 100)}_km, "
        f"percentile_cont({fraction}) WITHIN GROUP (ORDER BY duration_min) "
        f"FILTER (WHERE valid_duration) AS duration_p{int(fraction * 100)}_min, "
        for fraction in PERCENTILES
    )
    reasons = ", ".join(
        reason_counts_sql(steps, f"{name}_reason", f"{name}_reasons")
        for name, steps in USAGE_SETS.items()
    )
    sql = (
        f"{scoped} SELECT count(*) AS candidate_count, "
        "count(DISTINCT local_date) AS active_days, "
        # The clipped upper bound reaches into the local day that has not ended yet.
        "($3::timestamptz > (date_trunc('day', $11::timestamptz AT TIME ZONE ($10)) "
        "AT TIME ZONE ($10))) AS partial_today, "
        "count(*) FILTER (WHERE valid_distance) AS distance_valid_count, "
        "sum(distance) FILTER (WHERE valid_distance) AS distance_sum_km, "
        "sum(distance) FILTER (WHERE valid_distance) "
        "/ nullif(count(DISTINCT local_date), 0) AS distance_per_active_day_km, "
        f"{percentiles}"
        "count(*) FILTER (WHERE valid_duration) AS duration_valid_count, "
        "sum(duration_min) FILTER (WHERE valid_duration) AS duration_sum_min, "
        "count(*) FILTER (WHERE valid_speed) AS speed_valid_count, "
        "sum(distance) FILTER (WHERE valid_speed) AS speed_distance_km, "
        "sum(duration_min) FILTER (WHERE valid_speed) AS speed_duration_min, "
        # Weighted over one batch: sum km * 60 / sum minutes, never a mean of trip speeds.
        "sum(distance) FILTER (WHERE valid_speed) * 60 "
        "/ nullif(sum(duration_min) FILTER (WHERE valid_speed), 0) AS mean_speed_kmh, "
        "count(*) FILTER (WHERE valid_paired) AS paired_valid_count, "
        "sum(distance) FILTER (WHERE valid_paired) AS paired_distance_km, "
        "sum(energy_kwh) FILTER (WHERE valid_paired) AS paired_energy_kwh, "
        f"{reasons} FROM scoped"
    )
    return sql, [*args, query.as_of]


def usage_groups_sql(query: DrivingAnalysisQuery, vehicle_id: int) -> tuple[str, list[Any]]:
    """One pass for both fixed groupings; absent groups are filled by the caller."""
    scoped, args = scoped_sql(query, vehicle_id)
    aggregates = ", ".join(
        [
            "count(*) AS drives",
            "count(*) FILTER (WHERE valid_distance) AS distance_valid_count",
            "sum(distance) FILTER (WHERE valid_distance) AS distance_sum_km",
            "count(*) FILTER (WHERE valid_duration) AS duration_valid_count",
            "sum(duration_min) FILTER (WHERE valid_duration) AS duration_sum_min",
            "count(*) FILTER (WHERE valid_paired) AS paired_valid_count",
            "sum(distance) FILTER (WHERE valid_paired) AS paired_distance_km",
            "sum(energy_kwh) FILTER (WHERE valid_paired) AS paired_energy_kwh",
            "sum(energy_kwh) FILTER (WHERE valid_paired) * 1000 "
            "/ nullif(sum(distance) FILTER (WHERE valid_paired), 0) AS consumption_wh_km",
        ]
        + [
            reason_counts_sql(steps, f"{name}_reason", f"{name}_reasons")
            for name, steps in USAGE_SETS.items()
        ]
    )
    sql = (
        f"{scoped} SELECT distance_bin, weekday, grouping(weekday) AS by_distance, {aggregates} "
        "FROM scoped GROUP BY GROUPING SETS ((distance_bin), (weekday))"
    )
    return sql, args
