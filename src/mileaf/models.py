from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mileaf.db import Base, UTCDateTime


def utcnow() -> datetime:
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    vehicles: Mapped[list[Vehicle]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    data_sources: Mapped[list[DataSource]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )


class Membership(Base):
    __tablename__ = "memberships"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(20), default="owner")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    user: Mapped[User] = relationship(back_populates="memberships")
    tenant: Mapped[Tenant] = relationship(back_populates="memberships")


class DataSource(Base):
    __tablename__ = "data_sources"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(40), default="teslamate_agent")
    status: Mapped[str] = mapped_column(String(20), default="active")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    source_version: Mapped[str | None] = mapped_column(String(80))
    schema_version: Mapped[str | None] = mapped_column(String(80))
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    tenant: Mapped[Tenant] = relationship(back_populates="data_sources")
    vehicles: Mapped[list[Vehicle]] = relationship(
        back_populates="data_source", cascade="all, delete-orphan"
    )


class PairingCode(Base):
    __tablename__ = "pairing_codes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    code_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Vehicle(Base):
    __tablename__ = "vehicles"
    __table_args__ = (
        UniqueConstraint("data_source_id", "external_id", name="uq_vehicles_source_external"),
        Index("ix_vehicles_tenant", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_sources.id", ondelete="CASCADE")
    )
    external_id: Mapped[int] = mapped_column(Integer)
    vin: Mapped[str | None] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(120))
    model: Mapped[str] = mapped_column(String(40))
    marketing_name: Mapped[str | None] = mapped_column(String(120))
    trim_badging: Mapped[str | None] = mapped_column(String(80))
    exterior_color: Mapped[str | None] = mapped_column(String(80))
    wheel_type: Mapped[str | None] = mapped_column(String(80))
    efficiency: Mapped[float | None] = mapped_column(Float)
    display_priority: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    tenant: Mapped[Tenant] = relationship(back_populates="vehicles")
    data_source: Mapped[DataSource] = relationship(back_populates="vehicles")
    status: Mapped[VehicleStatus | None] = relationship(
        back_populates="vehicle", cascade="all, delete-orphan", uselist=False
    )
    drives: Mapped[list[Drive]] = relationship(
        back_populates="vehicle", cascade="all, delete-orphan"
    )
    charges: Mapped[list[Charge]] = relationship(
        back_populates="vehicle", cascade="all, delete-orphan"
    )


class VehicleStatus(Base):
    __tablename__ = "vehicle_statuses"

    vehicle_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vehicles.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime)
    state: Mapped[str | None] = mapped_column(String(20))
    battery_level: Mapped[int | None] = mapped_column(Integer)
    usable_battery_level: Mapped[int | None] = mapped_column(Integer)
    range_km: Mapped[float | None] = mapped_column(Float)
    rated_range_km: Mapped[float | None] = mapped_column(Float)
    odometer_km: Mapped[float | None] = mapped_column(Float)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    speed_kmh: Mapped[int | None] = mapped_column(Integer)
    power_kw: Mapped[float | None] = mapped_column(Float)
    is_charging: Mapped[bool] = mapped_column(Boolean, default=False)
    charger_power_kw: Mapped[float | None] = mapped_column(Float)
    outside_temp_c: Mapped[float | None] = mapped_column(Float)
    inside_temp_c: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    vehicle: Mapped[Vehicle] = relationship(back_populates="status")


class Drive(Base):
    __tablename__ = "drives"
    __table_args__ = (
        UniqueConstraint("data_source_id", "external_id", name="uq_drives_source_external"),
        Index("ix_drives_tenant_vehicle_start", "tenant_id", "vehicle_id", "start_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"))
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_sources.id", ondelete="CASCADE")
    )
    external_id: Mapped[int] = mapped_column(Integer)
    start_date: Mapped[datetime] = mapped_column(UTCDateTime)
    end_date: Mapped[datetime | None] = mapped_column(UTCDateTime)
    distance_km: Mapped[float | None] = mapped_column(Float)
    duration_min: Mapped[int | None] = mapped_column(Integer)
    start_address: Mapped[str | None] = mapped_column(String(500))
    end_address: Mapped[str | None] = mapped_column(String(500))
    start_geofence: Mapped[str | None] = mapped_column(String(120))
    end_geofence: Mapped[str | None] = mapped_column(String(120))
    start_latitude: Mapped[float | None] = mapped_column(Float)
    start_longitude: Mapped[float | None] = mapped_column(Float)
    end_latitude: Mapped[float | None] = mapped_column(Float)
    end_longitude: Mapped[float | None] = mapped_column(Float)
    start_battery_level: Mapped[int | None] = mapped_column(Integer)
    end_battery_level: Mapped[int | None] = mapped_column(Integer)
    start_range_km: Mapped[float | None] = mapped_column(Float)
    end_range_km: Mapped[float | None] = mapped_column(Float)
    outside_temp_avg_c: Mapped[float | None] = mapped_column(Float)
    inside_temp_avg_c: Mapped[float | None] = mapped_column(Float)
    speed_max_kmh: Mapped[int | None] = mapped_column(Integer)
    power_max_kw: Mapped[float | None] = mapped_column(Float)
    power_min_kw: Mapped[float | None] = mapped_column(Float)
    ascent_m: Mapped[int | None] = mapped_column(Integer)
    descent_m: Mapped[int | None] = mapped_column(Integer)
    energy_kwh: Mapped[float | None] = mapped_column(Float)
    efficiency_wh_km: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    vehicle: Mapped[Vehicle] = relationship(back_populates="drives")


class Charge(Base):
    __tablename__ = "charges"
    __table_args__ = (
        UniqueConstraint("data_source_id", "external_id", name="uq_charges_source_external"),
        Index("ix_charges_tenant_vehicle_start", "tenant_id", "vehicle_id", "start_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"))
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_sources.id", ondelete="CASCADE")
    )
    external_id: Mapped[int] = mapped_column(Integer)
    start_date: Mapped[datetime] = mapped_column(UTCDateTime)
    end_date: Mapped[datetime | None] = mapped_column(UTCDateTime)
    duration_min: Mapped[int | None] = mapped_column(Integer)
    energy_added_kwh: Mapped[float | None] = mapped_column(Float)
    energy_used_kwh: Mapped[float | None] = mapped_column(Float)
    start_battery_level: Mapped[int | None] = mapped_column(Integer)
    end_battery_level: Mapped[int | None] = mapped_column(Integer)
    start_range_km: Mapped[float | None] = mapped_column(Float)
    end_range_km: Mapped[float | None] = mapped_column(Float)
    cost: Mapped[float | None] = mapped_column(Float)
    location: Mapped[str | None] = mapped_column(String(500))
    geofence: Mapped[str | None] = mapped_column(String(120))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    charger_type: Mapped[str | None] = mapped_column(String(20))
    max_power_kw: Mapped[float | None] = mapped_column(Float)
    outside_temp_avg_c: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    vehicle: Mapped[Vehicle] = relationship(back_populates="charges")
