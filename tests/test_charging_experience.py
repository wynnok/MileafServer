"""Charging experience through the approved HTTP seam and isolated PostgreSQL."""

from datetime import UTC, datetime

import pytest

from mileaf.dependencies import get_now


@pytest.fixture(autouse=True)
def charging_clock(app):
    app.dependency_overrides[get_now] = lambda: datetime(2026, 10, 28, 12, tzinfo=UTC)


STATS = "/v1/vehicles/1/stats"
LIST = "/v1/vehicles/1/charging-sessions"
WINDOW = {
    "kind": "charging",
    "bucket": "month",
    "from": "2026-01-01T00:00:00Z",
    "to": "2026-04-01T00:00:00Z",
}


async def test_coverage_and_paired_unit_cost_preserve_legacy_ratio(client, db):
    await db.execute("UPDATE charging_processes SET charge_energy_added=NULL WHERE id=1")
    await db.execute("UPDATE charging_processes SET charge_energy_added=0 WHERE id=2")
    await db.execute("UPDATE charging_processes SET duration_min=NULL WHERE id=3")
    body = (await client.get(STATS, params=WINDOW)).json()
    totals = body["totals"]
    assert totals["sessions"] == 8
    assert totals["energy_known_sessions"] == 7
    assert totals["cost_known_sessions"] == 7
    assert totals["duration_known_sessions"] == 7
    assert totals["duration_min_total"] == 420
    assert totals["classification_known_sessions"] == 7
    assert totals["unknown_energy_kwh"] == 20
    assert totals["unknown_duration_min"] == 60
    assert totals["cost_known_energy_kwh"] == 100
    assert totals["cost_per_kwh"] == pytest.approx(72 / 100)
    assert totals["unit_cost_added"] == {
        "value": 0.48,
        "numerator_cost": 48,
        "denominator_kwh": 100,
        "eligible_sessions": 5,
        "excluded_cost_sessions": 2,
    }
    assert totals["unit_cost_grid"]["value"] == pytest.approx(72 / 154)
    detail = (await client.get("/v1/charging-sessions/1")).json()
    assert detail["unit_cost_added"]["value"] is None
    assert detail["unit_cost_added"]["excluded_cost_sessions"] == 1
    assert detail["unit_cost_grid"]["value"] == pytest.approx(12 / 22)
    active = (await client.get("/v1/charging-sessions/8")).json()
    assert active["unit_cost_added"] is None


@pytest.mark.parametrize("cost,energy,known", [(None, None, 0), (0, 0, 1)])
async def test_all_missing_and_real_zero_are_distinct(client, db, cost, energy, known):
    await db.execute(
        "UPDATE charging_processes SET cost=$1,charge_energy_added=$2,duration_min=NULL "
        "WHERE end_date IS NOT NULL",
        cost,
        energy,
    )
    totals = (await client.get(STATS, params=WINDOW)).json()["totals"]
    assert totals["cost"] == cost
    assert totals["cost_known_sessions"] == 8 * known
    assert totals["energy_known_sessions"] == 8 * known
    assert totals["duration_known_sessions"] == 0
    assert totals["unit_cost_added"]["value"] is None
    empty = (await client.get(STATS.replace("/1/", "/2/"), params=WINDOW)).json()["totals"]
    assert empty["sessions"] == empty["cost"] == 0
    assert empty["unit_cost_added"]["eligible_sessions"] == 0
    assert empty["unit_cost_added"]["value"] is None


async def test_end_day_drilldown_filters_and_bound_cursor(client, db):
    await db.execute(
        "UPDATE charging_processes SET start_date='2026-02-25 23:10',"
        "end_date='2026-02-26 03:30' WHERE id=1"
    )
    params = {
        "from": "2026-02-26T00:00:00Z",
        "to": "2026-02-27T00:00:00Z",
        "time_basis": "ended_at",
        "tz": "Asia/Shanghai",
    }
    listing = (await client.get(LIST, params=params)).json()
    assert listing["matched_sessions"] == 1
    assert [item["id"] for item in listing["items"]] == [1]
    assert listing["query"]["time_basis"] == "ended_at"
    stats = (await client.get(STATS, params={**WINDOW, **params, "bucket": "day"})).json()
    assert stats["totals"]["sessions"] == stats["buckets"][0]["sessions"] == 1
    search = (await client.get(LIST, params={"query": "  家  ", "limit": 1})).json()
    assert search["matched_sessions"] == 1
    assert search["query"]["filters"]["query"] == "家"
    assert search["items"][0]["location_id"]
    page_params = {"sort": "unit_cost_added_asc", "limit": 2, "cost_status": "known"}
    page = (await client.get(LIST, params=page_params)).json()
    assert page["items"][0]["id"] == 4
    cursor = page["next_cursor"]
    assert cursor
    assert (
        await client.get(LIST, params={**page_params, "cursor": cursor, "charging_type": "dc"})
    ).status_code == 400
    ids = [item["id"] for item in page["items"]]
    while page["next_cursor"]:
        page = (
            await client.get(LIST, params={**page_params, "cursor": page["next_cursor"]})
        ).json()
        ids += [item["id"] for item in page["items"]]
    assert len(ids) == len(set(ids)) == 8
    assert (await client.get(LIST, params={"query": "%"})).json()["matched_sessions"] == 0


async def test_location_accounts_and_drilldown_share_completed_match_set(client, db):
    await db.execute("UPDATE charging_processes SET address_id=NULL WHERE id=5")
    await db.execute("UPDATE addresses SET name='同名',road=NULL,city=NULL WHERE id IN (1,2)")
    params = {"from": WINDOW["from"], "to": WINDOW["to"], "limit": 1}
    url = "/v1/vehicles/1/charging-locations"
    response = await client.get(url, params=params)
    assert response.status_code == 200
    body = response.json()
    assert body["total_locations"] == 3
    assert body["unlocated_sessions"] == 1
    items = body["items"]
    while body["next_cursor"]:
        body = (await client.get(url, params={**params, "cursor": body["next_cursor"]})).json()
        items.extend(body["items"])
    assert len({item["location_id"] for item in items}) == 3
    assert sum(item["sessions"] for item in items) == 8
    assert sum(item["energy_added_kwh"] for item in items) == 160
    for item in items:
        drill = {
            **params,
            "limit": 100,
            "time_basis": "ended_at",
            "location_id": item["location_id"],
        }
        listing = (await client.get(LIST, params=drill)).json()
        assert listing["matched_sessions"] == item["sessions"]
        stats = (await client.get(STATS, params={**WINDOW, **drill})).json()
        assert stats["totals"]["unit_cost_added"] == item["unit_cost_added"]
        assert item["representative_session_id"] in [row["id"] for row in listing["items"]]
        assert item["coordinate_source"] == "latest_session_position"
    assert (await client.get(url)).status_code == 400
    assert (await client.get(url, params=params, headers={"Authorization": ""})).status_code == 401


async def test_raw_process_metrics_and_fixed_twenty_eighty_example(client, db):
    await db.execute(
        "UPDATE charging_processes SET start_date='2026-10-27 05:12',"
        "end_date='2026-10-27 05:46',start_battery_level=18,end_battery_level=82,"
        "charge_energy_added=46.8,charge_energy_used=49.5,cost=71.28,duration_min=34 WHERE id=1"
    )
    await db.execute("DELETE FROM charges WHERE charging_process_id=1")
    await db.execute(
        "INSERT INTO charges(id,charging_process_id,date,battery_level,charger_power,"
        "charge_energy_added,ideal_battery_range_km) SELECT 20000+n,1,"
        "'2026-10-27 05:12'::timestamp+n*interval '1 minute',"
        "CASE WHEN n=0 THEN 18 WHEN n=1 THEN 20 WHEN n>=29 THEN 80+(n-29)*2/5 "
        "ELSE 20+(n-1)*59/28 END,CASE WHEN n=17 THEN 175 ELSE 166 END,"
        "46.8*n/34,300 FROM generate_series(0,34) n"
    )
    response = await client.get("/v1/charging-sessions/1")
    assert response.status_code == 200
    body = response.json()
    assert body["unit_cost_added"]["value"] == pytest.approx(71.28 / 46.8)
    assert body["unit_cost_grid"]["value"] == 1.44
    metrics = body["process_metrics"]
    assert metrics["power_max_kw"] == 175
    assert metrics["power_valid_samples"] == 35
    assert metrics["observed_seconds"] == 2040
    assert metrics["uncovered_seconds"] == 0
    assert metrics["power_time_weighted_kw"] == pytest.approx(166.26470588235293)
    window = metrics["soc_windows"][0]
    assert window["status"] == "available"
    assert window["elapsed_seconds"] == 1680
    assert window["start_at"] == "2026-10-27T05:13:00Z"
    assert window["end_at"] == "2026-10-27T05:41:00Z"
    assert window["threshold_precision"] == "exact"
    # A two-point graph cannot expose the raw middle peak.
    series = (await client.get("/v1/charging-sessions/1/series", params={"points": 2})).json()
    assert max(point["power"] for point in series["series"]) == 166


async def test_extended_series_and_discrete_state_events(client, db):
    await db.execute(
        "UPDATE charges SET battery_heater_on=(id%2=0),charger_pilot_current=32 "
        "WHERE charging_process_id=1"
    )
    fields = "voltage_v,actual_current_a,pilot_current_a,outside_temp_c,battery_heater_on"
    response = await client.get(
        "/v1/charging-sessions/1/series", params={"fields": fields, "points": 2}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["points"] == 2
    assert body["series"][0]["voltage_v"] == 230
    assert body["series"][0]["pilot_current_a"] == 32
    assert "power" not in body["series"][0]
    assert body["state_events"]["total_events"] == 61
    assert len(body["state_events"]["events"]) == 61
    assert body["state_events"]["truncated"] is False
    assert body["capabilities"]["fields"]["voltage_v"] is True
    detail = (await client.get("/v1/charging-sessions/2")).json()
    assert detail["start_rated_range_km"] == 200
    assert detail["charger"]["fast_charger_type"] == "TeslaSupercharger"
    assert detail["charger"]["source"] == "latest_charge_sample"


@pytest.mark.parametrize(
    "samples,status,precision,seconds",
    [
        ([(0, 20), (60, 80)], "available", "exact", 60),
        ([(0, 18), (60, 22), (120, 82)], "available", "crossed", 60),
        ([(0, 20), (0, 80)], "gap_or_ambiguous", None, None),
        ([(0, 20), (600, 80)], "gap_or_ambiguous", None, None),
        ([(0, 20), (60, 19), (120, 80)], "gap_or_ambiguous", None, None),
        ([(0, 20), (60, None), (120, 80)], "gap_or_ambiguous", None, None),
        ([(0, 20), (60, 70)], "not_reached", None, None),
        ([(0, 80), (60, 82)], "not_reached", None, None),
        ([(0, 20)], "insufficient_samples", None, None),
        ([], "insufficient_samples", None, None),
    ],
)
async def test_observed_soc_windows_report_quality(client, db, samples, status, precision, seconds):
    await db.execute("DELETE FROM charges WHERE charging_process_id=1")
    for i, (offset, soc) in enumerate(samples):
        await db.execute(
            "INSERT INTO charges(id,charging_process_id,date,battery_level,charger_power,"
            "charge_energy_added,ideal_battery_range_km) VALUES ($1,1,"
            "'2026-01-05 08:00'::timestamp+$2*interval '1 second',$3,0,0,300)",
            50000 + i,
            offset,
            soc,
        )
    body = (await client.get("/v1/charging-sessions/1")).json()["process_metrics"]
    assert body["soc_windows"][0]["status"] == status
    assert body["soc_windows"][0]["threshold_precision"] == precision
    assert body["soc_windows"][0]["elapsed_seconds"] == seconds
    assert body["power_max_kw"] == (0 if samples else None)
    active = (await client.get("/v1/charging-sessions/8")).json()["process_metrics"]
    assert active["soc_windows"][0]["status"] == "incomplete_session"
    assert active["soc_windows"][0]["elapsed_seconds"] is None


async def test_nonuniform_power_weighting_excludes_large_gaps_and_retains_zero(client, db):
    await db.execute("DELETE FROM charges WHERE charging_process_id=1")
    await db.execute(
        "INSERT INTO charges(id,charging_process_id,date,battery_level,charger_power,"
        "charge_energy_added,ideal_battery_range_km) VALUES "
        "(60001,1,'2026-01-05 08:00',20,0,0,300),"
        "(60002,1,'2026-01-05 08:01',30,100,0,300),"
        "(60003,1,'2026-01-05 08:03',40,50,0,300),"
        "(60004,1,'2026-01-05 09:00',80,10,0,300)"
    )
    metrics = (await client.get("/v1/charging-sessions/1")).json()["process_metrics"]
    assert metrics["observed_seconds"] == 180
    assert metrics["uncovered_seconds"] == 3420
    assert metrics["power_time_weighted_kw"] == pytest.approx(66.6666666667)
    assert metrics["power_max_kw"] == 100


@pytest.mark.parametrize(
    "mutation",
    [
        "ALTER TABLE charges DROP COLUMN charger_voltage",
        "ALTER TABLE charges ALTER COLUMN charger_voltage TYPE text USING NULL",
    ],
)
async def test_optional_source_columns_declare_support_before_querying(db, app, mutation):
    import httpx

    await db.execute(mutation)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        await client.post("/v1/auth/setup", json={"password": "fixture password"})
        login = (await client.post("/v1/auth/login", json={"password": "fixture password"})).json()
        client.headers["Authorization"] = "Bearer " + login["token"]
        meta = (await client.get("/v1/meta")).json()
        assert meta["charging_experience"]["fields"]["voltage_v"] is False
        response = await client.get(
            "/v1/charging-sessions/1/series", params={"fields": "voltage_v"}
        )
        assert response.status_code == 200
        assert all(row["voltage_v"] is None for row in response.json()["series"])
        assert (await client.get("/v1/charging-sessions/1")).status_code == 200


async def test_discrete_event_budget_declares_truncation(client, db):
    await db.execute("DELETE FROM charges WHERE charging_process_id=1")
    await db.execute(
        "INSERT INTO charges(id,charging_process_id,date,battery_level,charger_power,"
        "charge_energy_added,ideal_battery_range_km,battery_heater) SELECT 70000+n,1,"
        "'2026-01-05 08:00'::timestamp+n*interval '1 second',40,0,0,300,n%2=0 "
        "FROM generate_series(0,1200) n"
    )
    body = (
        await client.get(
            "/v1/charging-sessions/1/series", params={"fields": "power,battery_heater", "points": 2}
        )
    ).json()
    assert body["points"] == 2
    assert body["state_events"]["total_events"] == 1201
    assert len(body["state_events"]["events"]) == 1000
    assert body["state_events"]["truncated"] is True


@pytest.mark.parametrize(
    "sort", ["started_desc", "ended_desc", "duration_desc", "unit_cost_added_asc"]
)
async def test_every_sort_pages_without_duplicates_and_places_nulls_last(client, db, sort):
    await db.execute("UPDATE charging_processes SET duration_min=NULL WHERE id=1")
    await db.execute("UPDATE charging_processes SET charge_energy_added=NULL WHERE id=2")
    params = {"sort": sort, "limit": 2}
    body = (await client.get(LIST, params=params)).json()
    items = body["items"][:]
    while body["next_cursor"]:
        body = (await client.get(LIST, params={**params, "cursor": body["next_cursor"]})).json()
        items.extend(body["items"])
    assert len(items) == len({item["id"] for item in items}) == 9
    if sort == "ended_desc":
        assert items[-1]["id"] == 8
    elif sort == "duration_desc":
        assert {item["id"] for item in items[-2:]} == {1, 8}
    elif sort == "unit_cost_added_asc":
        assert items[0]["id"] == 4
        assert {item["id"] for item in items[-3:]} == {2, 3, 8}


async def test_future_upper_bound_and_semantic_etag_with_advancing_clock(client, app):
    from datetime import timedelta

    first = await client.get(STATS, params=WINDOW)
    later = datetime(2026, 10, 28, 12, tzinfo=UTC) + timedelta(seconds=1)
    app.dependency_overrides[get_now] = lambda: later
    second = await client.get(
        STATS, params=WINDOW, headers={"If-None-Match": first.headers["etag"]}
    )
    assert second.status_code == 304
    assert first.headers["etag"].startswith("W/")
    future = {
        **WINDOW,
        "bucket": "day",
        "from": "2026-10-27T00:00:00Z",
        "to": "2026-11-01T00:00:00Z",
    }
    body = (await client.get(STATS, params=future)).json()
    assert body["query"]["to"] == "2026-10-28T12:00:01Z"
    assert len(body["buckets"]) == 2
    # Requested future bounds keep a cursor usable as now advances.
    params = {"to": "2027-01-01T00:00:00Z", "limit": 1}
    page = (await client.get(LIST, params=params)).json()
    app.state.cache.ttl = 0
    app.dependency_overrides[get_now] = lambda: later + timedelta(seconds=1)
    assert (
        await client.get(LIST, params={**params, "cursor": page["next_cursor"]})
    ).status_code == 200


async def test_location_identity_does_not_merge_names_and_retains_missing_coordinates(client, db):
    await db.execute("UPDATE addresses SET name='同名',road=NULL,city=NULL WHERE id IN (1,2)")
    await db.execute("UPDATE charging_processes SET geofence_id=NULL WHERE id=1")
    await db.execute("ALTER TABLE positions ALTER COLUMN latitude DROP NOT NULL")
    await db.execute("UPDATE positions SET latitude=NULL WHERE id=999999")
    params = {"from": WINDOW["from"], "to": WINDOW["to"]}
    body = (await client.get("/v1/vehicles/1/charging-locations", params=params)).json()
    assert body["total_locations"] == 2
    assert len({item["location_id"] for item in body["items"]}) == 2
    assert all(item["identity_kind"] == "address" for item in body["items"])
    assert all(item["location"]["display_name"] == "同名" for item in body["items"])
    assert all(item["coordinate_source"] is None for item in body["items"])


@pytest.mark.parametrize(
    "params",
    [
        {"query": "x" * 101},
        {"time_basis": "bad"},
        {"sort": "offset"},
        {"cost_status": "free"},
        {"charging_type": "fast"},
        {"location_id": "bad"},
    ],
)
async def test_invalid_charging_filters_are_uniform_errors(client, params):
    for path, base in [(LIST, {}), (STATS, WINDOW)]:
        response = await client.get(path, params={**base, **params})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"
        assert response.headers["x-request-id"]


@pytest.mark.slow
async def test_charging_queries_remain_bounded_on_large_fixture(client, app, performance_seed):
    from time import perf_counter

    app.state.cache.ttl = 0
    prefix = f"/v1/vehicles/{performance_seed}"
    bounds = {"from": "2025-04-01T00:00:00Z", "to": "2026-04-01T00:00:00Z"}
    for path, params in [
        ("/charging-sessions", {**bounds, "limit": 20, "sort": "unit_cost_added_asc"}),
        ("/charging-locations", {**bounds, "limit": 20}),
        ("/stats", {**bounds, "kind": "charging", "bucket": "month"}),
    ]:
        start = perf_counter()
        response = await client.get(prefix + path, params=params)
        assert response.status_code == 200, response.text
        assert perf_counter() - start < 5
        body = response.json()
        assert len(body.get("items", body.get("buckets", []))) <= 20


async def test_frozen_openapi_exposes_charging_contract(client):
    import json
    from pathlib import Path

    schema = (await client.get("/openapi.json")).json()
    frozen = Path(__file__).parents[1] / "docs/api/openapi-0.2.0.json"
    assert schema == json.loads(frozen.read_text())
    assert schema["info"]["version"] == "0.2.0"
    assert "/v1/vehicles/{vehicle_id}/charging-locations" in schema["paths"]
    echo = schema["components"]["schemas"]["ChargingQueryEcho"]["properties"]
    assert "from" in echo and "from_" not in echo
