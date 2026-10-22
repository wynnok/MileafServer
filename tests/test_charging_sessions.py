"""Charging session detail contracts, including incomplete upstream aggregates."""


async def test_charging_detail_classification_cost_and_active(client):
    response = await client.get("/v1/charging-sessions/1")
    assert response.status_code == 200
    body = response.json()
    assert body["energy_added_kwh"] == 20
    assert body["energy_used_kwh"] == 22
    assert body["start_soc"] == body["start_battery_level"] == 40
    assert body["end_soc"] == body["end_battery_level"] == 70
    assert body["duration_min"] == 60
    assert body["outside_temp_c_avg"] == 15
    assert body["charger_phases_mode"] == 3
    assert body["is_dc"] is False
    assert body["location"]["display_name"] == "家"
    for identifier, dc, cost in [(2, True, 12), (3, False, None), (4, False, 0), (5, None, 12)]:
        row = (await client.get(f"/v1/charging-sessions/{identifier}")).json()
        assert row["is_dc"] is dc
        assert row["cost"] == cost
        assert "currency" not in row
    active = (await client.get("/v1/charging-sessions/8")).json()
    assert active["is_active"] is True
    assert active["energy_added_kwh"] == 20
    assert active["ended_at"] is active["end_soc"] is active["end_battery_level"] is None
    assert active["duration_min"] is None
    assert (await client.get("/v1/charging-sessions/999")).status_code == 404
    assert (
        await client.get("/v1/charging-sessions/1", headers={"Authorization": ""})
    ).status_code == 401


async def test_charging_series_dense_sparse_empty_and_missing(client, db):
    url = "/v1/charging-sessions"
    dense = await client.get(f"{url}/1/series", params={"points": 24})
    assert dense.status_code == 200
    body = dense.json()
    assert body["session_id"] == 1
    assert body["points"] == len(body["series"]) == 24
    times = [point["t"] for point in body["series"]]
    assert times == sorted(times)
    assert times[0] == "2026-01-05T08:00:00Z"
    assert times[-1] == "2026-01-05T09:00:00Z"
    assert body["series"][0]["power"] == 11
    assert body["series"][0]["soc"] == 40
    assert body["series"][-1]["soc"] == 70
    # Create a genuinely sparse three-point session without changing the global seed.
    await db.execute(
        "DELETE FROM charges WHERE charging_process_id=1 AND id NOT IN (1000,1030,1060)"
    )
    await db.execute("UPDATE charges SET battery_level=NULL WHERE id=1030")
    sparse = (await client.get(f"{url}/1/series", params={"points": 25})).json()
    assert [p["soc"] for p in sparse["series"]] == [40, None, 70]
    empty = (await client.get(f"{url}/5/series")).json()
    assert empty["series"] == []
    assert empty["resolution"] == "empty"
    for params in [{"points": 1}, {"points": 1001}, {"points": "bad"}, {"fields": "speed"}]:
        assert (await client.get(f"{url}/1/series", params=params)).status_code == 400
    assert (await client.get(f"{url}/999/series")).status_code == 404
    assert (await client.get(f"{url}/1/series", headers={"Authorization": ""})).status_code == 401


async def test_active_session_energy_uses_latest_charge_sample(client, db):
    # TeslaMate leaves the process aggregate NULL until charging completes.
    await db.execute("UPDATE charging_processes SET charge_energy_added=NULL WHERE id=8")
    detail = (await client.get("/v1/charging-sessions/8")).json()
    assert detail["is_active"] is True
    assert detail["energy_added_kwh"] == 20  # latest sample's cumulative value
    listing = (await client.get("/v1/vehicles/1/charging-sessions")).json()
    active = next(item for item in listing["items"] if item["id"] == 8)
    assert active["energy_added_kwh"] == 20
