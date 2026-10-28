"""ST03 usage rhythm, quantiles and fixed distance/weekday groups (#34 §5)."""

import json
from datetime import datetime
from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_serializer
from starlette.responses import Response

from mileaf.cache import conditional_response
from mileaf.database import fetch_rows
from mileaf.dependencies import get_pool
from mileaf.errors import APIError, ErrorCode
from mileaf.queries.driving_analysis import (
    DISTANCE_BIN_COUNT,
    DISTANCE_BIN_EDGES_KM,
    USAGE_METHOD,
    USAGE_SETS,
    USAGE_THRESHOLDS,
    WEEKDAY_COUNT,
    AnalysisStatus,
    DriveEvidenceFilter,
    DrivingAnalysisQuery,
    driving_analysis_parameters,
    reason_names,
    temperature_groups_sql,
    usage_groups_sql,
    usage_summary_sql,
)
from mileaf.queries.driving_query import DrivingQueryEcho
from mileaf.queries.parameters import ResourceRoute
from mileaf.queries.statistics_fields import statistics_feature_supported
from mileaf.vehicles import VehicleIdentity, get_vehicle_or_404

router = APIRouter(prefix="/v1", tags=["driving analysis"], route_class=ResourceRoute)


class SetCoverage(BaseModel):
    """One valid set: candidates, valid inputs and additive exclusion reasons."""

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


class UsageCoverage(BaseModel):
    candidate_count: int
    distance: SetCoverage
    duration: SetCoverage
    speed: SetCoverage
    paired: SetCoverage


class DistanceBinCoverage(BaseModel):
    distance: SetCoverage
    duration: SetCoverage
    paired: SetCoverage


class WeekdayBinCoverage(BaseModel):
    distance: SetCoverage
    duration: SetCoverage


class DistanceBin(BaseModel):
    lower_km: float
    upper_km: float | None = Field(description="Exclusive upper bound in km; null is unbounded")
    drives: int
    distance_km: float
    duration_min: float | None
    paired_energy_kwh: float | None
    paired_distance_km: float | None
    consumption_wh_km: float | None
    status: AnalysisStatus
    coverage: DistanceBinCoverage
    evidence_filter: DriveEvidenceFilter | None

    @field_serializer("evidence_filter")
    def serialize_evidence_filter(self, value: DriveEvidenceFilter | None) -> dict[str, Any] | None:
        # Unset filters stay absent so the object can be replayed as query parameters.
        return value.model_dump(mode="json", by_alias=True, exclude_none=True) if value else None


class WeekdayBin(BaseModel):
    weekday: int = Field(description="ISO weekday of the local started_at, Monday=1 .. Sunday=7")
    drives: int
    distance_km: float | None
    duration_min: float | None
    status: AnalysisStatus
    coverage: WeekdayBinCoverage


class DrivingAnalysisUsage(BaseModel):
    view: Literal["usage"] = "usage"
    status: AnalysisStatus
    method: Literal["usage-distribution-v1"] = USAGE_METHOD
    version: int = 1
    validation: Literal["provisional"] = "provisional"
    thresholds: dict[str, float] = Field(default_factory=lambda: dict(USAGE_THRESHOLDS))
    query: DrivingQueryEcho
    as_of: datetime
    active_days: int
    partial_today: bool
    driving_distance_km: float | None
    driving_duration_min: float | None
    distance_per_active_day_km: float | None
    mean_speed_kmh: float | None
    distance_p50_km: float | None
    distance_p90_km: float | None
    duration_p50_min: float | None
    duration_p90_min: float | None
    coverage: UsageCoverage
    distance_bins: list[DistanceBin]
    weekday_bins: list[WeekdayBin]


def empty_row(**values: Any) -> dict[str, Any]:
    """The bounded skeleton of a group or window without any candidate record."""
    return {
        **{name: 0 for name in ("drives", "candidate_count", "active_days")},
        "partial_today": False,
        **{
            key: None
            for key in (
                "distance_sum_km",
                "duration_sum_min",
                "distance_per_active_day_km",
                "mean_speed_kmh",
                "paired_energy_kwh",
                "paired_distance_km",
                "consumption_wh_km",
                "distance_p50_km",
                "distance_p90_km",
                "duration_p50_min",
                "duration_p90_min",
            )
        },
        **{f"{name}_valid_count": 0 for name in USAGE_SETS},
        **{
            f"{name}_reasons": json.dumps(dict.fromkeys(reason_names(steps), 0))
            for name, steps in USAGE_SETS.items()
        },
        **values,
    }


def set_coverage(row: dict[str, Any], name: str, candidates: int) -> SetCoverage:
    valid = int(row[f"{name}_valid_count"])
    reasons = json.loads(row[f"{name}_reasons"])
    return SetCoverage(
        candidate_count=candidates,
        valid_count=valid,
        excluded_count=candidates - valid,
        candidate_distance_km=additive(row.get("candidate_distance_km"), candidates),
        valid_distance_km=additive(row.get(f"{name}_valid_distance_km"), candidates),
        candidate_duration_min=additive(row.get("candidate_duration_min"), candidates),
        valid_duration_min=additive(row.get(f"{name}_valid_duration_min"), candidates),
        unknown_distance_count=int(row.get("unknown_distance_count", 0)),
        unknown_duration_count=int(row.get("unknown_duration_count", 0)),
        excluded_by_reason={reason: int(reasons[reason]) for _, reason in USAGE_SETS[name]},
    )


def additive(value: Any, candidates: int) -> Any:
    """No candidates keeps additive terms at zero; candidates without inputs stay null."""
    return value if candidates else 0


def group_status(candidates: int, row: dict[str, Any]) -> AnalysisStatus:
    if not candidates:
        return "no_records"
    if not (int(row["distance_valid_count"]) or int(row["duration_valid_count"])):
        return "insufficient_data"
    return "available"


def bin_bounds(index: int) -> tuple[float, float | None]:
    lower = 0.0 if index == 0 else DISTANCE_BIN_EDGES_KM[index - 1]
    upper = DISTANCE_BIN_EDGES_KM[index] if index < len(DISTANCE_BIN_EDGES_KM) else None
    return lower, upper


def distance_bins(
    query: DrivingAnalysisQuery, rows: dict[int, dict[str, Any]]
) -> list[DistanceBin]:
    bins = []
    for index in range(DISTANCE_BIN_COUNT):
        row = rows.get(index, empty_row())
        drives = int(row["drives"])
        lower, upper = bin_bounds(index)
        bins.append(
            DistanceBin(
                lower_km=lower,
                upper_km=upper,
                drives=drives,
                distance_km=additive(row["distance_sum_km"], drives),
                duration_min=additive(row["duration_sum_min"], drives),
                paired_energy_kwh=additive(row["paired_energy_kwh"], drives),
                paired_distance_km=additive(row["paired_distance_km"], drives),
                consumption_wh_km=row["consumption_wh_km"],
                status=group_status(drives, row),
                coverage=DistanceBinCoverage(
                    distance=set_coverage(row, "distance", drives),
                    duration=set_coverage(row, "duration", drives),
                    paired=set_coverage(row, "paired", drives),
                ),
                evidence_filter=query.evidence_filter(lower=lower, upper=upper),
            )
        )
    return bins


def weekday_bins(rows: dict[int, dict[str, Any]]) -> list[WeekdayBin]:
    bins = []
    for weekday in range(1, WEEKDAY_COUNT + 1):
        row = rows.get(weekday, empty_row())
        drives = int(row["drives"])
        bins.append(
            WeekdayBin(
                weekday=weekday,
                drives=drives,
                distance_km=additive(row["distance_sum_km"], drives),
                duration_min=additive(row["duration_sum_min"], drives),
                status=group_status(drives, row),
                coverage=WeekdayBinCoverage(
                    distance=set_coverage(row, "distance", drives),
                    duration=set_coverage(row, "duration", drives),
                ),
            )
        )
    return bins


def usage_result(
    query: DrivingAnalysisQuery,
    summary: dict[str, Any] | None,
    groups: list[dict[str, Any]],
    *,
    supported: bool,
) -> DrivingAnalysisUsage:
    row = summary if summary is not None else empty_row()
    candidates = int(row["candidate_count"])
    distance_rows = {
        int(group["distance_bin"]): group
        for group in groups
        if group["by_distance"] and group["distance_bin"] is not None
    }
    weekday_rows = {int(group["weekday"]): group for group in groups if not group["by_distance"]}
    status: AnalysisStatus = "available"
    if not supported:
        status = "unsupported"
    elif not candidates:
        status = "no_records"
    elif not (int(row["distance_valid_count"]) or int(row["duration_valid_count"])):
        status = "insufficient_data"
    return DrivingAnalysisUsage(
        status=status,
        query=query.echo,
        as_of=query.as_of,
        active_days=int(row["active_days"]),
        partial_today=bool(row["partial_today"]),
        driving_distance_km=additive(row["distance_sum_km"], candidates),
        driving_duration_min=additive(row["duration_sum_min"], candidates),
        distance_per_active_day_km=row["distance_per_active_day_km"],
        mean_speed_kmh=row["mean_speed_kmh"],
        distance_p50_km=row["distance_p50_km"],
        distance_p90_km=row["distance_p90_km"],
        duration_p50_min=row["duration_p50_min"],
        duration_p90_min=row["duration_p90_min"],
        coverage=UsageCoverage(
            candidate_count=candidates,
            distance=set_coverage(row, "distance", candidates),
            duration=set_coverage(row, "duration", candidates),
            speed=set_coverage(row, "speed", candidates),
            paired=set_coverage(row, "paired", candidates),
        ),
        distance_bins=distance_bins(query, distance_rows),
        weekday_bins=weekday_bins(weekday_rows),
    )


class TemperatureBin(BaseModel):
    lower_c: float
    upper_c: float
    drives: int
    distance_km: float | None
    paired_energy_kwh: float | None
    paired_distance_km: float | None
    consumption_wh_km: float | None
    median_drive_distance_km: float | None
    mean_trip_speed_kmh: float | None
    coverage: UsageCoverage
    evidence_filter: dict[str, Any]
    sample_status: Literal["adequate", "sparse", "insufficient_data"]


class DrivingAnalysisConditions(BaseModel):
    view: Literal["conditions"] = "conditions"
    status: AnalysisStatus
    method: Literal["temperature-association-v1"] = "temperature-association-v1"
    version: int = 1
    validation: Literal["provisional"] = "provisional"
    thresholds: dict[str, float] = Field(
        default_factory=lambda: {
            "temperature_step_c": 5.0,
            "minimum_paired_drives": 5.0,
            "minimum_paired_distance_km": 50.0,
        }
    )
    query: DrivingQueryEcho
    as_of: datetime
    summary: DrivingAnalysisUsage
    temperature_bins: list[TemperatureBin]
    distance_bins: list[DistanceBin]
    unknown_temperature_drives: int


@router.get(
    "/vehicles/{vehicle_id}/driving-analysis",
    response_model=DrivingAnalysisUsage | DrivingAnalysisConditions,
)
async def driving_analysis(
    request: Request,
    vehicle: Annotated[VehicleIdentity, Depends(get_vehicle_or_404)],
    params: Annotated[DrivingAnalysisQuery, Depends(driving_analysis_parameters)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> Response:
    supported = statistics_feature_supported(
        request.app.state.compatibility.statistics_columns, "driving_analysis"
    )
    summary: dict[str, Any] | None = None
    groups: list[dict[str, Any]] = []
    temperatures: list[dict[str, Any]] = []
    if supported:
        summary_sql, summary_args = usage_summary_sql(params, vehicle.id)
        groups_sql, groups_args = usage_groups_sql(params, vehicle.id)
        async with pool.acquire() as db:
            summary = dict((await fetch_rows(db, summary_sql, *summary_args))[0])
            if params.view == "conditions":
                temp_sql, temp_args = temperature_groups_sql(params, vehicle.id)
                combined = await fetch_rows(
                    db,
                    "SELECT (SELECT jsonb_agg(g) FROM ("
                    + groups_sql
                    + ") g) AS groups, (SELECT jsonb_agg(t) FROM ("
                    + temp_sql
                    + ") t) AS temperatures",
                    *temp_args,
                )
                groups = json.loads(combined[0]["groups"] or "[]")
                temperatures = json.loads(combined[0]["temperatures"] or "[]")
                for row in groups + temperatures:
                    for name in USAGE_SETS:
                        row[f"{name}_reasons"] = json.dumps(row[f"{name}_reasons"])
            else:
                groups = [dict(group) for group in await fetch_rows(db, groups_sql, *groups_args)]
    result: DrivingAnalysisUsage | DrivingAnalysisConditions = usage_result(
        params, summary, groups, supported=supported
    )
    if params.view == "conditions":
        bins = []
        unknown = 0
        for row in temperatures:
            count = int(row["drives"])
            if row["lower_c"] is None:
                unknown += count
                continue
            lower = float(row["lower_c"])
            filters = params.echo.filters.model_dump(exclude_none=True)
            filters["outside_temp_min_c"] = max(lower, filters.get("outside_temp_min_c", lower))
            filters["outside_temp_max_c"] = min(
                lower + 5, filters.get("outside_temp_max_c", lower + 5)
            )
            evidence = {
                "from": params.start.isoformat(),
                "to": params.end.isoformat(),
                "tz": params.echo.tz,
                **filters,
            }
            valid = int(row["paired_valid_count"])
            bins.append(
                TemperatureBin(
                    lower_c=lower,
                    upper_c=lower + 5,
                    drives=count,
                    distance_km=row["distance_sum_km"],
                    paired_energy_kwh=row["paired_energy_kwh"],
                    paired_distance_km=row["paired_distance_km"],
                    consumption_wh_km=row["consumption_wh_km"],
                    median_drive_distance_km=row["median_drive_distance_km"],
                    mean_trip_speed_kmh=row["mean_trip_speed_kmh"],
                    coverage=UsageCoverage(
                        candidate_count=count,
                        **{name: set_coverage(row, name, count) for name in USAGE_SETS},
                    ),
                    evidence_filter=evidence,
                    sample_status="adequate"
                    if valid >= 5 and row["paired_distance_km"] >= 50
                    else "sparse"
                    if valid
                    else "insufficient_data",
                )
            )
        if len(bins) > 100:
            raise APIError(
                400, ErrorCode.INVALID_REQUEST, "Too many temperature groups; narrow the range"
            )
        result = DrivingAnalysisConditions(
            status=result.status,
            query=params.echo,
            as_of=params.as_of,
            summary=result,
            temperature_bins=bins,
            distance_bins=result.distance_bins,
            unknown_temperature_drives=unknown,
        )
    return conditional_response(
        request,
        result.model_dump_json(by_alias=True).encode(),
        ttl=30,
        validator_body=result.model_dump_json(
            by_alias=True,
            exclude={"as_of": True, "summary": {"as_of": True}}
            if isinstance(result, DrivingAnalysisConditions)
            else {"as_of"},
        ).encode(),
    )
