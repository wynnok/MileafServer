"""Public ST03 usage contracts against the isolated TeslaMate fixture."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mileaf.app import create_app
from mileaf.config import Settings

PATH = "/v1/vehicles/1/driving-analysis"
WINDOW = {"view": "usage", "from": "2025-06-01T00:00:00Z", "to": "2025-07-01T00:00:00Z"}
REASONS = {
    "distance": ("missing_distance", "invalid_distance", "negative_distance"),
    "duration": ("missing_duration", "negative_duration"),
    "speed": (
        "missing_distance",
        "invalid_distance",
        "negative_distance",
        "missing_duration",
        "negative_duration",
        "zero_duration",
    ),
    "paired": (
        "missing_distance",
        "invalid_distance",
        "non_positive_distance",
        "missing_energy",
        "invalid_energy",
    ),
}


async def seed_usage(db):
    """Hand-computed usage scenario; car 1 uses rated range and efficiency 0.15 kWh/km.

    701 Mon  5 km /  30 min / 1.5 kWh      706 Fri  NULL / 45 min / 7.5 kWh
    702 Tue 10 km /  30 min / 3.0 kWh      707 Sun 100 km / NULL  / 22.5 kWh
    703 Tue 60 km /  60 min / 15.0 kWh     708 in progress, never a candidate
    704 Thu  0 km /  10 min / NULL         709 Tue  20 km /  0 min / 3.0 kWh
    705 Fri -3 km /  20 min / 4.5 kWh      710 Wed  NULL / -5 min / 3.0 kWh
    """
    await db.execute("""
        INSERT INTO drives(id,car_id,start_date,end_date,distance,duration_min,
            start_rated_range_km,end_rated_range_km,outside_temp_avg)
        VALUES
            (701,1,'2025-06-02 08:00','2025-06-02 08:30',5,30,350,340,10),
            (702,1,'2025-06-03 08:00','2025-06-03 08:30',10,30,350,330,10),
            (703,1,'2025-06-03 18:00','2025-06-03 19:00',60,60,350,250,10),
            (704,1,'2025-06-05 08:00','2025-06-05 08:10',0,10,350,350,10),
            (705,1,'2025-06-06 08:00','2025-06-06 08:20',-3,20,350,320,10),
            (706,1,'2025-06-06 12:00','2025-06-06 12:45',NULL,45,350,300,10),
            (707,1,'2025-06-08 09:00','2025-06-08 10:40',100,NULL,350,200,10),
            (708,1,'2025-06-09 09:00',NULL,30,NULL,350,320,10),
            (709,1,'2025-06-10 08:00','2025-06-10 08:00',20,0,350,330,10),
            (710,1,'2025-06-11 08:00','2025-06-11 08:30',NULL,-5,350,330,10)
    """)


async def usage(client, **params):
    response = await client.get(PATH, params=WINDOW | params)
    assert response.status_code == 200, response.text
    return response.json()


def assert_coverage(body, name, candidate, valid, reasons):
    assert body == {
        "candidate_count": candidate,
        "valid_count": valid,
        "excluded_count": candidate - valid,
        "excluded_by_reason": dict(zip(REASONS[name], reasons, strict=True)),
    }
    assert body["excluded_count"] == sum(body["excluded_by_reason"].values())


async def test_usage_requires_an_explicit_bounded_window_and_a_supported_view(client):
    base = {"from": "2025-06-01T00:00:00Z", "to": "2025-07-01T00:00:00Z"}
    for params in [
        {},
        {"view": "usage"},
        {"view": "usage", "from": base["from"]},
        {"view": "usage", "to": base["to"]},
        {"view": "usage", **base, "from": "2025-06-01"},
        {"view": "usage", **base, "from": base["to"], "to": base["from"]},
        {"view": "usage", "from": "2023-06-01T00:00:00Z", "to": "2025-06-01T00:00:00Z"},
        {"view": "usage", **base, "tz": "Mars/Olympus"},
        {"view": "usage", **base, "distance_min_km": "nan"},
        {"view": "usage", **base, "distance_min_km": 10, "distance_max_km": 10},
        {"view": "usage", **base, "outside_temp_min_c": 5, "outside_temp_max_c": -5},
        {"view": "usage", **base, "energy_status": "zero"},
        # Clipped to the request moment, the window becomes empty.
        {"view": "usage", "from": "2026-03-15T12:00:00Z", "to": "2026-04-01T00:00:00Z"},
    ]:
        response = await client.get(PATH, params=params)
        assert response.status_code == 400, params
        assert response.json()["error"]["code"] == "invalid_request"
        assert response.headers["x-request-id"]
    longest = await client.get(
        PATH, params=WINDOW | {"from": "2023-06-02T00:00:00Z", "to": "2025-06-01T00:00:00Z"}
    )
    assert longest.status_code == 200
    assert (await client.get(PATH, params=WINDOW, headers={"Authorization": ""})).status_code == 401
    unknown = await client.get("/v1/vehicles/999/driving-analysis", params=WINDOW)
    assert unknown.status_code == 404


async def test_no_records_keeps_the_fixed_group_shape(client):
    body = (await client.get("/v1/vehicles/2/driving-analysis", params=WINDOW)).json()
    assert body["view"] == "usage"
    assert body["status"] == "no_records"
    assert body["method"] == "usage-distribution-v1"
    assert body["version"] == 1
    assert body["validation"] == "provisional"
    assert body["thresholds"] == {
        "distance_bin_edge_1_km": 10.0,
        "distance_bin_edge_2_km": 50.0,
        "distance_bins": 3.0,
        "weekday_bins": 7.0,
        "percentile_low": 50.0,
        "percentile_high": 90.0,
        "speed_duration_exclusive_min": 0.0,
        "paired_distance_exclusive_min_km": 0.0,
    }
    assert body["query"] == {
        "vehicle_id": 2,
        "from": "2025-06-01T00:00:00Z",
        "to": "2025-07-01T00:00:00Z",
        "tz": "UTC",
        "bucket": None,
        "time_basis": "started_at",
        "filters": {"completed_only": True},
    }
    assert body["as_of"] == "2026-03-15T12:00:00Z"
    assert body["active_days"] == 0
    assert body["partial_today"] is False
    # Without candidates every additive term stays 0 and every ratio stays null.
    assert body["driving_distance_km"] == 0
    assert body["driving_duration_min"] == 0
    for name in (
        "distance_per_active_day_km",
        "mean_speed_kmh",
        "distance_p50_km",
        "distance_p90_km",
        "duration_p50_min",
        "duration_p90_min",
    ):
        assert body[name] is None
    assert body["coverage"]["candidate_count"] == 0
    for name, reasons in REASONS.items():
        assert_coverage(body["coverage"][name], name, 0, 0, (0,) * len(reasons))
    assert [(row["lower_km"], row["upper_km"]) for row in body["distance_bins"]] == [
        (0.0, 10.0),
        (10.0, 50.0),
        (50.0, None),
    ]
    for index, row in enumerate(body["distance_bins"]):
        assert row["drives"] == 0
        assert row["distance_km"] == 0
        assert row["duration_min"] == 0
        assert row["paired_energy_kwh"] == 0
        assert row["paired_distance_km"] == 0
        assert row["consumption_wh_km"] is None
        assert row["status"] == "no_records"
        bounds = [(0.0, 10.0), (10.0, 50.0), (50.0, None)][index]
        evidence = {
            "from": "2025-06-01T00:00:00Z",
            "to": "2025-07-01T00:00:00Z",
            "tz": "UTC",
            "distance_min_km": bounds[0],
            "distance_max_km": bounds[1],
            "completed_only": True,
            "method": "driving-match-v1",
            "version": 1,
        }
        # Unset bounds stay absent, so the object replays verbatim as query parameters.
        assert row["evidence_filter"] == {k: v for k, v in evidence.items() if v is not None}
    assert [row["weekday"] for row in body["weekday_bins"]] == [1, 2, 3, 4, 5, 6, 7]
    for row in body["weekday_bins"]:
        assert row["drives"] == 0
        assert row["distance_km"] == 0
        assert row["duration_min"] == 0
        assert row["status"] == "no_records"
        assert "evidence_filter" not in row


async def test_usage_metrics_are_weighted_over_one_match_set(client, db):
    await seed_usage(db)
    body = await usage(client)
    assert body["status"] == "available"
    # 708 is still running, so the completed match set holds the nine remaining drives.
    assert body["coverage"]["candidate_count"] == 9
    assert body["active_days"] == 7
    assert body["partial_today"] is False
    assert body["driving_distance_km"] == 195
    # Recorded duration only; 707 and 710 never fall back to their timestamp span.
    assert body["driving_duration_min"] == 195
    assert body["distance_per_active_day_km"] == 27.9
    # 195 km over 130 positive minutes: 75*60/130 = 34.6, not the 22.5 mean of trip speeds.
    assert body["mean_speed_kmh"] == 35
    assert_coverage(body["coverage"]["distance"], "distance", 9, 6, (2, 0, 1))
    assert_coverage(body["coverage"]["duration"], "duration", 9, 7, (1, 1))
    assert_coverage(body["coverage"]["speed"], "speed", 9, 4, (2, 0, 1, 1, 0, 1))
    assert_coverage(body["coverage"]["paired"], "paired", 9, 5, (2, 0, 2, 0, 0))


async def test_percentiles_use_the_whole_valid_set_not_the_group_medians(client, db):
    await seed_usage(db)
    body = await usage(client)
    # Six valid distances [0,5,10,20,60,100]: P50 interpolates to 15, P90 to 80.
    assert body["distance_p50_km"] == 15
    assert body["distance_p90_km"] == 80
    # Seven valid durations [0,10,20,30,30,45,60]: P50 30, P90 51 on a different record count.
    assert body["duration_p50_min"] == 30
    assert body["duration_p90_min"] == 51
    groups = body["distance_bins"]
    medians = [2.5, 15.0, 80.0]
    assert [row["drives"] for row in groups] == [2, 2, 2]
    # Averaging the group medians (32.5) or weighting them by drives never yields 15.
    assert sum(medians) / len(medians) != body["distance_p50_km"]


async def test_distance_groups_hold_their_bounds_and_the_zero_km_rules(client, db):
    await seed_usage(db)
    short, medium, long = (await usage(client))["distance_bins"]
    assert (short["lower_km"], short["upper_km"]) == (0.0, 10.0)
    assert (medium["lower_km"], medium["upper_km"]) == (10.0, 50.0)
    assert (long["lower_km"], long["upper_km"]) == (50.0, None)
    # 701 (5 km) and 704 (0 km): a zero-kilometre drive still counts in its group.
    assert short["drives"] == 2
    assert short["distance_km"] == 5
    assert short["duration_min"] == 40
    assert short["paired_energy_kwh"] == 1.5
    assert short["paired_distance_km"] == 5
    assert short["consumption_wh_km"] == 300
    assert short["status"] == "available"
    assert_coverage(short["coverage"]["distance"], "distance", 2, 2, (0, 0, 0))
    assert_coverage(short["coverage"]["duration"], "duration", 2, 2, (0, 0))
    # The zero-kilometre drive never joins the paired subset of its own group.
    assert_coverage(short["coverage"]["paired"], "paired", 2, 1, (0, 0, 1, 0, 0))
    # 702 opens the medium group at exactly 10 km; 709 records a zero duration.
    assert medium["drives"] == 2
    assert medium["distance_km"] == 30
    assert medium["duration_min"] == 30
    assert medium["paired_energy_kwh"] == 6
    assert medium["paired_distance_km"] == 30
    assert medium["consumption_wh_km"] == 200
    # 707 has no recorded duration, so the group's duration set is smaller than its drives.
    assert long["drives"] == 2
    assert long["distance_km"] == 160
    assert long["duration_min"] == 60
    assert_coverage(long["coverage"]["duration"], "duration", 2, 1, (1, 0))
    assert long["paired_energy_kwh"] == 37.5
    assert long["paired_distance_km"] == 160
    assert long["consumption_wh_km"] == 234
    # 705 (-3 km) and 706 (NULL) never enter a group; the period coverage explains both.
    assert sum(row["drives"] for row in (short, medium, long)) == 6


async def test_weekday_groups_follow_the_local_started_at(client, db):
    await seed_usage(db)
    body = await usage(client)
    rows = body["weekday_bins"]
    assert [row["weekday"] for row in rows] == [1, 2, 3, 4, 5, 6, 7]
    assert [row["drives"] for row in rows] == [1, 3, 1, 1, 2, 0, 1]
    assert sum(row["drives"] for row in rows) == body["coverage"]["candidate_count"]
    monday, tuesday, wednesday, thursday, friday, saturday, sunday = rows
    assert set(monday) == {"weekday", "drives", "distance_km", "duration_min", "status", "coverage"}
    assert (monday["distance_km"], monday["duration_min"]) == (5, 30)
    assert (tuesday["distance_km"], tuesday["duration_min"]) == (90, 90)
    # 710 has neither a valid distance nor a valid duration: candidates without any input.
    assert wednesday["status"] == "insufficient_data"
    assert wednesday["distance_km"] is None
    assert wednesday["duration_min"] is None
    assert_coverage(wednesday["coverage"]["distance"], "distance", 1, 0, (1, 0, 0))
    assert_coverage(wednesday["coverage"]["duration"], "duration", 1, 0, (0, 1))
    assert (thursday["distance_km"], thursday["duration_min"]) == (0, 10)
    # 705 keeps its negative distance as an exclusion reason while 706 is simply missing.
    assert friday["status"] == "available"
    assert friday["distance_km"] is None
    assert friday["duration_min"] == 65
    assert_coverage(friday["coverage"]["distance"], "distance", 2, 0, (1, 0, 1))
    assert (saturday["drives"], saturday["status"]) == (0, "no_records")
    assert sunday["distance_km"] == 100
    assert sunday["duration_min"] is None
    assert_coverage(sunday["coverage"]["duration"], "duration", 1, 0, (1, 0))


async def test_local_calendar_owns_dst_days_and_the_partial_current_day(client, db):
    from tests.fixtures.seeds.vehicles import seed_car

    vehicle = await seed_car(db, efficiency=0.2, name="DST 观察车")
    path = f"/v1/vehicles/{vehicle}/driving-analysis"
    # Berlin springs forward on 2025-03-30, so that local day lasts 23 hours:
    # 2025-03-29T23:00Z .. 2025-03-30T22:00Z. It falls back on 2025-10-26, a 25 hour day.
    await db.execute(
        """INSERT INTO drives(id,car_id,start_date,end_date,distance,duration_min,
            start_rated_range_km,end_rated_range_km)
        VALUES (711,$1,'2025-03-29 23:30','2025-03-30 00:00',20,30,350,330),
               (712,$1,'2025-03-30 00:30','2025-03-30 01:30',40,60,350,290),
               (713,$1,'2025-03-30 22:30','2025-03-30 23:30',60,60,350,260),
               (714,$1,'2025-10-25 22:30','2025-10-25 23:30',20,60,350,330),
               (715,$1,'2025-10-26 22:30','2025-10-26 23:30',40,60,350,290)""",
        vehicle,
    )
    short_day = {
        "view": "usage",
        "from": "2025-03-29T23:00:00Z",
        "to": "2025-03-30T22:00:00Z",
    }
    response = await client.get(path, params=short_day | {"tz": "Europe/Berlin"})
    assert response.status_code == 200, response.text
    local = response.json()
    # 711 starts on the 29th in UTC but on the 30th locally; 713 is already outside the window.
    assert local["coverage"]["candidate_count"] == 2
    assert local["active_days"] == 1
    assert [row["drives"] for row in local["weekday_bins"]] == [0, 0, 0, 0, 0, 0, 2]
    utc = (await client.get(path, params=short_day | {"tz": "UTC"})).json()
    assert utc["active_days"] == 2
    assert [row["drives"] for row in utc["weekday_bins"]] == [0, 0, 0, 0, 0, 1, 1]
    # The 25 hour local day keeps both drives on 2025-10-26 although their UTC dates differ.
    long_day = {
        "view": "usage",
        "tz": "Europe/Berlin",
        "from": "2025-10-25T22:00:00Z",
        "to": "2025-10-26T23:00:00Z",
    }
    fallback = (await client.get(path, params=long_day)).json()
    assert fallback["active_days"] == 1
    assert [row["drives"] for row in fallback["weekday_bins"]] == [0, 0, 0, 0, 0, 0, 2]
    assert (await client.get(path, params=long_day | {"tz": "UTC"})).json()["active_days"] == 2

    # An explicit future bound is clipped to the request moment and covers today so far.
    current = await usage(client, **{"from": "2026-03-01T00:00:00Z", "to": "2026-04-01T00:00:00Z"})
    assert current["query"]["to"] == "2026-03-15T12:00:00Z"
    assert current["partial_today"] is True
    # Los Angeles midnight is 07:00Z: ending exactly there covers no part of the current day.
    angeles = {"view": "usage", "tz": "America/Los_Angeles", "from": "2026-03-14T00:00:00Z"}
    closed = (await client.get(PATH, params=angeles | {"to": "2026-03-15T07:00:00Z"})).json()
    assert closed["partial_today"] is False
    open_day = (await client.get(PATH, params=angeles | {"to": "2026-03-15T07:00:01Z"})).json()
    assert open_day["partial_today"] is True


async def test_candidates_without_any_valid_input_stay_insufficient_data(client, db):
    await db.execute("""
        INSERT INTO drives(id,car_id,start_date,end_date,distance,duration_min)
        VALUES (720,1,'2025-06-12 08:00','2025-06-12 08:30',NULL,NULL)
    """)
    body = await usage(client, **{"from": "2025-06-12T00:00:00Z", "to": "2025-06-13T00:00:00Z"})
    assert body["status"] == "insufficient_data"
    assert body["coverage"]["candidate_count"] == 1
    # A candidate still marks its local day as active even without any valid value.
    assert body["active_days"] == 1
    assert body["driving_distance_km"] is None
    assert body["driving_duration_min"] is None
    assert body["distance_per_active_day_km"] is None
    assert body["mean_speed_kmh"] is None
    assert body["distance_p50_km"] is None
    assert body["duration_p90_min"] is None
    assert [row["drives"] for row in body["distance_bins"]] == [0, 0, 0]
    assert [row["drives"] for row in body["weekday_bins"]] == [0, 0, 0, 1, 0, 0, 0]
    assert body["weekday_bins"][3]["status"] == "insufficient_data"


async def test_evidence_filters_open_exactly_their_group_over_every_page(client, db):
    await seed_usage(db)
    body = await usage(client)
    expected = [[704, 701], [709, 702], [707, 703]]
    for index, group in enumerate(body["distance_bins"]):
        evidence = group["evidence_filter"]
        assert evidence["method"] == "driving-match-v1"
        assert evidence["version"] == 1
        assert evidence["completed_only"] is True
        assert evidence["from"] == body["query"]["from"]
        assert evidence["to"] == body["query"]["to"]
        assert evidence["tz"] == body["query"]["tz"]
        params = {key: value for key, value in evidence.items() if key not in {"method", "version"}}
        walked: list[int] = []
        cursor: str | None = None
        for _ in range(group["drives"] + 1):
            page_request = await client.get(
                "/v1/vehicles/1/drives",
                params=params | {"limit": 1} | ({"cursor": cursor} if cursor else {}),
            )
            assert page_request.status_code == 200
            page = page_request.json()
            # The group count always equals the complete evidence count, not the page size.
            assert page["matched_drives"] == group["drives"]
            walked.extend(row["id"] for row in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert cursor is None
        assert walked == expected[index]
        assert len(set(walked)) == group["drives"]


async def test_inherited_filters_narrow_the_groups_and_their_evidence(client, db):
    await seed_usage(db)
    body = await usage(
        client,
        distance_min_km=10,
        distance_max_km=20,
        outside_temp_min_c=5,
        energy_status="known",
    )
    filters = {
        "distance_min_km": 10.0,
        "distance_max_km": 20.0,
        "outside_temp_min_c": 5.0,
        "energy_status": "known",
        "completed_only": True,
    }
    assert body["query"]["filters"] == filters
    # Only 702 (10 km, 10 °C, known energy) survives the intersection.
    assert body["coverage"]["candidate_count"] == 1
    assert [row["drives"] for row in body["distance_bins"]] == [0, 1, 0]
    assert body["distance_bins"][1]["evidence_filter"] == {
        "from": "2025-06-01T00:00:00Z",
        "to": "2025-07-01T00:00:00Z",
        "tz": "UTC",
    } | filters | {"method": "driving-match-v1", "version": 1}
    # The inherited bounds already empty the other groups, and the evidence list rejects a
    # range whose minimum reaches its maximum, so those groups carry no filter at all.
    assert body["distance_bins"][0]["evidence_filter"] is None
    assert body["distance_bins"][2]["evidence_filter"] is None
    # completed_only is fixed by the analysis contract, whatever a client sends.
    forced = await usage(client, completed_only="false")
    assert forced["query"]["filters"] == {"completed_only": True}
    assert forced["coverage"]["candidate_count"] == 9


async def test_usage_stays_within_two_bounded_queries(app, client, db, database_dsn):
    import asyncio

    import asyncpg

    from mileaf.dependencies import get_pool

    await seed_usage(db)
    queries: list[str] = []

    async def record_queries(connection: asyncpg.Connection) -> None:
        connection.add_query_logger(lambda query: queries.append(query.query))

    pool = await asyncpg.create_pool(
        database_dsn,
        min_size=1,
        max_size=1,
        init=record_queries,
        server_settings={"default_transaction_read_only": "on"},
    )
    app.dependency_overrides[get_pool] = lambda: pool
    try:
        # Warm asyncpg's prepared statements and codecs outside the measured request.
        await client.get(PATH, params=WINDOW)
        await asyncio.sleep(0)
        queries.clear()
        measured = await client.get(PATH, params=WINDOW | {"distance_min_km": -5})
        await asyncio.sleep(0)
        assert measured.status_code == 200
        business = [query for query in queries if "FROM drives d" in query]
        assert len(business) == 2
        assert len([query for query in business if "percentile_cont(0.5)" in query]) == 1
        assert len([query for query in business if "GROUPING SETS" in query]) == 1
        # Neither statement loops per group; both aggregate the whole match set at once.
        assert all("generate_series" not in query for query in business)
        assert queries.count("SET LOCAL statement_timeout = '5s'") == len(business)
    finally:
        app.dependency_overrides.pop(get_pool)
        await pool.close()


async def test_conditional_requests_ignore_the_generation_time(client, db, app):
    from mileaf.dependencies import get_now

    await seed_usage(db)
    first = await client.get(PATH, params=WINDOW)
    assert first.status_code == 200
    assert first.headers["etag"].startswith('W/"')
    assert first.headers["cache-control"] == "private, max-age=30"
    # One day later: still inside the bearer token's lifetime, so only as_of moves.
    app.dependency_overrides[get_now] = lambda: datetime(2026, 3, 16, 9, tzinfo=UTC)
    unchanged = await client.get(
        PATH, params=WINDOW, headers={"If-None-Match": first.headers["etag"]}
    )
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert unchanged.headers["cache-control"] == "private, max-age=30"
    # A different local calendar is a different representation, not a revalidation hit.
    changed = await client.get(
        PATH,
        params=WINDOW | {"tz": "Europe/Berlin"},
        headers={"If-None-Match": first.headers["etag"]},
    )
    assert changed.status_code == 200


async def test_driving_analysis_openapi_limits_the_declared_view(client):
    schema = (await client.get("/openapi.json")).json()
    operation = schema["paths"]["/v1/vehicles/{vehicle_id}/driving-analysis"]["get"]
    params = {row["name"]: row for row in operation["parameters"]}
    assert params["view"]["required"] is True
    assert params["view"]["schema"] == {
        "enum": ["usage", "conditions"],
        "type": "string",
        "title": "View",
    }
    assert params["from"]["required"] is True
    assert params["to"]["required"] is True
    assert params["tz"]["schema"]["default"] == "UTC"
    # completed_only is fixed by the contract instead of being a client choice.
    assert "completed_only" not in params
    for name in (
        "distance_min_km",
        "distance_max_km",
        "outside_temp_min_c",
        "outside_temp_max_c",
        "energy_status",
    ):
        assert name in params
    model = schema["components"]["schemas"]["DrivingAnalysisUsage"]["properties"]
    assert {
        "view",
        "status",
        "method",
        "version",
        "validation",
        "thresholds",
        "query",
        "as_of",
        "active_days",
        "partial_today",
        "driving_distance_km",
        "driving_duration_min",
        "distance_per_active_day_km",
        "mean_speed_kmh",
        "distance_p50_km",
        "distance_p90_km",
        "duration_p50_min",
        "duration_p90_min",
        "coverage",
        "distance_bins",
        "weekday_bins",
    } == set(model)
    assert model["method"]["const"] == "usage-distribution-v1"
    assert model["validation"]["const"] == "provisional"
    assert model["status"]["enum"] == [
        "available",
        "no_records",
        "insufficient_data",
        "unsupported",
    ]
    evidence = schema["components"]["schemas"]["DriveEvidenceFilter"]["properties"]
    assert evidence["completed_only"]["const"] is True
    assert evidence["method"]["const"] == "driving-match-v1"
    frozen = json.loads((Path(__file__).parents[1] / "docs/api/openapi-0.3.0.json").read_text())
    assert (
        frozen["paths"]["/v1/vehicles/{vehicle_id}/driving-analysis"]
        == (schema["paths"]["/v1/vehicles/{vehicle_id}/driving-analysis"])
    )
    assert (
        frozen["components"]["schemas"]["DrivingAnalysisUsage"]
        == (schema["components"]["schemas"]["DrivingAnalysisUsage"])
    )


async def test_meta_declares_the_driving_analysis_feature(client):
    features = (await client.get("/v1/meta")).json()["statistics_insights"]["features"]
    assert set(features) == {
        "driving_coverage",
        "driving_analysis",
        "range_observations",
        "parking_observations",
    }
    assert features["driving_analysis"] is True


async def test_missing_usage_source_columns_fail_closed(db, database_dsn, tmp_path):
    # Every column this view reads also stays in the required startup probe, so a schema
    # without duration_min refuses to start instead of reporting no_records.
    await db.execute("ALTER TABLE drives DROP COLUMN duration_min")
    application = create_app(
        Settings(database_dsn=database_dsn, state_file=tmp_path / "state.json")
    )
    with pytest.raises(RuntimeError, match=r"drives\.duration_min"):
        async with application.router.lifespan_context(application):
            pytest.fail("a missing usage source column must refuse startup")
