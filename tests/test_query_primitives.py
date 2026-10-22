"""Shared primitive seam, evaluated by PostgreSQL rather than SQL-string assertions."""

from datetime import UTC, datetime

import pytest

from mileaf.queries.time import bucket_sql, window_sql


async def test_midnight_windows_use_start_time_once(db):
    for tz, day, expected in [
        ("UTC", 14, 20),
        ("UTC", 15, 0),
        ("Asia/Shanghai", 14, 0),
        ("Asia/Shanghai", 15, 20),
    ]:
        start, end = window_sql("today", now="$1", tz="$2")
        value = await db.fetchval(
            f"SELECT coalesce(sum(distance),0) FROM drives WHERE id=6 "
            f"AND start_date AT TIME ZONE 'UTC' >= {start} "
            f"AND start_date AT TIME ZONE 'UTC' < {end}",
            datetime(2026, 3, day, 12, tzinfo=UTC),
            tz,
        )
        assert value == expected


@pytest.mark.parametrize("day,hours", [(29, 23), (25, 25)])
async def test_dst_day_duration(db, day, hours):
    month = 3 if hours == 23 else 10
    start, end = window_sql("today", now="$1", tz="$2")
    row = await db.fetchrow(
        f"SELECT {start} AS start, {end} AS end",
        datetime(2026, month, day, 12, tzinfo=UTC),
        "Europe/Berlin",
    )
    assert (row["end"] - row["start"]).total_seconds() == hours * 3600


async def test_calendar_buckets_and_repeatability(db):
    expression = bucket_sql("day", timestamp="start_date AT TIME ZONE 'UTC'", tz="$1")
    query = f"SELECT id, {expression} AS bucket FROM drives WHERE id IN (14,15,16) ORDER BY id"
    rows = await db.fetch(query, "Europe/Berlin")
    assert [r["bucket"] for r in rows] == [
        datetime(2026, 3, 27, 23, tzinfo=UTC),
        datetime(2026, 3, 28, 23, tzinfo=UTC),
        datetime(2026, 3, 28, 23, tzinfo=UTC),
    ]
    assert await db.fetch(query, "Europe/Berlin") == rows
    week = bucket_sql("week", timestamp="start_date AT TIME ZONE 'UTC'", tz="$1")
    assert await db.fetchval(f"SELECT {week} FROM drives WHERE id=6", "UTC") == datetime(
        2026, 3, 9, tzinfo=UTC
    )
    start, end = window_sql("month", now="$1", tz="$2")
    row = await db.fetchrow(
        f"SELECT {start} AS start, {end} AS end", datetime(2026, 3, 15, tzinfo=UTC), "Asia/Shanghai"
    )
    assert row["start"] == datetime(2026, 2, 28, 16, tzinfo=UTC)
    assert row["end"] == datetime(2026, 3, 31, 16, tzinfo=UTC)


async def test_energy_and_consumption(db):
    from mileaf.queries.energy import consumption_wh_km_sql, energy_kwh_sql

    energy = energy_kwh_sql()
    consumption = consumption_wh_km_sql()

    async def values(drive_id):
        return await db.fetchrow(
            f"SELECT {energy} AS energy, {consumption} AS consumption FROM drives d "
            "JOIN cars c ON c.id=d.car_id CROSS JOIN settings s WHERE d.id=$1",
            drive_id,
        )

    row = await values(1)
    assert row["energy"] == pytest.approx(3.75)
    assert row["consumption"] == pytest.approx(187.5)
    assert dict(await values(8)) == {"energy": None, "consumption": None}
    assert (await values(9))["consumption"] is None
    await db.execute("UPDATE settings SET preferred_range='ideal', unit_of_length='mi'")
    row = await values(1)
    assert row["energy"] == pytest.approx(8.25)
    assert row["consumption"] == pytest.approx(412.5)
    await db.execute("UPDATE drives SET end_ideal_range_km=400 WHERE id=1")
    assert dict(await values(1)) == {"energy": None, "consumption": None}
    from tests.fixtures.seeds.vehicles import seed_car

    vehicle_id = await seed_car(db, efficiency=None)
    await db.execute("UPDATE drives SET car_id=$1 WHERE id=2", vehicle_id)
    assert dict(await values(2)) == {"energy": None, "consumption": None}


async def test_timezone_validation_http(app, client):
    from fastapi import Depends

    from mileaf.queries.time import validate_timezone

    @app.get("/v1/timezone-probe")
    async def probe(tz=Depends(validate_timezone)):
        return {"tz": tz}

    assert (await client.get("/v1/timezone-probe")).json() == {"tz": "UTC"}
    for tz in ["Mars/Olympus", "/etc/passwd", "../UTC"]:
        response = await client.get("/v1/timezone-probe", params={"tz": tz})
        assert response.status_code == 400
        assert response.json() == {
            "error": {"code": "invalid_request", "message": "Invalid IANA timezone"}
        }
