"""Rated range observations through HTTP and isolated PostgreSQL."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import perf_counter

import httpx
import pytest

from mileaf.dependencies import get_now

URL = "/v1/vehicles/1/range-observations"
THRESHOLDS = {"soc_min": 20, "soc_max": 90, "minimum_valid_days": 3, "validation": "provisional"}


@pytest.fixture(autouse=True)
def observation_clock(app):
    app.dependency_overrides[get_now] = lambda: datetime(2026, 4, 15, 12, tzinfo=UTC)


async def observation_session(db, identifier, start, samples, *, end=None, car_id=1):
    """Insert a session; samples are (soc, rated) pairs or dicts with explicit offsets."""
    await db.execute(
        "INSERT INTO charging_processes(id,car_id,position_id,start_date,end_date) "
        "VALUES($1,$2,999999,$3,$4)",
        identifier,
        car_id,
        start,
        start + timedelta(hours=1) if end is None else end,
    )
    rows = []
    for index, sample in enumerate(samples):
        point = {"at": index, "ideal": 300} | (
            dict(zip(("soc", "rated"), sample, strict=True))
            if isinstance(sample, tuple)
            else sample
        )
        rows.append(
            (
                identifier,
                start + timedelta(seconds=point["at"]),
                point.get("soc"),
                point.get("usable"),
                point.get("rated"),
                point.get("ideal"),
            )
        )
    await db.executemany(
        "INSERT INTO charges(charging_process_id,date,battery_level,usable_battery_level,"
        "rated_battery_range_km,ideal_battery_range_km,charge_energy_added,charger_power) "
        "VALUES($1,$2,$3,$4,$5,$6,0,0)",
        rows,
    )


async def observed_history(db):
    """Drop seeded charging history so each test owns every candidate session."""
    await db.execute("DELETE FROM charges; DELETE FROM charging_processes")


async def get_body(client, **params):
    response = await client.get(URL, params={"months": 6, **params})
    assert response.status_code == 200, response.text
    return response.json()


def bucket(body, start):
    return next(row for row in body["buckets"] if row["bucket_start"] == start)


# --- window, echo and bucket shape -------------------------------------------------


async def test_empty_window_has_six_month_buckets_and_calendar_echo(client):
    body = await get_body(client, to="2025-12-01T00:00:00Z")
    assert body["query"] == {
        "vehicle_id": 1,
        "from": "2025-06-01T00:00:00Z",
        "to": "2025-12-01T00:00:00Z",
        "tz": "UTC",
        "bucket": "month",
        "filters": {"months": 6},
        "time_basis": "ended_at",
    }
    assert body["as_of"] == "2026-04-15T12:00:00Z"
    assert [row["bucket_start"] for row in body["buckets"]] == [
        f"2025-{month:02d}-01T00:00:00Z" for month in range(6, 12)
    ]
    assert all(row["partial"] is False for row in body["buckets"])
    assert body["totals"]["status"] == "no_records"
    assert body["totals"]["sample_status"] == "insufficient_data"
    assert body["totals"]["coverage"] == {
        "candidate_points": 0,
        "valid_points": 0,
        "excluded_points": 0,
        "candidate_sessions": 0,
        "valid_sessions": 0,
        "valid_days": 0,
        "excluded_by_reason": {},
    }
    assert body["totals"]["thresholds"] == THRESHOLDS
    assert body["totals"]["method"] == "session-day-rated-range-v1"
    assert body["totals"]["version"] == 1
    assert body["first_observed_at"] is None
    assert body["first_observed_scope"] == "query"
    assert body["range_kind"] == "rated"
    assert body["soc_source"] == "battery_level"
    assert body["ongoing_sessions"] == 0


async def test_twelve_month_window_defaults_to_as_of_and_marks_the_partial_month(client):
    body = await get_body(client, months=12)
    assert body["query"]["from"] == "2025-05-01T00:00:00Z"
    assert body["query"]["to"] == "2026-04-15T12:00:00Z"
    assert body["query"]["filters"] == {"months": 12}
    assert len(body["buckets"]) == 12
    assert [row["partial"] for row in body["buckets"]] == [False] * 11 + [True]
    assert body["buckets"][-1]["bucket_start"] == "2026-04-01T00:00:00Z"


async def test_future_cutoff_is_clipped_to_as_of(client):
    body = await get_body(client, to="2027-06-01T00:00:00Z")
    assert body["query"]["to"] == "2026-04-15T12:00:00Z"
    assert body["query"]["from"] == "2025-11-01T00:00:00Z"
    assert body["buckets"][-1]["partial"] is True


async def test_month_start_cutoff_keeps_the_previous_month_whole(client):
    body = await get_body(client, to="2026-04-01T00:00:00Z")
    assert body["query"]["from"] == "2025-10-01T00:00:00Z"
    assert body["buckets"][-1]["bucket_start"] == "2026-03-01T00:00:00Z"
    assert body["buckets"][-1]["partial"] is False


async def test_local_timezone_defines_month_boundaries_and_attribution(client, db):
    await observed_history(db)
    # 2026-03-31T20:00Z is 2026-04-01T04:00 in Shanghai, so it belongs to April.
    await observation_session(
        db, 1, datetime(2026, 3, 31, 19), [(50, 200)], end=datetime(2026, 3, 31, 20)
    )
    body = await get_body(client, to="2026-04-01T00:00:00Z", tz="Asia/Shanghai")
    assert body["query"]["from"] == "2025-10-31T16:00:00Z"
    assert body["query"]["to"] == "2026-04-01T00:00:00Z"
    assert [row["bucket_start"] for row in body["buckets"]] == [
        "2025-10-31T16:00:00Z",
        "2025-11-30T16:00:00Z",
        "2025-12-31T16:00:00Z",
        "2026-01-31T16:00:00Z",
        "2026-02-28T16:00:00Z",
        "2026-03-31T16:00:00Z",
    ]
    assert [row["partial"] for row in body["buckets"]] == [False] * 5 + [True]
    assert bucket(body, "2026-03-31T16:00:00Z")["coverage"]["candidate_sessions"] == 1
    assert bucket(body, "2026-02-28T16:00:00Z")["status"] == "no_records"


async def test_dst_timezone_buckets_use_the_local_calendar(client):
    body = await get_body(client, to="2026-04-01T00:00:00Z", tz="America/New_York")
    assert body["query"]["from"] == "2025-10-01T04:00:00Z"
    assert [row["bucket_start"] for row in body["buckets"]] == [
        "2025-10-01T04:00:00Z",
        "2025-11-01T04:00:00Z",
        "2025-12-01T05:00:00Z",
        "2026-01-01T05:00:00Z",
        "2026-02-01T05:00:00Z",
        "2026-03-01T05:00:00Z",
    ]
    assert body["buckets"][-1]["partial"] is True


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"months": 3},
        {"months": "6,12"},
        {"months": 6, "to": "2026-04-01T00:00:00"},
        {"months": 6, "to": "not-a-timestamp"},
        {"months": 6, "tz": "Mars/Olympus"},
    ],
)
async def test_invalid_window_parameters_are_rejected(client, params):
    response = await client.get(URL, params=params)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.headers["x-request-id"]


async def test_unknown_vehicle_is_not_found(client):
    response = await client.get("/v1/vehicles/999/range-observations", params={"months": 6})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_conditional_requests_revalidate_without_as_of(client):
    first = await client.get(URL, params={"months": 6})
    assert first.status_code == 200
    assert first.headers["cache-control"] == "private, max-age=30"
    assert first.headers["etag"].startswith('W/"')
    matched = await client.get(
        URL, params={"months": 6}, headers={"If-None-Match": first.headers["etag"]}
    )
    assert matched.status_code == 304
    different = await client.get(
        URL, params={"months": 12}, headers={"If-None-Match": first.headers["etag"]}
    )
    assert different.status_code == 200


# --- three-level equal weighting ---------------------------------------------------


async def test_session_day_month_weighting_uses_all_days_not_sampling_frequency(client, db):
    await observed_history(db)
    await observation_session(db, 1, datetime(2026, 2, 5, 8), [(50, 200)] * 20)
    await observation_session(db, 2, datetime(2026, 2, 5, 10), [(50, 300)])
    await observation_session(db, 3, datetime(2026, 2, 6, 8), [(50, 350)])
    await observation_session(db, 4, datetime(2026, 2, 7, 8), [(50, 450)])
    body = await get_body(client, to="2026-04-01T00:00:00Z")
    february = bucket(body, "2026-02-01T00:00:00Z")
    # Per session: 400 (twenty samples), 600, 700, 900. Day 02-05: median(400,600)=500.
    # Month over day values 500/700/900: median 700, P10 540, P90 860.
    assert february["status"] == "available"
    assert february["sample_status"] == "adequate"
    assert february["median_range_km"] == 700
    assert february["p10_range_km"] == 540
    assert february["p90_range_km"] == 860
    assert february["coverage"] == {
        "candidate_points": 23,
        "valid_points": 23,
        "excluded_points": 0,
        "candidate_sessions": 4,
        "valid_sessions": 4,
        "valid_days": 3,
        "excluded_by_reason": {},
    }
    assert body["totals"]["median_range_km"] == 700
    assert body["totals"]["coverage"]["valid_days"] == 3
    assert body["first_observed_at"] == "2026-02-05T08:00:00Z"
    assert body["first_observed_scope"] == "query"


async def test_totals_use_every_day_value_instead_of_averaging_months(client, db):
    await observed_history(db)
    await observation_session(db, 1, datetime(2026, 2, 5, 8), [(50, 50)])
    await observation_session(db, 2, datetime(2026, 2, 6, 8), [(50, 50)])
    await observation_session(db, 3, datetime(2026, 3, 5, 8), [(50, 450)])
    body = await get_body(client, to="2026-04-01T00:00:00Z")
    assert bucket(body, "2026-02-01T00:00:00Z")["median_range_km"] == 100
    assert bucket(body, "2026-03-01T00:00:00Z")["median_range_km"] == 900
    # Day values are 100, 100 and 900: median 100 (not 500), P90 100+0.8*800.
    assert body["totals"]["median_range_km"] == 100
    assert body["totals"]["p10_range_km"] == 100
    assert body["totals"]["p90_range_km"] == 740
    assert body["totals"]["coverage"]["valid_days"] == 3
    assert body["totals"]["sample_status"] == "adequate"


async def test_point_pairing_uses_battery_level_and_rated_range_only(client, db):
    await observed_history(db)
    await observation_session(
        db,
        1,
        datetime(2026, 2, 5, 8),
        [{"soc": 50, "usable": 48, "rated": 200, "ideal": 999}],
    )
    body = await get_body(client, to="2026-04-01T00:00:00Z")
    # 200*100/50; usable SOC (2081.25) and ideal range (1998) must not leak in.
    assert body["totals"]["median_range_km"] == 400
    assert body["range_kind"] == "rated"
    assert body["soc_source"] == "battery_level"


async def test_cross_month_session_is_attributed_whole_to_its_local_end_month(client, db):
    await observed_history(db)
    start = datetime(2026, 1, 31, 23)
    await observation_session(
        db,
        1,
        start,
        [(50, 200), (50, 200), (50, 200)],
        end=datetime(2026, 2, 1, 1),
    )
    body = await get_body(client, to="2026-03-01T00:00:00Z")
    assert body["query"]["from"] == "2025-09-01T00:00:00Z"
    january = bucket(body, "2026-01-01T00:00:00Z")
    february = bucket(body, "2026-02-01T00:00:00Z")
    assert january["status"] == "no_records"
    assert january["coverage"]["candidate_sessions"] == 0
    assert january["coverage"]["candidate_points"] == 0
    assert february["coverage"]["candidate_sessions"] == 1
    assert february["coverage"]["candidate_points"] == 3
    assert february["coverage"]["valid_days"] == 1
    assert february["median_range_km"] == 400
    assert body["first_observed_at"] == "2026-01-31T23:00:00Z"


async def test_first_observed_at_stays_inside_the_query_window(client, db):
    await observed_history(db)
    # The session ends inside the window, so all three samples count; the September
    # sample still predates the window and must not become first_observed_at.
    await observation_session(
        db,
        1,
        datetime(2025, 9, 30, 23),
        [{"at": 0, "soc": 50, "rated": 200}, {"at": 3600, "soc": 50, "rated": 200}],
        end=datetime(2025, 10, 1, 1),
    )
    body = await get_body(client, to="2026-04-01T00:00:00Z")
    assert body["query"]["from"] == "2025-10-01T00:00:00Z"
    october = bucket(body, "2025-10-01T00:00:00Z")
    assert october["coverage"]["candidate_points"] == 2
    assert october["coverage"]["valid_days"] == 1
    assert body["first_observed_at"] == "2025-10-01T00:00:00Z"
    assert body["first_observed_scope"] == "query"


async def test_samples_outside_the_session_span_are_not_candidates(client, db):
    await observed_history(db)
    start = datetime(2026, 2, 5, 8)
    await observation_session(
        db,
        1,
        start,
        [
            {"at": -60, "soc": 50, "rated": 999},
            {"at": 0, "soc": 50, "rated": 200},
            {"at": 3600, "soc": 50, "rated": 200},
            {"at": 3660, "soc": 50, "rated": 999},
        ],
        end=start + timedelta(hours=1),
    )
    body = await get_body(client, to="2026-04-01T00:00:00Z")
    coverage = body["totals"]["coverage"]
    assert coverage["candidate_points"] == 2
    assert coverage["valid_points"] == 2
    assert coverage["excluded_points"] == 0
    assert body["totals"]["median_range_km"] == 400


async def test_other_vehicle_sessions_never_enter_the_observation(client, db):
    await observed_history(db)
    await observation_session(db, 1, datetime(2026, 2, 5, 8), [(50, 200)])
    await observation_session(db, 2, datetime(2026, 2, 6, 8), [(50, 900)], car_id=2)
    body = await get_body(client, to="2026-04-01T00:00:00Z")
    assert body["totals"]["coverage"]["candidate_sessions"] == 1
    assert body["totals"]["median_range_km"] == 400


async def test_ongoing_sessions_are_counted_separately_and_never_observed(client, db):
    await observed_history(db)
    for identifier, day in enumerate((5, 6, 7), start=1):
        await observation_session(db, identifier, datetime(2026, 2, day, 8), [(50, 200)])
    await db.execute(
        "INSERT INTO charging_processes(id,car_id,position_id,start_date,end_date) "
        "VALUES(9,1,999999,'2026-02-10 08:00',NULL)"
    )
    await db.execute(
        "INSERT INTO charges(charging_process_id,date,battery_level,rated_battery_range_km,"
        "charge_energy_added,charger_power,ideal_battery_range_km) "
        "VALUES(9,'2026-02-10 08:00',50,9999,0,0,300)"
    )
    body = await get_body(client, to="2026-04-01T00:00:00Z")
    assert body["ongoing_sessions"] == 1
    assert body["totals"]["median_range_km"] == 400
    assert body["totals"]["coverage"] == {
        "candidate_points": 3,
        "valid_points": 3,
        "excluded_points": 0,
        "candidate_sessions": 3,
        "valid_sessions": 3,
        "valid_days": 3,
        "excluded_by_reason": {},
    }


# --- validity, exclusion priority and sample status --------------------------------


async def test_exclusion_reasons_follow_one_fixed_priority(client, db):
    await observed_history(db)
    await observation_session(
        db,
        1,
        datetime(2026, 2, 5, 8),
        [
            {"soc": None, "rated": None},
            {"soc": 0, "rated": 200},
            {"soc": 20, "rated": 200},
            {"soc": 90, "rated": 450},
            {"soc": 91, "rated": 200},
            {"soc": 50, "rated": None},
            {"soc": 50, "rated": -200},
            {"soc": 50, "rated": 0},
            {"soc": 50, "rated": Decimal("NaN")},
            {"soc": 95, "rated": None},
        ],
    )
    body = await get_body(client, to="2026-04-01T00:00:00Z")
    # Valid values: 200*100/20=1000 and 450*100/90=500; the session median is 750.
    february = bucket(body, "2026-02-01T00:00:00Z")
    assert february["status"] == "available"
    assert february["sample_status"] == "sparse"
    assert february["median_range_km"] == 750
    assert february["p10_range_km"] == 750
    assert february["p90_range_km"] == 750
    assert february["coverage"] == {
        "candidate_points": 10,
        "valid_points": 2,
        "excluded_points": 8,
        "candidate_sessions": 1,
        "valid_sessions": 1,
        "valid_days": 1,
        "excluded_by_reason": {
            "missing_soc": 1,
            "soc_out_of_range": 3,
            "invalid_rated_range": 4,
        },
    }


async def test_candidates_without_any_valid_point_are_insufficient_data(client, db):
    await observed_history(db)
    await observation_session(db, 1, datetime(2026, 2, 5, 8), [(95, 200), (19, 200)])
    body = await get_body(client, to="2026-04-01T00:00:00Z")
    february = bucket(body, "2026-02-01T00:00:00Z")
    assert february["status"] == "insufficient_data"
    assert february["sample_status"] == "insufficient_data"
    assert february["median_range_km"] is None
    assert february["coverage"]["candidate_points"] == 2
    assert february["coverage"]["valid_points"] == 0
    assert february["coverage"]["valid_days"] == 0
    assert february["coverage"]["excluded_by_reason"] == {"soc_out_of_range": 2}
    assert body["totals"]["status"] == "insufficient_data"
    assert body["totals"]["coverage"]["excluded_points"] == 2
    assert body["totals"]["coverage"]["excluded_by_reason"] == {"soc_out_of_range": 2}
    assert body["first_observed_at"] is None


async def test_session_without_samples_is_a_candidate_but_not_valid(client, db):
    await observed_history(db)
    await observation_session(db, 1, datetime(2026, 2, 5, 8), [])
    body = await get_body(client, to="2026-04-01T00:00:00Z")
    february = bucket(body, "2026-02-01T00:00:00Z")
    assert february["status"] == "insufficient_data"
    assert february["coverage"]["candidate_sessions"] == 1
    assert february["coverage"]["candidate_points"] == 0
    assert february["coverage"]["valid_sessions"] == 0
    assert february["coverage"]["excluded_by_reason"] == {}


async def test_empty_database_is_no_records_and_not_zero(client, db):
    await observed_history(db)
    body = await get_body(client, to="2026-04-01T00:00:00Z")
    assert body["totals"]["status"] == "no_records"
    assert body["totals"]["median_range_km"] is None
    assert all(row["status"] == "no_records" for row in body["buckets"])
    assert body["ongoing_sessions"] == 0


@pytest.mark.parametrize(("days", "sample_status"), [(2, "sparse"), (3, "adequate")])
async def test_month_sample_status_depends_on_valid_days(client, db, days, sample_status):
    await observed_history(db)
    for identifier, day in enumerate(range(5, 5 + days), start=1):
        await observation_session(db, identifier, datetime(2026, 2, day, 8), [(50, 200)])
    body = await get_body(client, to="2026-04-01T00:00:00Z")
    february = bucket(body, "2026-02-01T00:00:00Z")
    assert february["coverage"]["valid_days"] == days
    assert february["status"] == "available"
    assert february["sample_status"] == sample_status


async def test_missing_months_stay_explicitly_empty(client, db):
    await observed_history(db)
    await observation_session(db, 1, datetime(2025, 12, 5, 8), [(50, 200)])
    await observation_session(db, 2, datetime(2026, 2, 6, 8), [(50, 300)])
    body = await get_body(client, to="2026-04-01T00:00:00Z")
    assert [row["status"] for row in body["buckets"]] == [
        "no_records",
        "no_records",
        "available",
        "no_records",
        "available",
        "no_records",
    ]
    empty = bucket(body, "2026-01-01T00:00:00Z")
    assert empty["median_range_km"] is None
    assert empty["p10_range_km"] is None
    assert empty["p90_range_km"] is None
    assert empty["coverage"]["candidate_sessions"] == 0
    # Two day values (400, 600) across the window: median 500, P10 420, P90 580.
    assert body["totals"]["median_range_km"] == 500
    assert body["totals"]["p10_range_km"] == 420
    assert body["totals"]["p90_range_km"] == 580
    assert body["totals"]["coverage"]["valid_days"] == 2
    assert body["totals"]["sample_status"] == "sparse"


async def test_response_reports_observations_without_raw_samples_or_health_claims(client, db):
    await observed_history(db)
    await observation_session(db, 1, datetime(2026, 2, 5, 8), [(50, 200)] * 5)
    response = await client.get(URL, params={"months": 12})
    body = response.json()
    assert set(body) == {
        "query",
        "as_of",
        "range_kind",
        "soc_source",
        "first_observed_at",
        "first_observed_scope",
        "ongoing_sessions",
        "method",
        "version",
        "thresholds",
        "buckets",
        "totals",
    }
    assert set(body["buckets"][0]) == {
        "bucket_start",
        "partial",
        "status",
        "sample_status",
        "median_range_km",
        "p10_range_km",
        "p90_range_km",
        "coverage",
        "method",
        "version",
        "thresholds",
    }
    assert body["thresholds"] == THRESHOLDS
    for claim in ("soh", "capacity", "degradation", "baseline", "cycle", "forecast"):
        assert claim not in response.text.lower()


# --- capability declaration --------------------------------------------------------


async def test_meta_declares_the_range_observations_capability(client):
    meta = (await client.get("/v1/meta")).json()
    assert meta["statistics_insights"]["features"]["range_observations"] is True


@pytest.mark.parametrize(
    "mutation",
    [
        "ALTER TABLE charges DROP COLUMN rated_battery_range_km",
        "ALTER TABLE charges ALTER COLUMN rated_battery_range_km TYPE text USING NULL",
    ],
)
async def test_missing_source_columns_report_unsupported_not_empty_data(db, app, mutation):
    await db.execute(mutation)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as anonymous,
    ):
        await anonymous.post("/v1/auth/setup", json={"password": "fixture password"})
        login = (
            await anonymous.post("/v1/auth/login", json={"password": "fixture password"})
        ).json()
        anonymous.headers["Authorization"] = "Bearer " + login["token"]
        meta = (await anonymous.get("/v1/meta")).json()
        assert meta["statistics_insights"]["features"]["range_observations"] is False
        response = await anonymous.get(URL, params={"months": 6})
        assert response.status_code == 200
        body = response.json()
        assert body["totals"]["status"] == "unsupported"
        assert all(row["status"] == "unsupported" for row in body["buckets"])
        assert body["first_observed_at"] is None
        assert body["buckets"][-1]["partial"] is True
        # Published 0.1/0.2 surfaces keep working when an optional statistics column is gone.
        legacy = await anonymous.get(
            "/v1/vehicles/1/stats",
            params={
                "from": "2026-01-01T00:00:00Z",
                "to": "2026-04-01T00:00:00Z",
                "kind": "charging",
                "bucket": "month",
            },
        )
        assert legacy.status_code == 200


@pytest.mark.slow
async def test_range_observations_stay_bounded_on_large_fixture(client, app, db, performance_seed):
    app.state.cache.ttl = 0
    await db.execute(
        "UPDATE charges SET rated_battery_range_km=300 WHERE charging_process_id>=100000"
    )
    start = perf_counter()
    response = await client.get(
        f"/v1/vehicles/{performance_seed}/range-observations", params={"months": 12}
    )
    elapsed = perf_counter() - start
    assert response.status_code == 200, response.text
    assert elapsed < 5
    body = response.json()
    coverage = body["totals"]["coverage"]
    assert len(body["buckets"]) == 12
    assert coverage["candidate_points"] > 50_000
    assert coverage["valid_points"] == coverage["candidate_points"]
    assert coverage["candidate_sessions"] > 2_000
    assert "items" not in body and "samples" not in body and "points" not in body
