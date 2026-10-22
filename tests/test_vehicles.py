"""Vehicle HTTP contract, including the dependency shared with future routes."""

from fastapi import Depends


async def test_vehicle_list(client):
    response = await client.get("/v1/vehicles")
    assert response.status_code == 200
    assert response.json() == [
        {
            "id": 1,
            "name": "行笺测试车",
            "model": "3",
            "vin_suffix": "0001",
            "state": {"value": "charging", "since": "2026-03-15T11:00:00Z"},
            "battery": {"level": 68, "range_km": 320.0, "range_kind": "rated"},
            "freshness": {
                "last_position_at": "2026-03-15T11:55:00Z",
                "last_state_at": "2026-03-14T23:00:00Z",
            },
        },
        {
            "id": 2,
            "name": None,
            "model": "Y",
            "vin_suffix": "0002",
            "state": None,
            "battery": None,
            "freshness": {"last_position_at": None, "last_state_at": None},
        },
    ]
    assert (await client.get("/v1/vehicles", headers={"Authorization": ""})).status_code == 401


async def test_preferred_range_and_latest_state(db, client):
    await db.execute("UPDATE settings SET preferred_range='ideal'")
    await db.execute(
        "UPDATE charging_processes SET end_date='2026-03-15 12:00' WHERE end_date IS NULL"
    )
    response = await client.get("/v1/vehicles")
    vehicle = response.json()[0]
    assert vehicle["battery"] == {"level": 68, "range_km": 350.0, "range_kind": "ideal"}
    assert vehicle["state"] == {"value": "online", "since": "2026-03-14T23:00:00Z"}
    conditional = await client.get(
        "/v1/vehicles", headers={"If-None-Match": response.headers["etag"]}
    )
    assert conditional.status_code == 304


async def test_creation_order_and_lookup(db, client, app):
    from mileaf.vehicles import get_vehicle_or_404

    @app.get("/v1/lookup/{vehicle_id}")
    async def lookup(vehicle=Depends(get_vehicle_or_404)):
        return vehicle

    await db.execute("UPDATE cars SET inserted_at='2025-01-01' WHERE id=2")
    assert [v["id"] for v in (await client.get("/v1/vehicles")).json()] == [2, 1]
    assert (await client.get("/v1/lookup/2")).json() == {
        "id": 2,
        "name": None,
        "model": "Y",
        "vin_suffix": "0002",
    }
    missing = await client.get("/v1/lookup/999")
    assert missing.status_code == 404
    assert missing.json() == {"error": {"code": "not_found", "message": "Resource not found"}}
    anonymous = await client.get("/v1/lookup/999", headers={"Authorization": ""})
    assert anonymous.status_code == 401
