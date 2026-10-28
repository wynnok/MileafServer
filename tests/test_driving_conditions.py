"""Temperature association through the public HTTP seam."""

import pytest

from tests.test_driving_analysis import PATH, WINDOW, seed_usage


async def test_conditions_pairing_and_evidence(client, db):
    await seed_usage(db)
    response = await client.get(PATH, params=WINDOW | {"view": "conditions"})
    assert response.status_code == 200, response.text
    body = response.json()
    group = body["temperature_bins"][0]
    assert (group["lower_c"], group["upper_c"], group["drives"]) == (10, 15, 9)
    assert group["paired_distance_km"] == 195
    assert group["paired_energy_kwh"] == 45
    assert group["consumption_wh_km"] == pytest.approx(45000 / 195)
    assert group["sample_status"] == "adequate"
    evidence = await client.get("/v1/vehicles/1/drives", params=group["evidence_filter"])
    assert evidence.status_code == 200
    assert evidence.json()["matched_drives"] == 9


async def test_negative_temperature_and_missing_and_sparse(client, db):
    await seed_usage(db)
    await db.execute(
        "UPDATE drives SET outside_temp_avg=CASE id WHEN 701 THEN -5 "
        "WHEN 702 THEN -0.1 WHEN 703 THEN 0 ELSE NULL END WHERE id BETWEEN 701 AND 710"
    )
    result = await client.get(PATH, params=WINDOW | {"view": "conditions"})
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["unknown_temperature_drives"] == 6
    assert [(g["lower_c"], g["drives"]) for g in body["temperature_bins"]] == [(-5, 2), (0, 1)]
    assert all(g["sample_status"] == "sparse" for g in body["temperature_bins"])


async def test_conditions_temperature_group_budget(client, db):
    await db.execute(
        "INSERT INTO drives(car_id,start_date,end_date,distance,duration_min,outside_temp_avg) "
        "SELECT 1,'2025-06-01'::timestamp,'2025-06-01 01:00'::timestamp,10,60,g*5 "
        "FROM generate_series(-50,50) g"
    )
    result = await client.get(PATH, params=WINDOW | {"view": "conditions"})
    assert result.status_code == 400


async def test_new_analysis_precision_and_coverage(client, db):
    await seed_usage(db)
    await db.execute("UPDATE drives SET distance=5.004 WHERE id=701")
    response = await client.get(PATH, params=WINDOW | {"view": "conditions"})
    body = response.json()
    assert body["summary"]["driving_distance_km"] == pytest.approx(195.004)
    paired = body["temperature_bins"][0]["coverage"]["paired"]
    assert paired["valid_distance_km"] == pytest.approx(195.004)
    assert paired["candidate_distance_km"] == pytest.approx(192.004)
    assert paired["unknown_distance_count"] == 2


async def test_conditions_etag_ignores_nested_generation_time(client, app, db):
    from datetime import UTC, datetime, timedelta

    from mileaf.dependencies import get_now

    await seed_usage(db)
    now = datetime(2026, 3, 15, 12, tzinfo=UTC)
    app.dependency_overrides[get_now] = lambda: now
    response = await client.get(PATH, params=WINDOW | {"view": "conditions"})
    now += timedelta(seconds=1)
    repeat = await client.get(
        PATH,
        params=WINDOW | {"view": "conditions"},
        headers={"If-None-Match": response.headers["etag"]},
    )
    assert repeat.status_code == 304


async def test_valid_coverage_amounts_exclude_negative_other_metric(client, db):
    await seed_usage(db)
    await db.execute("UPDATE drives SET duration_min=-5 WHERE id=701")
    body = (await client.get(PATH, params=WINDOW | {"view": "conditions"})).json()
    coverage = body["temperature_bins"][0]["coverage"]
    assert coverage["duration"]["candidate_distance_km"] == 192
    assert coverage["duration"]["valid_distance_km"] == 90
    assert coverage["paired"]["valid_duration_min"] == 90
