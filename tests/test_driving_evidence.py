"""Public driving evidence contracts against the isolated TeslaMate fixture."""

import json
from pathlib import Path

import pytest

BOUNDS = {"from": "2025-06-01T00:00:00Z", "to": "2025-07-01T00:00:00Z"}


async def seed_evidence(db):
    await db.execute("""
        INSERT INTO drives(id,car_id,start_date,end_date,distance,duration_min,
            start_rated_range_km,end_rated_range_km,outside_temp_avg)
        VALUES
            (601,1,'2025-06-01','2025-06-01 01:00',0,60,350,325,-5),
            (602,1,'2025-06-02','2025-06-02 01:00',10,60,350,325,0),
            (603,1,'2025-06-03','2025-06-03 01:00',49.99,60,350,325,4.9),
            (604,1,'2025-06-04','2025-06-04 01:00',50,60,350,350,5),
            (605,1,'2025-06-05','2025-06-05 01:00',-1,60,350,325,NULL),
            (606,1,'2025-06-06','2025-06-06 01:00',NULL,60,350,325,-0.1),
            (607,1,'2025-06-07',NULL,20,NULL,350,NULL,0)
    """)


async def test_combined_filters_page_over_the_complete_match_set(client, db):
    await seed_evidence(db)
    params = BOUNDS | {
        "completed_only": "true",
        "distance_min_km": 10,
        "distance_max_km": 50,
        "outside_temp_min_c": 0,
        "outside_temp_max_c": 5,
        "energy_status": "known",
        "tz": "Asia/Shanghai",
        "limit": 1,
    }
    first = await client.get("/v1/vehicles/1/drives", params=params)
    assert first.status_code == 200
    body = first.json()
    assert [row["id"] for row in body["items"]] == [603]
    assert body["matched_drives"] == 2
    assert body["query"]["vehicle_id"] == 1
    assert body["query"]["time_basis"] == "started_at"
    assert body["query"]["bucket"] is None
    assert body["query"]["filters"]["completed_only"] is True
    assert body["as_of"] == "2026-03-15T12:00:00Z"
    second = await client.get(
        "/v1/vehicles/1/drives", params=params | {"cursor": body["next_cursor"], "limit": 2}
    )
    assert second.status_code == 200
    assert [row["id"] for row in second.json()["items"]] == [602]
    assert second.json()["matched_drives"] == 2
    assert second.json()["next_cursor"] is None


async def test_numeric_filters_exclude_nonfinite_source_values(client, db):
    await seed_evidence(db)
    await db.execute("UPDATE drives SET outside_temp_avg='NaN' WHERE id=603")
    response = await client.get("/v1/vehicles/1/drives", params=BOUNDS | {"outside_temp_min_c": 0})
    assert response.status_code == 200
    assert [row["id"] for row in response.json()["items"]] == [607, 604, 602]
    assert response.json()["matched_drives"] == 3


async def test_energy_status_agrees_with_in_progress_nulls(client, db):
    await seed_evidence(db)
    # Even if an unfinished source row has provisional range endpoints, it has no final energy.
    await db.execute("UPDATE drives SET end_rated_range_km=325 WHERE id=607")
    known = await client.get("/v1/vehicles/1/drives", params=BOUNDS | {"energy_status": "known"})
    assert known.status_code == 200
    assert [row["id"] for row in known.json()["items"]] == [606, 605, 603, 602, 601]
    unknown = await client.get(
        "/v1/vehicles/1/drives", params=BOUNDS | {"energy_status": "unknown"}
    )
    assert unknown.status_code == 200
    assert [row["id"] for row in unknown.json()["items"]] == [607, 604]
    assert unknown.json()["items"][0]["energy_kwh"] is None


async def test_filter_boundaries_defaults_and_raw_negative_evidence(client, db):
    await seed_evidence(db)
    path = "/v1/vehicles/1/drives"
    for filters, expected in [
        ({"distance_min_km": 0, "distance_max_km": 10}, [601]),
        ({"distance_min_km": 10, "distance_max_km": 50}, [607, 603, 602]),
        ({"distance_min_km": 50}, [604]),
        ({"distance_min_km": -2, "distance_max_km": 0}, [605]),
        ({"outside_temp_min_c": -5, "outside_temp_max_c": 0}, [606, 601]),
    ]:
        result = await client.get(path, params=BOUNDS | filters)
        assert result.status_code == 200
        assert [row["id"] for row in result.json()["items"]] == expected
        assert result.json()["matched_drives"] == len(expected)
    negative = (await client.get(path, params=BOUNDS | {"distance_max_km": 0})).json()
    assert negative["items"][0]["distance_km"] == -1
    default = (await client.get(path, params=BOUNDS)).json()
    assert default["matched_drives"] == 7
    assert default["query"]["filters"] == {}
    assert default["query"]["tz"] == "UTC"
    assert default["method"] == "driving-match-v1"
    assert default["version"] == 1
    assert default["thresholds"] == {}
    empty = (await client.get("/v1/vehicles/2/drives", params=BOUNDS)).json()
    assert empty["items"] == []
    assert empty["matched_drives"] == 0
    assert empty["status"] == "no_records"


async def test_bound_cursors_reject_changed_queries_and_legacy_only_without_new_filters(client, db):
    import base64

    await seed_evidence(db)
    path = "/v1/vehicles/1/drives"
    first = (await client.get(path, params=BOUNDS | {"limit": 1})).json()
    cursor = first["next_cursor"]
    for changes in [
        {"distance_min_km": 0},
        {"outside_temp_min_c": 0},
        {"energy_status": "known"},
        {"completed_only": "true"},
        {"tz": "Asia/Shanghai"},
        {"from": "2025-06-02T00:00:00Z"},
        {"to": "2025-06-30T00:00:00Z"},
    ]:
        response = await client.get(path, params=BOUNDS | {"cursor": cursor} | changes)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"
    assert (
        await client.get("/v1/vehicles/2/drives", params=BOUNDS | {"cursor": cursor})
    ).status_code == 400
    legacy = base64.urlsafe_b64encode(b"2025-06-05T00:00:00+00:00|605").decode().rstrip("=")
    allowed = await client.get(path, params=BOUNDS | {"cursor": legacy})
    assert allowed.status_code == 200
    assert [row["id"] for row in allowed.json()["items"]] == [604, 603, 602, 601]
    forbidden = await client.get(path, params=BOUNDS | {"cursor": legacy, "completed_only": "true"})
    assert forbidden.status_code == 400


async def test_invalid_queries_and_clipped_upper_bound_keep_the_public_error_contract(client):
    path = "/v1/vehicles/1/drives"
    for params in [
        {"distance_min_km": "nan"},
        {"distance_max_km": "inf"},
        {"outside_temp_min_c": "-inf"},
        {"outside_temp_max_c": "NaN"},
        {"distance_min_km": 10, "distance_max_km": 10},
        {"outside_temp_min_c": 5, "outside_temp_max_c": -5},
        {"distance_min_km": "bad"},
        {"energy_status": "zero"},
        {"completed_only": "bad"},
        {"tz": "invalid/zone"},
        {"cursor": "bad"},
        {"limit": 101},
        {"from": "2025-06-01"},
        {"from": "2027-01-01T00:00:00Z", "to": "2027-02-01T00:00:00Z"},
    ]:
        response = await client.get(path, params=params)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"
        assert response.headers["x-request-id"]
    clipped = await client.get(path, params={"to": "2027-01-01T00:00:00Z"})
    assert clipped.status_code == 200
    assert clipped.json()["query"]["to"] == "2026-03-15T12:00:00Z"
    assert (await client.get(path, headers={"Authorization": ""})).status_code == 401
    assert (await client.get("/v1/vehicles/999/drives")).status_code == 404
    cached = await client.get(path, params=BOUNDS)
    unchanged = await client.get(
        path, params=BOUNDS, headers={"If-None-Match": cached.headers["etag"]}
    )
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert unchanged.headers["cache-control"] == "private, max-age=30"


async def test_driving_evidence_openapi_declares_filters_and_echo(client):
    schema = (await client.get("/openapi.json")).json()
    operation = schema["paths"]["/v1/vehicles/{vehicle_id}/drives"]["get"]
    params = {row["name"]: row for row in operation["parameters"]}
    assert params["completed_only"]["schema"]["default"] is False
    assert params["tz"]["schema"]["default"] == "UTC"
    for name in [
        "distance_min_km",
        "distance_max_km",
        "outside_temp_min_c",
        "outside_temp_max_c",
        "energy_status",
    ]:
        assert name in params
    page = schema["components"]["schemas"]["DrivePage"]
    assert {"query", "as_of", "matched_drives"} <= set(page["required"])
    frozen = json.loads((Path(__file__).parents[1] / "docs/api/openapi-0.3.0.json").read_text())
    assert schema == frozen


async def test_match_source_columns_stay_covered_by_the_required_probe(db, app):
    """The match rules add no optional column, so a missing source fails startup loudly."""
    await db.execute("ALTER TABLE drives DROP COLUMN distance")
    with pytest.raises(RuntimeError, match=r"drives\.distance \(expected float8\)"):
        async with app.router.lifespan_context(app):
            pass
