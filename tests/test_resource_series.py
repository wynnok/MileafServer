"""Sampling properties observable by API clients."""


async def test_series_bound_endpoints_peak_and_nulls(client, db):
    url, dense, sparse, empty = "/v1/drives", 1, 2, 13
    await db.execute("UPDATE positions SET speed=150 WHERE id=1060")
    result = await client.get(f"{url}/{dense}/series", params={"points": 24})
    assert result.status_code == 200
    body = result.json()
    assert body["resolution"] == "server_sampled"
    assert len(body["series"]) == body["points"] == 24
    times = [point["t"] for point in body["series"]]
    assert times == sorted(times)
    assert times[0] == "2026-03-15T08:00:00Z"
    assert times[-1] == "2026-03-15T10:00:00Z"
    assert max(p["speed_kmh"] for p in body["series"]) == 150
    assert all("lat" in p and "lon" in p for p in body["series"])
    assert all("battery_level" not in p for p in body["series"])  # default is speed
    await db.execute("UPDATE positions SET speed=NULL WHERE id=2001")
    sparse_body = (await client.get(f"{url}/{sparse}/series", params={"points": 24})).json()
    assert [p["speed_kmh"] for p in sparse_body["series"]] == [60, None, 60]
    empty_body = (await client.get(f"{url}/{empty}/series")).json()
    assert empty_body["series"] == []
    assert empty_body["resolution"] == "empty"
    for params in [
        {"points": 1},
        {"points": 1001},
        {"points": "bad"},
        {"fields": "unknown"},
        {"fields": ""},
    ]:
        assert (await client.get(f"{url}/{dense}/series", params=params)).status_code == 400
    assert (await client.get(f"{url}/999/series")).status_code == 404
    assert (
        await client.get(f"{url}/{dense}/series", headers={"Authorization": ""})
    ).status_code == 401
