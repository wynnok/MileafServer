"""Drive HTTP contracts over the TeslaMate fixture database."""


async def test_drive_detail_and_active_nulls(client):
    response = await client.get("/v1/drives/1")
    assert response.status_code == 200
    body = response.json()
    assert body["distance_km"] == 20
    assert body["duration_min"] == 120
    assert body["energy_kwh"] == 3.75  # (350 - 325) * 0.15
    assert body["consumption_wh_km"] == 188  # 3.75 / 20 * 1000, half up
    assert body["speed_avg_kmh"] == 10
    assert body["speed_max_kmh"] == 90
    assert body["start"]["battery_level"] == 70
    assert body["end"]["battery_level"] == 70
    assert body["range_kind"] == "rated"
    assert body["outside_temp_c_avg"] == 15
    assert body["inside_temp_c_avg"] == 21
    assert body["power_max_kw"] == 120
    active = (await client.get("/v1/drives/7")).json()
    for field in ["ended_at", "distance_km", "duration_min", "energy_kwh", "consumption_wh_km"]:
        assert active[field] is None
    assert active["end"]["battery_level"] is None
    invalid_range = (await client.get("/v1/drives/8")).json()
    assert invalid_range["energy_kwh"] is None
    assert invalid_range["consumption_wh_km"] is None


async def test_drive_locations_speed_fallback_and_preferred_range(client, db):
    for identifier, label in [(10, "家"), (11, "测试路 10"), (12, None), (13, None)]:
        response = await client.get(f"/v1/drives/{identifier}")
        assert response.status_code == 200
        start = response.json()["start"]
        assert start["display_name"] == label
        assert (start["lat"] is None) == (identifier == 13)
    await db.execute("UPDATE drives SET duration_min=NULL WHERE id=1")
    await db.execute("UPDATE settings SET preferred_range='ideal',unit_of_length='mi'")
    body = (await client.get("/v1/drives/1")).json()
    assert body["speed_avg_kmh"] == 10  # 20 km / two elapsed hours
    assert body["range_kind"] == "ideal"
    assert body["distance_km"] == 20
    assert body["energy_kwh"] == 8.25  # (380 - 325) * 0.15
    assert body["consumption_wh_km"] == 413
    assert (await client.get("/v1/drives/999")).status_code == 404
    assert (await client.get("/v1/drives/1", headers={"Authorization": ""})).status_code == 401


async def test_series_field_selection_and_whitelist(client):
    response = await client.get("/v1/drives/2/series", params={"fields": "battery,elevation"})
    assert response.status_code == 200
    assert response.json()["series"][0] == {
        "t": "2026-03-15T10:30:00Z",
        "lat": 31.23,
        "lon": 121.47,
        "battery_level": 70,
        "elevation_m": 20,
    }
    # Coordinates are always present but are not chart field selectors.
    assert (await client.get("/v1/drives/2/series", params={"fields": "lat"})).status_code == 400
