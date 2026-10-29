"""Owner experience through the approved HTTP seam and isolated PostgreSQL."""

import pytest

RANGE = {"from": "2026-03-01T00:00:00Z", "to": "2026-03-15T12:00:00Z", "tz": "UTC"}
BASE = "/v1/vehicles/1/"


async def test_battery_history_real_points_gaps_and_cache(client, db):
    await db.execute("""INSERT INTO positions(id,car_id,date,latitude,longitude,battery_level,
        usable_battery_level) VALUES (90001,1,'2026-03-14 01:00',0,0,0,NULL),
        (90002,1,'2026-03-14 01:00',0,0,12,0),
        (90003,1,'2026-03-14 02:00',0,0,90,85)""")
    params = {**RANGE, "from": "2026-03-14T00:00:00Z", "max_points": 2}
    response = await client.get(BASE + "battery-history", params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["output_points"] <= 2
    assert body["raw_points"] > body["deduplicated_points"]
    assert body["gaps"]["items"]
    assert body["method"] == "position-history-v1"
    assert all(p["source"] == "position" for p in body["items"])
    cached = await client.get(
        BASE + "battery-history", params=params, headers={"If-None-Match": response.headers["etag"]}
    )
    assert cached.status_code == 304
    assert (await client.get("/v1/vehicles/2/battery-history", params=RANGE)).json()[
        "status"
    ] == "no_records"


async def test_timeline_intersection_and_bound_paging(client, db):
    await db.execute("""INSERT INTO drives(id,car_id,start_date,end_date) VALUES
        (90001,1,'2026-02-28','2026-03-02'),(90002,1,'2026-03-14',NULL)""")
    params = {**RANGE, "limit": 1}
    items = []
    while True:
        response = await client.get(BASE + "activity-timeline", params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        items.extend(body["items"])
        if not body["next_cursor"]:
            break
        params["cursor"] = body["next_cursor"]
    assert len({x["event_id"] for x in items}) == len(items)
    cross = next(x for x in items if x["event_id"] == "drive:90001")
    assert cross["intersection_start"] == "2026-03-01T00:00:00Z"
    assert next(x for x in items if x["event_id"] == "drive:90002")["status"] == "ongoing"
    assert (
        await client.get(BASE + "activity-timeline", params={**params, "tz": "Asia/Shanghai"})
    ).status_code == 400


async def test_owner_openapi_routes(tmp_path):
    import httpx

    from mileaf.app import create_app
    from mileaf.config import Settings

    app = create_app(
        Settings(
            database_dsn="postgresql://fixture@127.0.0.1:1/unused", state_file=tmp_path / "state"
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        schema = (await http.get("/openapi.json")).json()
    assert "/v1/vehicles/{vehicle_id}/battery-history" in schema["paths"]


async def test_latest_observation_preserves_null_and_zero(client, db):
    await db.execute("""INSERT INTO positions(id,car_id,date,latitude,longitude,inside_temp,
        outside_temp,tpms_pressure_fl) VALUES (90010,1,'2026-03-15 11:59',0,0,0,-5,2.5)""")
    response = await client.get(BASE + "vehicle-observations/latest")
    assert response.status_code == 200, response.text
    metrics = response.json()["items"][0]["metrics"]
    assert metrics["inside_temp_c"]["value"] == 0
    assert metrics["outside_temp_c"]["value"] == -5
    assert metrics["tpms_fl_bar"]["value"] == 2.5
    assert metrics["tpms_fr_bar"]["value"] is None
    assert metrics["tpms_fl_bar"]["unit"] == "bar"


async def test_owner_latest_openapi(tmp_path):
    import httpx

    from mileaf.app import create_app
    from mileaf.config import Settings

    app = create_app(
        Settings(
            database_dsn="postgresql://fixture@127.0.0.1:1/unused", state_file=tmp_path / "state"
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        schema = (await http.get("/openapi.json")).json()
    assert "/v1/vehicles/{vehicle_id}/vehicle-observations/latest" in schema["paths"]


async def test_driving_locations_and_map(client, db):
    await db.execute("""INSERT INTO drives(id,car_id,start_date,end_date,distance,duration_min,
        end_geofence_id,end_position_id) VALUES
        (90100,1,'2026-03-10','2026-03-10 01:00',0,0,1,1000),
        (90101,1,'2026-03-10','2026-03-10 02:00',20,30,1,1000)""")
    response = await client.get(BASE + "driving-locations", params={**RANGE, "limit": 1})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["totals"]["total_locations"] >= 1
    location = body["items"][0]
    evidence = await client.get(
        BASE + "drives",
        params={
            **RANGE,
            "completed_only": "true",
            "destination_location_id": location["location_id"],
        },
    )
    assert evidence.json()["matched_drives"] == location["visits"]
    areas = await client.get(
        BASE + "visited-areas", params={**RANGE, "zoom": 4, "bbox": "-180,-90,180,90"}
    )
    assert areas.status_code == 200, areas.text
    assert sum(x["count"] for x in areas.json()["items"]) >= 2


async def test_charging_habits_percentiles_and_soc_boundary(client, db):
    await db.execute("""INSERT INTO charging_processes(id,car_id,position_id,start_date,end_date,
        charge_energy_added,duration_min,start_battery_level,end_battery_level) VALUES
        (90201,2,1000,'2026-03-08 06:30','2026-03-08 08:00',0,0,0,100),
        (90202,2,1000,'2026-03-08 07:30','2026-03-08 09:00',20,100,50,100),
        (90203,2,1000,'2026-03-08 10:00','2026-03-08 11:00',NULL,-1,NULL,NULL)""")
    response = await client.get(
        "/v1/vehicles/2/charging-analysis",
        params={**RANGE, "tz": "America/New_York", "view": "habits"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 168
    assert body["totals"]["energy_added_kwh"]["p50"] == 10
    assert body["totals"]["duration_min"]["p90"] == 90
    assert body["totals"]["end_soc"][9]["count"] == 2


async def test_firmware_predecessor_survives_paging(client, db):
    await db.execute("""INSERT INTO updates(id,car_id,start_date,end_date,version) VALUES
        (90301,2,'2026-03-01','2026-03-01 01:00','same'),
        (90302,2,'2026-03-02','2026-03-02 01:00','same'),
        (90303,2,'2026-03-03',NULL,'next')""")
    path = "/v1/vehicles/2/software-updates"
    first = await client.get(path, params={"limit": 1})
    assert first.status_code == 200, first.text
    assert first.json()["items"][0]["status"] == "ongoing"
    second = await client.get(path, params={"limit": 1, "cursor": first.json()["next_cursor"]})
    item = second.json()["items"][0]
    assert item["id"] == 90302
    assert item["interval_seconds"] == 82800
    assert (
        await client.get(BASE + "software-updates", params={"cursor": first.json()["next_cursor"]})
    ).status_code == 400


async def test_drive_process_raw_power_and_time_weighted_speed(client, db):
    await db.execute("""INSERT INTO drives(id,car_id,start_date,end_date,ascent,descent)
        VALUES (90400,2,'2026-03-10','2026-03-10 00:10',0,20)""")
    await db.execute("""INSERT INTO positions(id,car_id,drive_id,date,latitude,longitude,
        speed,power,
        tpms_pressure_fl) VALUES (90400,2,90400,'2026-03-10',0,0,0,-10,2.5),
        (90401,2,90400,'2026-03-10 00:01',0,0,60,5,NULL),
        (90402,2,90400,'2026-03-10 00:03',0,0,90,0,0),
        (90403,2,90400,'2026-03-10 00:10',0,0,NULL,NULL,NULL)""")
    path = "/v1/drives/90400"
    series = await client.get(path + "/series", params={"fields": "power,tpms_fl"})
    assert series.status_code == 200, series.text
    assert series.json()["series"][0]["power_kw"] == -10
    assert series.json()["series"][0]["tpms_fl_bar"] == 2.5
    detail = (await client.get(path)).json()
    assert detail["ascent_m"] == 0
    response = await client.get(path + "/speed-histogram")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["totals"]["observed_seconds"] == 180
    assert body["totals"]["uncovered_seconds"] == 420
    assert body["items"][0]["seconds"] == 60
    assert body["items"][2]["seconds"] == 120


@pytest.mark.parametrize(
    "resource,extra",
    [
        ("battery-history", {}),
        ("environment-history", {}),
        ("activity-timeline", {}),
        ("driving-locations", {}),
        ("visited-areas", {"zoom": 4, "bbox": "-180,-90,180,90"}),
        ("charging-analysis", {}),
        ("software-updates", {}),
        ("vehicle-observations/latest", {}),
    ],
)
async def test_owner_authorization_vehicle_and_cache(client, resource, extra):
    params = (
        {**RANGE, **extra}
        if resource not in {"software-updates", "vehicle-observations/latest"}
        else extra
    )
    path = BASE + resource
    first = await client.get(path, params=params)
    assert first.status_code == 200, first.text
    assert first.headers["x-request-id"]
    second = await client.get(path, params=params, headers={"If-None-Match": first.headers["etag"]})
    assert second.status_code == 304
    assert (await client.get(path, params=params, headers={"Authorization": ""})).status_code == 401
    assert (await client.get("/v1/vehicles/999/" + resource, params=params)).status_code == 404


@pytest.mark.parametrize(
    "extra",
    [
        {"max_points": 1},
        {"max_points": 1001},
        {"tz": "invalid"},
        {"from": "2026-03-01"},
        {"from": "2026-01-01T00:00:00Z"},
        {"from": "2026-03-16T00:00:00Z", "to": "2026-03-17T00:00:00Z"},
    ],
)
async def test_history_rejects_invalid_queries(client, extra):
    assert (
        await client.get(BASE + "battery-history", params={**RANGE, **extra})
    ).status_code == 400


async def test_missing_optional_column_is_explicit_unsupported(client, db, app):
    await db.execute("ALTER TABLE positions DROP COLUMN usable_battery_level")
    # Restart the probe as a new service would after a schema change.
    from mileaf.probes import probe_database

    app.state.compatibility = await probe_database(app.state.pool)
    response = await client.get(BASE + "battery-history", params=RANGE)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "unsupported"
    meta = (await client.get("/v1/meta")).json()
    assert meta["owner_experience"]["features"]["battery_history"] is False
    assert meta["owner_experience"]["features"]["vehicle_observations"] is True
    assert meta["owner_experience"]["features"]["drive_series"] is True


async def test_fall_dst_repeated_hour_and_zero_soc(client, db):
    await db.execute("""INSERT INTO charging_processes(id,car_id,position_id,start_date,end_date,
        charge_energy_added,start_battery_level,end_battery_level) VALUES
        (90500,2,1000,'2025-11-02 05:30','2025-11-02 05:45',0,0,100),
        (90501,2,1000,'2025-11-02 06:30','2025-11-02 06:45',10,0,100)""")
    response = await client.get(
        "/v1/vehicles/2/charging-analysis",
        params={
            "from": "2025-11-02T00:00:00Z",
            "to": "2025-11-03T00:00:00Z",
            "tz": "America/New_York",
        },
    )
    assert response.status_code == 200, response.text
    cell = next(x for x in response.json()["items"] if x["weekday"] == 7 and x["hour"] == 1)
    assert cell["sessions"] == 2
    assert cell["energy_added_kwh"] == 10
    assert response.json()["totals"]["start_soc"][0]["count"] == 2


@pytest.mark.slow
async def test_owner_large_fixture(client, app, performance_seed, db):
    from time import perf_counter

    app.state.cache.ttl = 0
    await db.execute(
        """INSERT INTO updates(id,car_id,start_date,end_date,version)
        SELECT 200000+n,$1,'2024-03-15'::timestamp+n*interval '8 hours',
        '2024-03-15'::timestamp+n*interval '8 hours'+interval '1 hour','fixture'
        FROM generate_series(1,2000) n""",
        performance_seed,
    )
    await db.execute(
        """INSERT INTO states(id,car_id,state,start_date,end_date)
        SELECT 200000+n,$1,'online','2026-03-10'::timestamp+n*interval '1 minute',
        '2026-03-10'::timestamp+(n+1)*interval '1 minute' FROM generate_series(1,1000) n""",
        performance_seed,
    )
    base = f"/v1/vehicles/{performance_seed}/"
    bounds = {"from": "2024-03-15T12:00:00Z", "to": "2026-03-15T12:00:00Z"}
    for path, params in [
        ("battery-history", {**RANGE, "max_points": 100}),
        ("environment-history", {**RANGE, "max_points": 100}),
        ("activity-timeline", {**RANGE, "limit": 100}),
        ("driving-locations", bounds),
        ("visited-areas", {**bounds, "zoom": 4, "bbox": "-180,-90,180,90"}),
        ("charging-analysis", bounds),
        ("software-updates", {}),
        ("vehicle-observations/latest", {}),
    ]:
        start = perf_counter()
        response = await client.get(base + path, params=params)
        elapsed = perf_counter() - start
        assert response.status_code == 200, response.text
        assert elapsed < 5, (path, elapsed)
        assert len(response.json()["items"]) <= 1000
        print(f"{path}: {elapsed:.3f}s")


async def test_owner_openapi_has_typed_items(client):
    schemas = (await client.get("/openapi.json")).json()["components"]["schemas"]
    item = schemas["OwnerResult_BatteryHistoryPoint_"]["properties"]["items"]
    assert item["type"] == "array"
    assert item["items"]["$ref"].endswith("/BatteryHistoryPoint")


async def test_wrong_optional_elevation_type_does_not_break_old_drive(client, db, app):
    await db.execute("ALTER TABLE drives ALTER COLUMN ascent TYPE text USING ascent::text")
    from mileaf.probes import probe_database

    app.state.compatibility = await probe_database(app.state.pool)
    response = await client.get("/v1/drives/1")
    assert response.status_code == 200, response.text
    assert response.json()["ascent_m"] is None
    assert response.json()["elevation_quality"]["ascent"] == "unsupported"


async def test_timeline_preserves_actual_state(client, db):
    await db.execute("""INSERT INTO states(id,car_id,state,start_date,end_date) VALUES
        (90600,2,'online','2026-03-10','2026-03-10 01:00'),
        (90601,2,'asleep','2026-03-10 01:00','2026-03-10 02:00'),
        (90602,2,'offline','2026-03-10 02:00',NULL)""")
    response = await client.get("/v1/vehicles/2/activity-timeline", params=RANGE)
    assert response.status_code == 200, response.text
    assert {item["state"] for item in response.json()["items"]} == {"online", "asleep", "offline"}


async def test_speed_cache_revalidates_drive_existence(client, db):
    path = "/v1/drives/1/speed-histogram"
    first = await client.get(path)
    assert first.status_code == 200
    await db.execute("DELETE FROM drives WHERE id=1")
    response = await client.get(path, headers={"If-None-Match": first.headers["etag"]})
    assert response.status_code == 404
