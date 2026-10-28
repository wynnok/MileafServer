"""Public ST03 usage contracts against the isolated TeslaMate fixture."""

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
        # conditions is owned by #38; the declared view enum stays accurate until then.
        {"view": "conditions", **base},
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
