"""Metric values through the public Python and SQL boundaries."""

from decimal import Decimal

import httpx
import pytest
from fastapi import Depends

from mileaf.app import create_app
from mileaf.config import Settings
from mileaf.dependencies import get_pool
from mileaf.units import consumption_wh_km, distance_km, metric_sql, speed_kmh
from tests.fixtures.seeds.vehicles import seed_car


def test_metric_rounding_does_not_reconvert_source_values():
    assert distance_km(Decimal("100.05")) == Decimal("100.1")
    assert distance_km(34.2) == Decimal("34.2")
    assert speed_kmh(Decimal("100.5")) == Decimal("101")
    assert consumption_wh_km(Decimal("150.5")) == Decimal("151")
    assert distance_km(None) is None
    assert speed_kmh(None) is None
    assert consumption_wh_km(None) is None


async def test_sql_rounding_matches_python(db):
    assert await db.fetchval(
        f"SELECT {metric_sql('$1', places=1)}", Decimal("100.05")
    ) == distance_km(Decimal("100.05"))
    assert await db.fetchval(f"SELECT {metric_sql('$1', places=0)}", Decimal("100.5")) == speed_kmh(
        Decimal("100.5")
    )


@pytest.mark.parametrize("unit", ["km", "mi"])
async def test_site_preference_cached_without_reconverting_metric_columns(
    db, database_dsn, tmp_path, unit
):
    from mileaf.units import get_units

    vehicle_id = await seed_car(db, unit_of_length=unit)
    await db.execute(
        "INSERT INTO drives(car_id,start_date,distance,speed_max,start_rated_range_km) "
        "VALUES ($1,'2026-03-15',100,100,320)",
        vehicle_id,
    )
    app = create_app(Settings(database_dsn=database_dsn, state_file=tmp_path / "state.json"))

    @app.get("/v1/unit-probe")
    async def probe(pool=Depends(get_pool), units=Depends(get_units)):
        row = await pool.fetchrow(
            f"SELECT {metric_sql('distance', places=1)} AS distance, "
            f"{metric_sql('speed_max', places=0)} AS speed, start_rated_range_km "
            "FROM drives WHERE car_id=$1",
            vehicle_id,
        )
        return {
            "source": units.source_unit_of_length,
            "distance_km": distance_km(row["distance"]),
            "speed_kmh": speed_kmh(row["speed"]),
            "range_km": row["start_rated_range_km"],
        }

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        await client.post("/v1/auth/setup", json={"password": "a long password"})
        login = await client.post("/v1/auth/login", json={"password": "a long password"})
        client.headers["Authorization"] = "Bearer " + login.json()["token"]
        # Preference changes are observed on pool restart, never per request.
        await db.execute("UPDATE settings SET unit_of_length=$1", "mi" if unit == "km" else "km")
        response = await client.get("/v1/unit-probe")
        assert response.status_code == 200
        assert response.json() == {
            "source": unit,
            "distance_km": 100.0,
            "speed_kmh": 100,
            "range_km": 320,
        }


@pytest.mark.parametrize("invalid", ["ft", None])
async def test_invalid_or_missing_unit_rejects_startup(db, database_dsn, tmp_path, invalid):
    if invalid:
        await db.execute("ALTER TYPE unit_of_length ADD VALUE 'ft'")
        await db.execute("UPDATE settings SET unit_of_length='ft'")
    else:
        await db.execute("DELETE FROM settings")
    app = create_app(Settings(database_dsn=database_dsn, state_file=tmp_path / "state.json"))
    with pytest.raises(RuntimeError, match=r"settings\.unit_of_length.*km.*mi"):
        async with app.router.lifespan_context(app):
            pytest.fail("Invalid settings must prevent startup")
