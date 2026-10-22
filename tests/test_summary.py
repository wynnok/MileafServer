"""Overview metrics have independently worked fixture expectations."""


async def test_summary_timezone_periods_and_empty_vehicle(client):
    response = await client.get("/v1/vehicles/1/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["vehicle"]["id"] == 1
    assert body["state"] == {"value": "charging", "since": "2026-03-15T11:00:00Z"}
    assert body["charging_power_kw"] == 11
    assert body["battery"] == {"level": 68, "range_km": 320, "range_kind": "rated"}
    assert body["odometer_km"] == 10020
    assert body["period"]["today"] == {"drives": 2, "distance_km": 40, "energy_kwh": 7.5}
    # March: drives 1,2,3,4,6,8,9,14,15,16,17; id8 has no valid range delta,
    # id9 has zero km but valid energy. 10 * 3.75 kWh, 10 * 20 km.
    assert body["period"]["month"] == {"drives": 11, "distance_km": 200, "energy_kwh": 37.5}
    local = (await client.get("/v1/vehicles/1/summary", params={"tz": "Asia/Shanghai"})).json()
    assert local["period"]["today"] == {"drives": 3, "distance_km": 60, "energy_kwh": 11.25}
    assert body["firmware"] == {"version": "2026.2.3", "installed_at": "2026-02-15T08:30:00Z"}
    assert body["freshness"]["last_position_at"] == "2026-03-15T11:55:00Z"
    assert body["freshness"]["last_state_at"] == "2026-03-14T23:00:00Z"
    empty = (await client.get("/v1/vehicles/2/summary")).json()
    assert all(
        empty[field] is None for field in ["battery", "last_drive", "last_charge", "firmware"]
    )
    assert (
        empty["period"]["today"]
        == empty["period"]["month"]
        == {"drives": 0, "distance_km": 0, "energy_kwh": 0}
    )
    assert (await client.get("/v1/vehicles/999/summary")).status_code == 404
    assert (await client.get("/v1/vehicles/1/summary", params={"tz": "bad"})).status_code == 400
    assert (
        await client.get("/v1/vehicles/1/summary", headers={"Authorization": ""})
    ).status_code == 401


async def test_summary_missing_power_nonpositive_energy_and_cache_equivalence(client, db, app):
    # Make the non-positive range scenario the latest drive for this HTTP request.
    await db.execute(
        "UPDATE drives SET start_date='2026-04-01',end_date='2026-04-01 01:00' WHERE id=8"
    )
    await db.execute("DELETE FROM charges WHERE charging_process_id=8")
    response = await client.get("/v1/vehicles/1/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["state"]["value"] == "charging"
    assert body["charging_power_kw"] is None
    assert body["last_drive"] == {
        "id": 8,
        "started_at": "2026-04-01T00:00:00Z",
        "distance_km": 20,
        "consumption_wh_km": None,
    }
    assert body["last_charge"] == {
        "id": 9,
        "started_at": "2026-03-29T01:00:00Z",
        "energy_kwh": 20,
        "end_soc": 70,
    }
    cached = await client.get("/v1/vehicles/1/summary")
    assert cached.content == response.content
    assert app.state.cache.hits >= 1
    app.state.cache.ttl = 0
    uncached = await client.get("/v1/vehicles/1/summary")
    assert uncached.content == response.content
    assert uncached.json()["freshness"] == {
        "last_position_at": "2026-03-15T11:55:00Z",
        "last_state_at": "2026-03-14T23:00:00Z",
    }


async def test_summary_active_charge_energy_uses_latest_sample(client, db):
    await db.execute("DELETE FROM charging_processes WHERE id=9")
    await db.execute("UPDATE charging_processes SET charge_energy_added=NULL WHERE id=8")
    body = (await client.get("/v1/vehicles/1/summary")).json()
    assert body["last_charge"] == {
        "id": 8,
        "started_at": "2026-03-15T11:00:00Z",
        "energy_kwh": 20,
        "end_soc": None,
    }
