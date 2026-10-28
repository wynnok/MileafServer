"""A2's public SQL seam: fixture content, replay and isolation."""

from datetime import datetime
from decimal import Decimal

import asyncpg


async def test_base_seed_has_two_vehicles_and_no_data_for_second(db: asyncpg.Connection) -> None:
    assert [tuple(row) for row in await db.fetch("SELECT id, name FROM cars ORDER BY id")] == [
        (1, "行笺测试车"),
        (2, None),
    ]
    assert await db.fetchval("SELECT count(*) FROM positions WHERE car_id=2") == 0
    assert await db.fetchval("SELECT preferred_range::text FROM settings") == "rated"
    assert await db.fetchval("SELECT unit_of_length::text FROM settings") == "km"
    assert await db.fetchval("SELECT efficiency FROM cars WHERE id=1") == 0.15
    assert await db.fetchval("SELECT count(*) FROM drives") == 50
    assert await db.fetchval("SELECT max(date) FROM positions") == datetime(2026, 3, 15, 11, 55)
    assert await db.fetchval(
        "SELECT rated_battery_range_km FROM positions ORDER BY date DESC LIMIT 1"
    ) == Decimal("320")


async def test_scene_matrix_is_queryable(db: asyncpg.Connection) -> None:
    # Each literal expectation comes from #6, rather than recomputing the seed algorithm.
    cases = [
        (
            "SELECT array_agg(state::text ORDER BY id) FROM states",
            ["online", "asleep", "offline", "online"],
        ),
        (
            "SELECT count(*) FROM positions WHERE date > '2026-03-14 12:00' "
            "AND date < '2026-03-14 22:00'",
            0,
        ),
        ("SELECT count(*) FROM drives WHERE end_date IS NULL", 1),
        (
            "SELECT count(*) FROM drives WHERE start_date::date='2026-03-15' "
            "AND end_date IS NOT NULL",
            2,
        ),
        ("SELECT start_date FROM drives WHERE id=6", datetime(2026, 3, 14, 23, 30)),
        ("SELECT end_date FROM drives WHERE id=6", datetime(2026, 3, 15, 0, 40)),
        ("SELECT start_rated_range_km-end_rated_range_km FROM drives WHERE id=8", 0),
        ("SELECT distance FROM drives WHERE id=9", 0),
        ("SELECT start_geofence_id=1 AND end_geofence_id=2 FROM drives WHERE id=10", True),
        ("SELECT start_address_id=1 AND end_address_id=2 FROM drives WHERE id=11", True),
        (
            "SELECT start_geofence_id IS NULL AND start_address_id IS NULL "
            "AND start_position_id IS NOT NULL FROM drives WHERE id=12",
            True,
        ),
        (
            "SELECT start_geofence_id IS NULL AND end_geofence_id IS NULL "
            "AND start_address_id IS NULL AND end_address_id IS NULL "
            "AND start_position_id IS NULL AND end_position_id IS NULL FROM drives WHERE id=13",
            True,
        ),
        ("SELECT count(*) FROM positions WHERE drive_id=1", 121),
        (
            "SELECT min(delta)=interval '1 minute' AND max(delta)=interval '1 minute' FROM "
            "(SELECT date-lag(date) OVER (ORDER BY date) delta FROM positions WHERE drive_id=1) p",
            True,
        ),
        ("SELECT count(*) FROM positions WHERE drive_id=2", 3),
        ("SELECT count(*) FROM positions WHERE drive_id=13", 0),
        (
            "SELECT array_agg(start_date ORDER BY id) FROM drives WHERE id BETWEEN 14 AND 17",
            [
                datetime(2026, 3, 28, 22, 30),
                datetime(2026, 3, 29, 0, 30),
                datetime(2026, 3, 29, 21, 30),
                datetime(2026, 3, 30, 2),
            ],
        ),
        ("SELECT count(*) FROM charges WHERE charging_process_id=1 AND charger_phases=3", 61),
        (
            "SELECT bool_and(fast_charger_present AND charger_phases IS NULL) "
            "FROM charges WHERE charging_process_id=2",
            True,
        ),
        ("SELECT cost IS NULL FROM charging_processes WHERE id=3", True),
        ("SELECT cost FROM charging_processes WHERE id=4", 0),
        ("SELECT count(*) FROM charges WHERE charging_process_id=5", 0),
        ("SELECT count(*) FROM charges WHERE charging_process_id=6", 2),
        ("SELECT count(*) FROM charges WHERE charging_process_id=7 AND charger_phases IS NULL", 21),
        (
            "SELECT bool_and(fast_charger_present IS NULL) FROM charges "
            "WHERE charging_process_id=7",
            True,
        ),
        ("SELECT end_date IS NULL FROM charging_processes WHERE id=8", True),
        (
            "SELECT max(date) FROM charges WHERE charging_process_id=8",
            datetime(2026, 3, 15, 11, 50),
        ),
        ("SELECT start_date FROM charging_processes WHERE id=9", datetime(2026, 3, 29, 1)),
        ("SELECT array_agg(name ORDER BY id) FROM geofences", ["家", "公司"]),
        ("SELECT road || ' ' || house_number FROM addresses WHERE id=1", "测试路 10"),
        ("SELECT city FROM addresses WHERE id=2 AND road IS NULL AND house_number IS NULL", "上海"),
        ("SELECT version FROM updates", "2026.2.3"),
        ("SELECT end_date FROM updates", datetime(2026, 2, 15, 8, 30)),
        ("SELECT max(version) FROM schema_migrations", 20260808090000),
        ("SELECT count(*) FROM schema_migrations", 105),
    ]
    for query, expected in cases:
        assert await db.fetchval(query) == expected, query


async def test_domain_seed_replay_preserves_exact_rows(db: asyncpg.Connection) -> None:
    from tests.fixtures.seeds import SEEDERS

    tables = (
        "settings",
        "car_settings",
        "cars",
        "states",
        "positions",
        "drives",
        "geofences",
        "addresses",
        "charging_processes",
        "charges",
        "updates",
    )
    snapshots = {
        table: await db.fetchval(f"SELECT jsonb_agg(to_jsonb(row) ORDER BY id) FROM {table} row")
        for table in tables
    }
    for seed in SEEDERS:
        await seed(db)
        for table in tables:
            assert (
                await db.fetchval(f"SELECT jsonb_agg(to_jsonb(row) ORDER BY id) FROM {table} row")
                == snapshots[table]
            ), table


async def test_seed_car_helper_adds_local_vehicle_without_converting_storage(
    db: asyncpg.Connection,
) -> None:
    from tests.fixtures.seeds.vehicles import seed_car

    vehicle_id = await seed_car(db, unit_of_length="mi")
    assert vehicle_id == 3
    assert await db.fetchval("SELECT efficiency FROM cars WHERE id=$1", vehicle_id) is None
    assert await db.fetchval("SELECT unit_of_length::text FROM settings") == "mi"
    assert await db.fetchval("SELECT distance FROM drives WHERE id=1") == 20
    other_id = await seed_car(db, efficiency=0.16)
    assert other_id == 4
    assert await db.fetchval("SELECT efficiency FROM cars WHERE id=$1", other_id) == 0.16


async def test_health_client_uses_real_database(health_client) -> None:
    response = await health_client.get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.3.0"}
