from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import asyncpg


class TeslaMateSourceError(Exception):
    """Raised when the local TeslaMate database cannot be read."""


MIN_SUPPORTED_SCHEMA_VERSION = 20250613133700


def ensure_supported_schema(version: str | None) -> None:
    if not version:
        return
    try:
        numeric = int(version)
    except ValueError:
        return
    if numeric < MIN_SUPPORTED_SCHEMA_VERSION:
        raise TeslaMateSourceError(
            "TeslaMate schema is too old; upgrade TeslaMate to 1.30 or newer"
        )


class TeslaMateSource:
    """Read-only access to a self-hosted TeslaMate PostgreSQL database."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def __aenter__(self) -> TeslaMateSource:
        await self.connect()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def connect(self) -> None:
        if self._pool is None:
            try:
                self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=2)
            except (OSError, ValueError, asyncpg.PostgresError) as exc:
                raise TeslaMateSourceError("cannot connect to the TeslaMate database") from exc

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def schema_version(self) -> str | None:
        pool = self._require_pool()
        try:
            row = await pool.fetchrow("SELECT max(version)::text AS version FROM schema_migrations")
        except asyncpg.UndefinedTableError as exc:
            raise TeslaMateSourceError("schema_migrations table not found") from exc
        except asyncpg.PostgresError as exc:
            raise TeslaMateSourceError("cannot read TeslaMate schema version") from exc
        return row["version"] if row else None

    async def fetch_vehicles(self) -> list[Mapping[str, Any]]:
        return await self._fetch(
            """
            SELECT id, eid, vid, vin, name, model, efficiency,
                   trim_badging, marketing_name, exterior_color, wheel_type,
                   spoiler_type, display_priority
            FROM cars
            ORDER BY id
            """
        )

    async def fetch_statuses(self) -> list[Mapping[str, Any]]:
        return await self._fetch(
            """
            SELECT DISTINCT ON (p.car_id)
                p.car_id,
                p.date AS captured_at,
                p.battery_level,
                p.usable_battery_level,
                p.est_battery_range_km,
                p.rated_battery_range_km,
                p.odometer,
                p.latitude,
                p.longitude,
                p.speed,
                p.power,
                p.outside_temp,
                p.inside_temp,
                s.state,
                cp.id AS charging_process_id,
                ch.charger_power AS charger_power_kw
            FROM positions p
            LEFT JOIN LATERAL (
                SELECT state
                FROM states
                WHERE car_id = p.car_id AND end_date IS NULL
                ORDER BY start_date DESC
                LIMIT 1
            ) s ON TRUE
            LEFT JOIN LATERAL (
                SELECT id
                FROM charging_processes
                WHERE car_id = p.car_id AND end_date IS NULL
                ORDER BY start_date DESC
                LIMIT 1
            ) cp ON TRUE
            LEFT JOIN LATERAL (
                SELECT charger_power
                FROM charges
                WHERE charging_process_id = cp.id
                ORDER BY date DESC
                LIMIT 1
            ) ch ON TRUE
            ORDER BY p.car_id, p.date DESC
            """
        )

    async def fetch_drives(self, since: datetime | None = None) -> list[Mapping[str, Any]]:
        return await self._fetch(
            """
            SELECT
                d.id, d.car_id, d.start_date, d.end_date, d.distance, d.duration_min,
                d.outside_temp_avg, d.inside_temp_avg, d.speed_max, d.power_max, d.power_min,
                d.start_ideal_range_km, d.end_ideal_range_km,
                d.start_rated_range_km, d.end_rated_range_km,
                d.start_km, d.end_km, d.ascent, d.descent,
                sg.name AS start_geofence,
                eg.name AS end_geofence,
                COALESCE(
                    sa.display_name, sa.name,
                    NULLIF(CONCAT_WS(' ', sa.road, sa.house_number), ''),
                    sa.city, sa.neighbourhood
                ) AS start_address,
                COALESCE(
                    ea.display_name, ea.name,
                    NULLIF(CONCAT_WS(' ', ea.road, ea.house_number), ''),
                    ea.city, ea.neighbourhood
                ) AS end_address,
                sp.latitude AS start_latitude,
                sp.longitude AS start_longitude,
                sp.battery_level AS start_battery_level,
                ep.latitude AS end_latitude,
                ep.longitude AS end_longitude,
                ep.battery_level AS end_battery_level
            FROM drives d
            LEFT JOIN geofences sg ON sg.id = d.start_geofence_id
            LEFT JOIN geofences eg ON eg.id = d.end_geofence_id
            LEFT JOIN addresses sa ON sa.id = d.start_address_id
            LEFT JOIN addresses ea ON ea.id = d.end_address_id
            LEFT JOIN positions sp ON sp.id = d.start_position_id
            LEFT JOIN positions ep ON ep.id = d.end_position_id
            WHERE d.end_date IS NOT NULL
              AND ($1::timestamp IS NULL OR d.start_date >= $1::timestamp)
            ORDER BY d.start_date ASC, d.id ASC
            """,
            _naive_utc(since),
        )

    async def fetch_charges(self, since: datetime | None = None) -> list[Mapping[str, Any]]:
        return await self._fetch(
            """
            SELECT
                cp.id, cp.car_id, cp.start_date, cp.end_date, cp.duration_min,
                cp.charge_energy_added, cp.charge_energy_used,
                cp.start_battery_level, cp.end_battery_level,
                cp.start_ideal_range_km, cp.end_ideal_range_km,
                cp.start_rated_range_km, cp.end_rated_range_km,
                cp.outside_temp_avg, cp.cost,
                g.name AS geofence,
                COALESCE(
                    a.display_name, a.name,
                    NULLIF(CONCAT_WS(' ', a.road, a.house_number), ''),
                    a.city, a.neighbourhood
                ) AS location,
                p.latitude, p.longitude,
                agg.max_power_kw,
                agg.fast_charge
            FROM charging_processes cp
            LEFT JOIN geofences g ON g.id = cp.geofence_id
            LEFT JOIN addresses a ON a.id = cp.address_id
            LEFT JOIN positions p ON p.id = cp.position_id
            LEFT JOIN LATERAL (
                SELECT max(charger_power) AS max_power_kw,
                       bool_or(fast_charger_present) AS fast_charge
                FROM charges c
                WHERE c.charging_process_id = cp.id
            ) agg ON TRUE
            WHERE cp.end_date IS NOT NULL
              AND ($1::timestamp IS NULL OR cp.start_date >= $1::timestamp)
            ORDER BY cp.start_date ASC, cp.id ASC
            """,
            _naive_utc(since),
        )

    async def _fetch(self, query: str, *args: Any) -> list[Mapping[str, Any]]:
        try:
            rows = await self._require_pool().fetch(query, *args)
        except asyncpg.PostgresError as exc:
            raise TeslaMateSourceError(
                "cannot read TeslaMate data; the schema may be unsupported"
            ) from exc
        return [dict(row) for row in rows]

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise TeslaMateSourceError("database connection is not open")
        return self._pool


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)
