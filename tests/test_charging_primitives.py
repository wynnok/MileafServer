"""One classification shared by list SQL and interval energy aggregation."""

from datetime import UTC, datetime
from decimal import Decimal

from mileaf.queries.charging import charging_energy, is_dc_sql


async def test_classification_and_energy_use_the_same_rule(db):
    rows = await db.fetch(
        f"SELECT cp.id,{is_dc_sql()} AS is_dc FROM charging_processes cp ORDER BY cp.id"
    )
    assert [r["is_dc"] for r in rows] == [
        False,
        True,
        False,
        False,
        None,
        False,
        False,
        False,
        False,
    ]
    result = await charging_energy(
        db,
        vehicle_id=1,
        from_=datetime(2026, 1, 1, tzinfo=UTC),
        to=datetime(2026, 4, 1, tzinfo=UTC),
    )
    # Finished sessions: six AC, one DC, one without any samples, 20 kWh each.
    assert result == {
        "ac_energy": Decimal(120),
        "dc_energy": Decimal(20),
        "unknown_energy": Decimal(20),
        "total_energy": Decimal(160),
    }


async def test_all_null_phases_are_dc_and_partial_nulls_use_non_null_mode(db):
    await db.execute(
        "UPDATE charges SET charger_phases=NULL,fast_charger_present=NULL "
        "WHERE charging_process_id=1"
    )
    assert (
        await db.fetchval(f"SELECT {is_dc_sql()} FROM charging_processes cp WHERE cp.id=1") is True
    )
    # A minority of NULLs does not override the mode of the non-null phases.
    assert (
        await db.fetchval(f"SELECT {is_dc_sql()} FROM charging_processes cp WHERE cp.id=7") is False
    )
    result = await charging_energy(db, vehicle_id=2)
    assert result == {"ac_energy": 0, "dc_energy": 0, "unknown_energy": 0, "total_energy": 0}


async def test_aggregation_uses_half_open_end_time_window(db):
    result = await charging_energy(
        db,
        vehicle_id=1,
        from_=datetime(2026, 1, 5, 9, tzinfo=UTC),
        to=datetime(2026, 2, 5, 9, tzinfo=UTC),
    )
    assert result["ac_energy"] == 20
    assert result["dc_energy"] == 0
    assert result["total_energy"] == 20
