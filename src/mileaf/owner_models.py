"""Public item schemas for owner endpoints; timestamps and units remain explicit."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class HistoryPoint(BaseModel):
    at: datetime
    source: Literal["position"]
    source_id: int


class BatteryHistoryPoint(HistoryPoint):
    soc_pct: float | None
    usable_soc_pct: float | None
    rated_range_km: float | None
    ideal_range_km: float | None


class EnvironmentHistoryPoint(HistoryPoint):
    inside_temp_c: float | None
    outside_temp_c: float | None


class ActivityEvent(BaseModel):
    event_id: str
    kind: Literal["drive", "charging_session", "state_segment"]
    state: Literal["online", "asleep", "offline"] | None
    record_id: int
    started_at: datetime
    ended_at: datetime | None
    intersection_start: datetime
    intersection_end: datetime
    status: Literal["completed", "ongoing"]


class MetricObservation(BaseModel):
    value: float | None
    unit: Literal["bar", "°C"]
    sampled_at: datetime | None
    source: Literal["position"]
    coverage: dict[str, int]
    status: Literal["available", "insufficient_data", "unsupported"]


class LatestObservation(BaseModel):
    source: Literal["position"]
    source_id: int
    sampled_at: datetime
    metrics: dict[str, MetricObservation]


class FirmwareUpdate(BaseModel):
    id: int
    version: str | None
    started_at: datetime
    ended_at: datetime | None
    status: Literal["completed", "ongoing"]
    previous_completed_at: datetime | None
    interval_seconds: float | None
    coverage: dict[str, bool]


class HabitCell(BaseModel):
    weekday: int
    hour: int
    sessions: int
    energy_valid: int
    energy_added_kwh: float | None
    coverage: dict[str, dict[str, int]]


class GridCell(BaseModel):
    count: int
    bounds: list[float]
    center: dict[str, float]


class SpeedBin(BaseModel):
    from_kmh: int
    to_kmh: int | None
    seconds: float


class DestinationLocation(BaseModel):
    location_id: str
    visits: int
    distance_km: float | None
    distance_valid: int
    duration_min: float | None
    duration_valid: int
    last_visited_at: datetime
    representative_drive_id: int
    rank: int
    location: dict[str, str | float | None]
    coordinate_source: Literal["latest_destination_position"]
    coverage: dict[str, dict[str, int]]
