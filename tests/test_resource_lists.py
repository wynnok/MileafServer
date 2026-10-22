"""The same pagination assertions apply to both resource tables."""

import pytest


@pytest.mark.parametrize("resource,total", [("drives", 50), ("charging-sessions", 9)])
async def test_stable_pages_and_half_open_windows(client, db, resource, total):
    url = f"/v1/vehicles/1/{resource}"
    first = await client.get(url, params={"limit": 7})
    assert first.status_code == 200
    body = first.json()
    ids = [item["id"] for item in body["items"]]
    table = "drives" if resource == "drives" else "charging_processes"
    if table == "drives":
        await db.execute("INSERT INTO drives(id,car_id,start_date) VALUES (999,1,'2027-01-01')")
    else:
        await db.execute(
            "INSERT INTO charging_processes(id,car_id,position_id,start_date) "
            "VALUES (999,1,999999,'2027-01-01')"
        )
    while body["next_cursor"]:
        response = await client.get(url, params={"limit": 7, "cursor": body["next_cursor"]})
        assert response.status_code == 200
        body = response.json()
        ids.extend(item["id"] for item in body["items"])
    assert len(ids) == len(set(ids)) == total
    assert set(ids) == set(range(1, total + 1))
    bounds = {"from": "2026-03-15T08:00:00Z", "to": "2026-03-15T10:30:00Z"}
    if resource != "drives":
        bounds = {"from": "2026-03-02T08:00:00Z", "to": "2026-03-03T08:00:00Z"}
    filtered = (await client.get(url, params=bounds)).json()
    assert [item["id"] for item in filtered["items"]] == ([1] if resource == "drives" else [5])
    assert (await client.get(f"/v1/vehicles/2/{resource}")).json()["items"] == []
    assert (await client.get(f"/v1/vehicles/999/{resource}")).status_code == 404
    for params in [
        {"limit": 0},
        {"limit": 101},
        {"limit": "abc"},
        {"cursor": "bad"},
        {"from": "bad"},
        {"from": "2026-01-01"},
        {"from": "2026-02-01T00:00Z", "to": "2026-01-01T00:00Z"},
    ]:
        assert (await client.get(url, params=params)).status_code == 400
    assert (await client.get(url, headers={"Authorization": ""})).status_code == 401
