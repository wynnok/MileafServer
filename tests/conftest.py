from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from mileaf.config import Settings
from mileaf.db import Base
from mileaf.main import create_app


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    test_engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with test_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield test_engine
    await test_engine.dispose()


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite://",
        jwt_secret="test-secret-0123456789abcdef-0123456789abcdef",
        agent_token_pepper="test-pepper",
        auto_create_tables=False,
    )


@pytest.fixture
async def client(
    settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings=settings, sessionmaker=sessionmaker)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client


async def register(
    client: httpx.AsyncClient, email: str = "owner@example.com", password: str = "correct-horse"
) -> dict:
    response = await client.post(
        "/v1/auth/register",
        json={"email": email, "password": password, "display_name": "Owner"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def auth_headers(auth: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth['access_token']}"}


async def pair_agent(client: httpx.AsyncClient, auth: dict) -> dict:
    code_response = await client.post("/v1/agent/pairing-codes", headers=auth_headers(auth))
    assert code_response.status_code == 200, code_response.text
    code = code_response.json()["code"]
    pair_response = await client.post(
        "/v1/agent/pair",
        json={
            "code": code,
            "name": "Mileaf Agent",
            "source_version": "1.0",
            "schema_version": "20260101000000",
        },
    )
    assert pair_response.status_code == 200, pair_response.text
    return pair_response.json()


def sample_sync_payload() -> dict:
    return {
        "source_version": "1.0",
        "schema_version": "20260101000000",
        "vehicles": [
            {
                "external_id": 1,
                "name": "Blue Thunder",
                "model": "3",
                "vin": "VIN00000000000001",
                "marketing_name": "Model 3",
                "efficiency": 0.153,
                "display_priority": 1,
            }
        ],
        "statuses": [
            {
                "vehicle_external_id": 1,
                "captured_at": "2026-10-20T10:00:00Z",
                "state": "online",
                "battery_level": 80,
                "usable_battery_level": 78,
                "range_km": 350.5,
                "rated_range_km": 400.0,
                "odometer_km": 12345.6,
                "latitude": 31.2304,
                "longitude": 121.4737,
                "speed_kmh": 0,
                "power_kw": 0.0,
                "is_charging": False,
                "outside_temp_c": 22.5,
                "inside_temp_c": 23.0,
            }
        ],
        "drives": [
            {
                "external_id": 101,
                "vehicle_external_id": 1,
                "start_date": "2026-10-19T08:00:00Z",
                "end_date": "2026-10-19T08:30:00Z",
                "distance_km": 25.0,
                "duration_min": 30,
                "start_address": "Home",
                "end_address": "Office",
                "start_battery_level": 90,
                "end_battery_level": 82,
                "start_range_km": 360.0,
                "end_range_km": 328.0,
                "energy_kwh": 3.825,
                "efficiency_wh_km": 153.0,
                "speed_max_kmh": 110,
            },
            {
                "external_id": 102,
                "vehicle_external_id": 1,
                "start_date": "2026-10-20T09:00:00Z",
                "end_date": "2026-10-20T09:20:00Z",
                "distance_km": 12.0,
                "duration_min": 20,
                "start_address": "Office",
                "end_address": "Gym",
                "start_battery_level": 82,
                "end_battery_level": 78,
                "energy_kwh": 1.836,
                "efficiency_wh_km": 153.0,
            },
        ],
        "charges": [
            {
                "external_id": 201,
                "vehicle_external_id": 1,
                "start_date": "2026-10-18T22:00:00Z",
                "end_date": "2026-10-18T23:00:00Z",
                "duration_min": 60,
                "energy_added_kwh": 10.0,
                "energy_used_kwh": 10.5,
                "start_battery_level": 60,
                "end_battery_level": 80,
                "start_range_km": 240.0,
                "end_range_km": 320.0,
                "cost": 12.5,
                "location": "Home",
                "geofence": "Home",
                "latitude": 31.2,
                "longitude": 121.4,
                "charger_type": "AC",
                "max_power_kw": 11.0,
            }
        ],
    }
