"""Startup and meta contracts against the fixture database."""

import asyncio
import os
import subprocess
import sys

import httpx
import pytest

from mileaf.app import create_app
from mileaf.config import Settings


async def test_meta_contract(client):
    response = await client.get("/v1/meta")
    assert response.status_code == 200
    body = response.json()
    assert body["charging_experience"]["contract_version"] == "charging-experience-v1"
    assert all(body["charging_experience"]["fields"].values())
    assert body["statistics_insights"]["contract_version"] == "statistics-insights-v1"
    assert body["statistics_insights"]["minimum_server_version"] == "0.3.0"
    assert body["statistics_insights"]["features"]["driving_coverage"] is True
    assert {
        key: value
        for key, value in body.items()
        if key not in {"charging_experience", "statistics_insights", "owner_experience"}
    } == {
        "version": "0.4.0",
        "teslamate_schema_version": 20260808090000,
        "schema_ok": True,
        "tested": True,
        "units": "metric",
        "source_unit_of_length": "km",
        "freshness": {
            "last_position_at": "2026-03-15T11:55:00Z",
            "last_state_at": "2026-03-14T23:00:00Z",
        },
    }
    assert (await client.get("/v1/meta", headers={"Authorization": ""})).status_code == 401


async def test_unknown_version_still_starts(db, database_dsn, tmp_path):
    await db.execute(
        "UPDATE schema_migrations SET version=99999999999999 WHERE version=20260808090000"
    )
    app = create_app(Settings(database_dsn=database_dsn, state_file=tmp_path / "state.json"))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        await client.post("/v1/auth/setup", json={"password": "fixture password"})
        token = (await client.post("/v1/auth/login", json={"password": "fixture password"})).json()[
            "token"
        ]
        response = await client.get("/v1/meta", headers={"Authorization": "Bearer " + token})
        assert response.status_code == 200
        assert response.json()["tested"] is False
        assert response.json()["teslamate_schema_version"] == 99999999999999


async def test_text_vehicle_name_starts_and_is_readable(db, database_dsn, tmp_path):
    await db.execute("ALTER TABLE cars ALTER COLUMN name TYPE text")
    expected_name = await db.fetchval("SELECT name FROM cars ORDER BY inserted_at, id LIMIT 1")
    app = create_app(Settings(database_dsn=database_dsn, state_file=tmp_path / "state.json"))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        await client.post("/v1/auth/setup", json={"password": "fixture password"})
        token = (await client.post("/v1/auth/login", json={"password": "fixture password"})).json()[
            "token"
        ]
        response = await client.get("/v1/vehicles", headers={"Authorization": "Bearer " + token})
        assert response.status_code == 200
        assert response.json()[0]["name"] == expected_name


@pytest.mark.parametrize(
    "mutation,missing",
    [
        ("ALTER TABLE positions RENAME TO missing_positions", "positions"),
        ("ALTER TABLE cars RENAME COLUMN id TO missing_id", "cars.id"),
        ("ALTER TABLE cars ALTER COLUMN name TYPE integer USING NULL", "cars.name"),
        ("ALTER TABLE positions ALTER COLUMN date TYPE text", "positions.date"),
        (
            "ALTER TABLE charges RENAME COLUMN charger_phases TO missing_phases",
            "charges.charger_phases",
        ),
    ],
)
async def test_startup_schema_failures(db, database_dsn, tmp_path, mutation, missing):
    await db.execute(mutation)
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "mileaf"],
        env={
            **os.environ,
            "MILEAF_DATABASE_DSN": database_dsn,
            "MILEAF_STATE_FILE": str(tmp_path / "state.json"),
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert missing in result.stderr
    assert "Restore" in result.stderr


async def test_credentials_permission_prevents_startup(db, database_dsn, tmp_path):
    role = database_dsn.split("//")[1].split("@")[0]
    await db.execute(f'GRANT SELECT ON private.tokens TO "{role}"')
    app = create_app(Settings(database_dsn=database_dsn, state_file=tmp_path / "state.json"))
    with pytest.raises(RuntimeError, match=r"REVOKE SELECT ON TABLE private\.tokens"):
        async with app.router.lifespan_context(app):
            pytest.fail("credential access must prevent startup")


def test_capabilities_are_enumerable():
    from mileaf.probes import CAPABILITIES

    assert set(CAPABILITIES) == {
        "meta",
        "vehicles",
        "drive_energy",
        "charging_classification",
        "locations",
        "statistics",
        "drive_details",
        "charging_session_details",
        "summary",
    }
    assert set(CAPABILITIES["vehicles"]) == {
        "cars",
        "states",
        "positions",
        "charging_processes",
        "settings",
    }
    assert set(CAPABILITIES["meta"]) == {
        "schema_migrations",
        "settings",
        "positions",
        "states",
        "cars",
    }
