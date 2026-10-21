from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=120)
    tenant_name: str | None = Field(default=None, max_length=120)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshIn(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserOut(ORMModel):
    id: uuid.UUID
    email: EmailStr
    display_name: str | None
    created_at: datetime


class TenantOut(ORMModel):
    id: uuid.UUID
    name: str
    created_at: datetime


class TenantMembershipOut(TenantOut):
    role: str


class AuthResponse(TokenPair):
    user: UserOut
    tenants: list[TenantMembershipOut]


class StatusOut(ORMModel):
    captured_at: datetime
    state: str | None
    battery_level: int | None
    usable_battery_level: int | None
    range_km: float | None
    rated_range_km: float | None
    odometer_km: float | None
    latitude: float | None
    longitude: float | None
    speed_kmh: int | None
    power_kw: float | None
    is_charging: bool
    charger_power_kw: float | None
    outside_temp_c: float | None
    inside_temp_c: float | None
    updated_at: datetime


class VehicleOut(ORMModel):
    id: uuid.UUID
    name: str
    model: str
    marketing_name: str | None
    trim_badging: str | None
    exterior_color: str | None
    wheel_type: str | None
    efficiency: float | None
    display_priority: int
    status: StatusOut | None = None


class DriveOut(ORMModel):
    id: uuid.UUID
    start_date: datetime
    end_date: datetime | None
    distance_km: float | None
    duration_min: int | None
    start_address: str | None
    end_address: str | None
    start_geofence: str | None
    end_geofence: str | None
    start_latitude: float | None
    start_longitude: float | None
    end_latitude: float | None
    end_longitude: float | None
    start_battery_level: int | None
    end_battery_level: int | None
    start_range_km: float | None
    end_range_km: float | None
    outside_temp_avg_c: float | None
    inside_temp_avg_c: float | None
    speed_max_kmh: int | None
    power_max_kw: float | None
    power_min_kw: float | None
    ascent_m: int | None
    descent_m: int | None
    energy_kwh: float | None
    efficiency_wh_km: float | None


class ChargeOut(ORMModel):
    id: uuid.UUID
    start_date: datetime
    end_date: datetime | None
    duration_min: int | None
    energy_added_kwh: float | None
    energy_used_kwh: float | None
    start_battery_level: int | None
    end_battery_level: int | None
    start_range_km: float | None
    end_range_km: float | None
    cost: float | None
    location: str | None
    geofence: str | None
    latitude: float | None
    longitude: float | None
    charger_type: str | None
    max_power_kw: float | None
    outside_temp_avg_c: float | None


class StatsOut(BaseModel):
    period: str
    period_start: datetime | None
    period_end: datetime
    drive_count: int
    distance_km: float
    drive_energy_kwh: float
    efficiency_wh_km: float | None
    charge_count: int
    charge_energy_kwh: float
    charge_cost: float
    avg_drive_distance_km: float | None


class SummaryOut(BaseModel):
    vehicle: VehicleOut
    status: StatusOut | None
    stats: StatsOut


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None


class AgentPairingCodeOut(BaseModel):
    code: str
    expires_at: datetime


class AgentPairIn(BaseModel):
    code: str = Field(min_length=4, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    source_version: str | None = Field(default=None, max_length=80)
    schema_version: str | None = Field(default=None, max_length=80)


class AgentPairOut(BaseModel):
    agent_token: str
    tenant_id: uuid.UUID
    data_source_id: uuid.UUID


class AgentHeartbeatIn(BaseModel):
    source_version: str | None = Field(default=None, max_length=80)
    schema_version: str | None = Field(default=None, max_length=80)


class DataSourceOut(ORMModel):
    id: uuid.UUID
    name: str
    status: str
    source_version: str | None
    schema_version: str | None
    last_seen_at: datetime | None
    created_at: datetime


class AgentVehicleIn(BaseModel):
    external_id: int
    name: str = Field(max_length=120)
    model: str = Field(max_length=40)
    vin: str | None = Field(default=None, max_length=32)
    marketing_name: str | None = Field(default=None, max_length=120)
    trim_badging: str | None = Field(default=None, max_length=80)
    exterior_color: str | None = Field(default=None, max_length=80)
    wheel_type: str | None = Field(default=None, max_length=80)
    efficiency: float | None = None
    display_priority: int | None = None

    model_config = ConfigDict(extra="ignore")


class AgentStatusIn(BaseModel):
    vehicle_external_id: int
    captured_at: datetime
    state: str | None = None
    battery_level: int | None = None
    usable_battery_level: int | None = None
    range_km: float | None = None
    rated_range_km: float | None = None
    odometer_km: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    speed_kmh: int | None = None
    power_kw: float | None = None
    is_charging: bool = False
    charger_power_kw: float | None = None
    outside_temp_c: float | None = None
    inside_temp_c: float | None = None

    model_config = ConfigDict(extra="ignore")


class AgentDriveIn(BaseModel):
    external_id: int
    vehicle_external_id: int
    start_date: datetime
    end_date: datetime | None = None
    distance_km: float | None = None
    duration_min: int | None = None
    start_address: str | None = Field(default=None, max_length=500)
    end_address: str | None = Field(default=None, max_length=500)
    start_geofence: str | None = Field(default=None, max_length=120)
    end_geofence: str | None = Field(default=None, max_length=120)
    start_latitude: float | None = None
    start_longitude: float | None = None
    end_latitude: float | None = None
    end_longitude: float | None = None
    start_battery_level: int | None = None
    end_battery_level: int | None = None
    start_range_km: float | None = None
    end_range_km: float | None = None
    outside_temp_avg_c: float | None = None
    inside_temp_avg_c: float | None = None
    speed_max_kmh: int | None = None
    power_max_kw: float | None = None
    power_min_kw: float | None = None
    ascent_m: int | None = None
    descent_m: int | None = None
    energy_kwh: float | None = None
    efficiency_wh_km: float | None = None

    model_config = ConfigDict(extra="ignore")


class AgentChargeIn(BaseModel):
    external_id: int
    vehicle_external_id: int
    start_date: datetime
    end_date: datetime | None = None
    duration_min: int | None = None
    energy_added_kwh: float | None = None
    energy_used_kwh: float | None = None
    start_battery_level: int | None = None
    end_battery_level: int | None = None
    start_range_km: float | None = None
    end_range_km: float | None = None
    cost: float | None = None
    location: str | None = Field(default=None, max_length=500)
    geofence: str | None = Field(default=None, max_length=120)
    latitude: float | None = None
    longitude: float | None = None
    charger_type: Literal["AC", "DC"] | None = None
    max_power_kw: float | None = None
    outside_temp_avg_c: float | None = None

    model_config = ConfigDict(extra="ignore")


class AgentSyncIn(BaseModel):
    source_version: str | None = Field(default=None, max_length=80)
    schema_version: str | None = Field(default=None, max_length=80)
    vehicles: list[AgentVehicleIn] = Field(default_factory=list, max_length=1000)
    statuses: list[AgentStatusIn] = Field(default_factory=list, max_length=1000)
    drives: list[AgentDriveIn] = Field(default_factory=list, max_length=1000)
    charges: list[AgentChargeIn] = Field(default_factory=list, max_length=1000)

    model_config = ConfigDict(extra="ignore")


class AgentSyncOut(BaseModel):
    vehicles: int
    statuses: int
    drives: int
    charges: int
    server_time: datetime
