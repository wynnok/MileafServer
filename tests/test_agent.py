from __future__ import annotations

import httpx

from tests.conftest import auth_headers, pair_agent, register, sample_sync_payload


async def test_agent_pair_ingest_is_idempotent_and_tenant_scoped(client: httpx.AsyncClient) -> None:
    auth = await register(client)
    agent = await pair_agent(client, auth)

    sync = await client.post(
        "/v1/agent/sync",
        headers={"Authorization": f"Bearer {agent['agent_token']}"},
        json=sample_sync_payload(),
    )
    assert sync.status_code == 200, sync.text
    assert sync.json()["vehicles"] == 1
    assert sync.json()["drives"] == 2

    vehicles = await client.get("/v1/vehicles", headers=auth_headers(auth))
    assert vehicles.status_code == 200, vehicles.text
    body = vehicles.json()
    assert len(body) == 1
    assert body[0]["name"] == "Blue Thunder"
    assert body[0]["status"]["battery_level"] == 80

    # Re-sending the same batch must not duplicate rows, and updates must apply.
    payload = sample_sync_payload()
    payload["drives"][0]["distance_km"] = 26.0
    sync_again = await client.post(
        "/v1/agent/sync",
        headers={"Authorization": f"Bearer {agent['agent_token']}"},
        json=payload,
    )
    assert sync_again.status_code == 200

    vehicle_id = body[0]["id"]
    drives = await client.get(f"/v1/vehicles/{vehicle_id}/drives", headers=auth_headers(auth))
    assert drives.status_code == 200, drives.text
    assert len(drives.json()["items"]) == 2
    distances = {item["distance_km"] for item in drives.json()["items"]}
    assert distances == {26.0, 12.0}

    # Another tenant must not see the data.
    other = await register(client, email="other@example.com")
    other_vehicles = await client.get("/v1/vehicles", headers=auth_headers(other))
    assert other_vehicles.status_code == 200
    assert other_vehicles.json() == []
    forbidden = await client.get(f"/v1/vehicles/{vehicle_id}", headers=auth_headers(other))
    assert forbidden.status_code == 404


async def test_revoked_agent_cannot_sync(client: httpx.AsyncClient) -> None:
    auth = await register(client)
    agent = await pair_agent(client, auth)

    revoke = await client.delete(
        f"/v1/agent/sources/{agent['data_source_id']}", headers=auth_headers(auth)
    )
    assert revoke.status_code == 204

    sync = await client.post(
        "/v1/agent/sync",
        headers={"Authorization": f"Bearer {agent['agent_token']}"},
        json=sample_sync_payload(),
    )
    assert sync.status_code == 401


async def test_agent_cannot_use_user_access_token(client: httpx.AsyncClient) -> None:
    auth = await register(client)
    response = await client.post(
        "/v1/agent/sync",
        headers=auth_headers(auth),
        json=sample_sync_payload(),
    )
    assert response.status_code == 401


async def test_pairing_code_is_single_use(client: httpx.AsyncClient) -> None:
    auth = await register(client)
    code_response = await client.post("/v1/agent/pairing-codes", headers=auth_headers(auth))
    code = code_response.json()["code"]

    first = await client.post("/v1/agent/pair", json={"code": code, "name": "Agent one"})
    assert first.status_code == 200
    second = await client.post("/v1/agent/pair", json={"code": code, "name": "Agent two"})
    assert second.status_code == 400
