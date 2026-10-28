"""Charging classification and energy aggregation using one shared expression."""

from datetime import datetime
from decimal import Decimal

import asyncpg

from mileaf.database import fetch_rows
from mileaf.probes import CAPABILITIES

CAPABILITIES["charging_classification"] = {
    "charges": {"charging_process_id": ("int4",), "charger_phases": ("int2",)},
    "charging_processes": {
        "id": ("int4",),
        "car_id": ("int2",),
        "end_date": ("timestamp",),
        "charge_energy_added": ("numeric",),
    },
}


def is_dc_aggregate_sql() -> str:
    """Same classification for correlated detail queries and batched match sets."""
    return (
        "CASE WHEN count(*)=0 THEN NULL "
        "ELSE coalesce(mode() WITHIN GROUP (ORDER BY sample.charger_phases),0)=0 END"
    )


def is_dc_sql(*, session: str = "cp") -> str:
    """Trusted SQL alias only; use in lists and grouped energy queries alike."""
    return (
        "(/* TeslaMate v4.3.0 charging-stats: mode of non-NULL charger_phases; "
        "NULL/legacy zero mode => DC, positive => AC. v4.3.0 normalizes non-positive "
        "phases to NULL (positive_charger_phases constraint). No samples => unknown "
        "(Mileaf extension). Classification is unitless; energy below is stored kWh. "
        "Independently rewritten from panel semantics. */ "
        f"SELECT {is_dc_aggregate_sql()} "
        f"FROM charges sample WHERE sample.charging_process_id={session}.id)"
    )


async def charging_energy(
    db: asyncpg.Connection,
    *,
    vehicle_id: int,
    from_: datetime | None = None,
    to: datetime | None = None,
) -> dict[str, Decimal]:
    """Aggregate finished sessions by end time [from, to), in stored added kWh.

    Unknown classifications remain separate; NULL energy is not invented as zero per
    session. Empty sums are zero. Callers can reuse is_dc_sql for calendar buckets.
    """
    rows = await fetch_rows(
        db,
        "/* Mileaf charging statistics (#5): sum charge_energy_added (kWh), "
        "not charging-stats electricity usage greatest(added,used). */ "
        f"WITH classified AS (SELECT cp.charge_energy_added AS energy, {is_dc_sql()} AS is_dc "
        "FROM charging_processes cp WHERE cp.car_id=$1 AND cp.end_date IS NOT NULL "
        "AND ($2::timestamptz IS NULL OR cp.end_date AT TIME ZONE 'UTC' >= $2) "
        "AND ($3::timestamptz IS NULL OR cp.end_date AT TIME ZONE 'UTC' < $3)) "
        "SELECT coalesce(sum(energy) FILTER (WHERE is_dc IS FALSE),0) AS ac_energy, "
        "coalesce(sum(energy) FILTER (WHERE is_dc IS TRUE),0) AS dc_energy, "
        "coalesce(sum(energy) FILTER (WHERE is_dc IS NULL),0) AS unknown_energy, "
        "coalesce(sum(energy),0) AS total_energy FROM classified",
        vehicle_id,
        from_,
        to,
    )
    return dict(rows[0])
