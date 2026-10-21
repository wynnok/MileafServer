from __future__ import annotations

import httpx

from tests.conftest import auth_headers, pair_agent, register, sample_sync_payload


async def seeded_vehicle(client: httpx.AsyncClient) -> tuple[dict, str]:
    auth = await register(client)
    agent = await pair_agent(client, auth)
    sync = await client.post(
        "/v1/agent/sync",
        headers={"Authorization": f"Bearer {agent['agent_token']}"},
        json=sample_sync_payload(),
    )
    assert sync.status_code == 200, sync.text
    vehicles = (await client.get("/v1/vehicles", headers=auth_headers(auth))).json()
    return auth, vehicles[0]["id"]


async def test_vehicle_summary_and_stats(client: httpx.AsyncClient) -> None:
    auth, vehicle_id = await seeded_vehicle(client)

    summary = await client.get(f"/v1/vehicles/{vehicle_id}/summary", headers=auth_headers(auth))
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["vehicle"]["name"] == "Blue Thunder"
    assert body["status"]["range_km"] == 350.5
    assert body["stats"]["drive_count"] == 2
    assert body["stats"]["distance_km"] == 37.0
    assert body["stats"]["charge_count"] == 1
    assert body["stats"]["charge_cost"] == 12.5

    stats = await client.get(
        f"/v1/vehicles/{vehicle_id}/stats?period=month", headers=auth_headers(auth)
    )
    assert stats.status_code == 200
    assert stats.json()["period"] == "month"


async def test_drive_pagination_and_detail(client: httpx.AsyncClient) -> None:
    auth, vehicle_id = await seeded_vehicle(client)

    first_page = await client.get(
        f"/v1/vehicles/{vehicle_id}/drives?limit=1", headers=auth_headers(auth)
    )
    assert first_page.status_code == 200
    body = first_page.json()
    assert len(body["items"]) == 1
    assert body["next_cursor"]

    second_page = await client.get(
        f"/v1/vehicles/{vehicle_id}/drives?limit=1&cursor={body['next_cursor']}",
        headers=auth_headers(auth),
    )
    assert second_page.status_code == 200
    assert second_page.json()["items"][0]["id"] != body["items"][0]["id"]

    drive_id = body["items"][0]["id"]
    detail = await client.get(
        f"/v1/vehicles/{vehicle_id}/drives/{drive_id}", headers=auth_headers(auth)
    )
    assert detail.status_code == 200
    assert detail.json()["id"] == drive_id


async def test_charge_list_and_detail(client: httpx.AsyncClient) -> None:
    auth, vehicle_id = await seeded_vehicle(client)

    charges = await client.get(f"/v1/vehicles/{vehicle_id}/charges", headers=auth_headers(auth))
    assert charges.status_code == 200
    charge = charges.json()["items"][0]
    assert charge["energy_added_kwh"] == 10.0

    detail = await client.get(
        f"/v1/vehicles/{vehicle_id}/charges/{charge['id']}", headers=auth_headers(auth)
    )
    assert detail.status_code == 200
    assert detail.json()["location"] == "Home"


async def test_invalid_period_and_cursor(client: httpx.AsyncClient) -> None:
    auth, vehicle_id = await seeded_vehicle(client)
    bad_period = await client.get(
        f"/v1/vehicles/{vehicle_id}/stats?period=fortnight", headers=auth_headers(auth)
    )
    assert bad_period.status_code == 400

    bad_cursor = await client.get(
        f"/v1/vehicles/{vehicle_id}/drives?cursor=not-a-cursor",
        headers=auth_headers(auth),
    )
    assert bad_cursor.status_code == 400
