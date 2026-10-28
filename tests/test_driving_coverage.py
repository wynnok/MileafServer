"""Driving quality and paired denominators through authenticated HTTP."""

from datetime import UTC, datetime

import pytest

from mileaf.app import create_app
from mileaf.config import Settings
from mileaf.dependencies import get_now

BASE = "/v1/vehicles/1/stats"
WINDOW = {
    "kind": "driving",
    "bucket": "day",
    "from": "2026-03-02T00:00:00Z",
    "to": "2026-03-06T00:00:00Z",
}


@pytest.fixture(autouse=True)
def clock(app):
    app.dependency_overrides[get_now] = lambda: datetime(2026, 10, 28, 12, tzinfo=UTC)


async def test_paired_energy_uses_only_the_same_positive_distance_drives(client):
    response = await client.get(BASE, params=WINDOW)
    assert response.status_code == 200
    body = response.json()
    # March 2: 0 km / 3.75 kWh; March 3: 20 km / unknown; March 5: 20 km / 3.75.
    assert body["totals"]["energy_kwh"] == 7.5
    assert body["totals"]["consumption_wh_km_net"] == 188
    paired = body["totals"]["net_consumption_paired"]
    assert paired["status"] == "available"
    assert paired["value_wh_km"] == 187.5
    assert paired["numerator_energy_kwh"] == 3.75
    assert paired["denominator_distance_km"] == 20
    assert paired["eligible_drives"] == 1
    assert paired["excluded_drives"] == 2
    assert paired["coverage"]["excluded_by_reason"] == {
        "missing_distance": 0,
        "invalid_distance": 0,
        "non_positive_distance": 1,
        "missing_energy": 1,
        "invalid_energy": 0,
    }
    coverage = body["totals"]["driving_coverage"]
    assert coverage["completed_drives"] == 3
    assert coverage["distance_known_drives"] == 3
    assert coverage["distance_known_km"] == 40
    assert coverage["net_energy_known_drives"] == 2
    assert coverage["net_energy_known_distance_km"] == 20
    assert coverage["duration_known_drives"] == 3
    assert coverage["duration_known_min"] == 90
    assert body["query"]["vehicle_id"] == 1
    assert body["query"]["time_basis"] == "started_at"
    assert body["query"]["filters"] == {}
    assert body["query"]["bucket"] == "day"
    assert body["range_kind"] == "rated"
    assert body["as_of"] == "2026-10-28T12:00:00Z"
    assert body["totals"]["comparison_quality"]["sample_status"] == "insufficient_data"


@pytest.mark.parametrize(
    ("known", "paired_distance", "expected"),
    [(5, 80, "adequate"), (5, 79, "insufficient_data"), (4, 80, "insufficient_data")],
)
async def test_comparison_thresholds_use_paired_count_and_distance(
    client, db, known, paired_distance, expected
):
    rows = [
        (101 + index, datetime(2026, 4, 1, 9, index), paired_distance / known, 325)
        for index in range(known)
    ]
    rows.append((120, datetime(2026, 4, 1, 10), 100 - paired_distance, 350))
    await db.executemany(
        "INSERT INTO drives(id,car_id,start_date,end_date,distance,duration_min,"
        "start_rated_range_km,end_rated_range_km) "
        "VALUES($1,1,$2::timestamp,$2::timestamp+interval '30 minutes',$3,30,350,$4)",
        rows,
    )
    response = await client.get(
        BASE, params={**WINDOW, "from": "2026-04-01T00:00:00Z", "to": "2026-04-02T00:00:00Z"}
    )
    assert response.status_code == 200
    totals = response.json()["totals"]
    assert totals["net_consumption_paired"]["denominator_distance_km"] == paired_distance
    assert totals["comparison_quality"]["sample_status"] == expected
    assert totals["comparison_quality"]["distance_coverage"] == paired_distance / 100
    assert totals["comparison_quality"]["validation"] == "provisional"


async def test_new_values_keep_precision_and_total_uses_its_own_denominator(client, db):
    await db.execute("UPDATE drives SET distance=20.04 WHERE id IN (18,19)")
    response = await client.get(
        BASE, params={**WINDOW, "from": "2026-01-01T00:00:00Z", "to": "2026-01-03T00:00:00Z"}
    )
    totals = response.json()["totals"]
    assert totals["distance_km"] == 40.1
    assert totals["net_consumption_paired"]["denominator_distance_km"] == 40.08
    assert totals["net_consumption_paired"]["value_wh_km"] == pytest.approx(187.125748502994)


async def test_missing_negative_and_zero_values_have_separate_coverage(client, db):
    await db.execute(
        "UPDATE drives SET distance=NULL,duration_min=NULL WHERE id=9;"
        "UPDATE drives SET distance=-10,duration_min=-5 WHERE id=8;"
        "UPDATE drives SET duration_min=0 WHERE id=4"
    )
    response = await client.get(BASE, params=WINDOW)
    coverage = response.json()["totals"]["driving_coverage"]
    assert coverage["distance_known_km"] == 10  # Keep known negative facts separate from validity.
    assert coverage["distance"]["candidate_count"] == 3
    assert coverage["distance"]["valid_count"] == 1
    assert coverage["distance"]["valid_distance_km"] == 20
    assert coverage["distance"]["excluded_by_reason"] == {
        "missing_distance": 1,
        "invalid_distance": 1,
    }
    # Candidates keep the known negative facts; only the valid set drops them.
    assert coverage["distance"]["candidate_distance_km"] == 10
    assert coverage["distance"]["candidate_duration_min"] == -5
    assert coverage["distance"]["unknown_distance_count"] == 1
    assert coverage["distance"]["unknown_duration_count"] == 1
    assert coverage["net_energy"]["valid_count"] == 2
    assert coverage["net_energy"]["valid_distance_km"] == 20
    assert coverage["net_energy"]["excluded_by_reason"] == {
        "missing_energy": 1,
        "invalid_energy": 0,
    }
    assert coverage["duration"]["valid_count"] == 1  # Zero duration remains a known valid record.
    assert coverage["duration"]["valid_duration_min"] == 0
    assert coverage["duration"]["excluded_by_reason"] == {
        "missing_duration": 1,
        "invalid_duration": 1,
    }


async def test_no_records_all_missing_and_ongoing_are_distinguishable(client, db):
    empty = await client.get(BASE.replace("/1/", "/2/"), params=WINDOW)
    assert empty.json()["totals"]["net_consumption_paired"]["status"] == "no_records"
    assert empty.json()["totals"]["net_consumption_paired"]["numerator_energy_kwh"] == 0
    await db.execute("UPDATE cars SET efficiency=NULL WHERE id=1")
    missing = (await client.get(BASE, params=WINDOW)).json()["totals"]
    assert missing["net_consumption_paired"]["status"] == "insufficient_data"
    assert missing["net_consumption_paired"]["numerator_energy_kwh"] is None
    ongoing = await client.get(
        BASE,
        params={**WINDOW, "from": "2026-03-15T11:00:00Z", "to": "2026-03-15T12:00:00Z"},
    )
    assert ongoing.json()["totals"]["drives"] == 0
    assert ongoing.json()["totals"]["driving_coverage"]["ongoing_drives"] == 1
    assert ongoing.json()["totals"]["net_consumption_paired"]["value_wh_km"] is None


async def test_query_clips_future_and_etag_ignores_generation_time(client, app):
    params = {**WINDOW, "from": "2026-03-01T00:00:00Z", "to": "2026-03-02T00:00:00Z"}
    first = await client.get(BASE, params=params)
    app.dependency_overrides[get_now] = lambda: datetime(2026, 10, 28, 13, tzinfo=UTC)
    second = await client.get(BASE, params=params, headers={"If-None-Match": first.headers["etag"]})
    assert second.status_code == 304
    future = await client.get(
        BASE, params={**WINDOW, "from": "2026-10-27T00:00:00Z", "to": "2026-11-01T00:00:00Z"}
    )
    assert future.json()["query"]["to"] == "2026-10-28T13:00:00Z"
    assert len(future.json()["buckets"]) == 2
    entirely_future = await client.get(
        BASE, params={**WINDOW, "from": "2026-10-29T00:00:00Z", "to": "2026-11-01T00:00:00Z"}
    )
    assert entirely_future.status_code == 400


async def test_ideal_range_kind_changes_the_paired_numerator(client, db):
    await db.execute("UPDATE settings SET preferred_range='ideal'")
    response = await client.get(
        BASE, params={**WINDOW, "from": "2026-01-01T00:00:00Z", "to": "2026-01-03T00:00:00Z"}
    )
    body = response.json()
    assert body["range_kind"] == "ideal"
    # Ideal start range is 380 for all fixture drives: the delta is 55 km.
    assert body["totals"]["energy_kwh"] == 16.5
    assert body["totals"]["net_consumption_paired"]["numerator_energy_kwh"] == 16.5
    assert body["totals"]["net_consumption_paired"]["denominator_distance_km"] == 40
    assert body["totals"]["net_consumption_paired"]["value_wh_km"] == 412.5


async def test_every_bucket_and_empty_dst_buckets_report_their_own_coverage(client):
    # Berlin spring forward: the local day 2026-03-29 starts at 2026-03-28T23:00Z and
    # holds drives 15 and 16 (20 km / 30 min / 3.75 kWh each).
    spring = {
        **WINDOW,
        "tz": "Europe/Berlin",
        "from": "2026-03-28T23:00:00Z",
        "to": "2026-03-29T22:00:00Z",
    }
    body = (await client.get(BASE, params=spring)).json()
    assert [bucket["bucket_start"] for bucket in body["buckets"]] == ["2026-03-28T23:00:00Z"]
    bucket = body["buckets"][0]
    assert bucket["driving_coverage"]["completed_drives"] == 2
    assert bucket["driving_coverage"]["duration"]["valid_duration_min"] == 60
    assert bucket["net_consumption_paired"]["value_wh_km"] == 187.5
    assert bucket["net_consumption_paired"]["eligible_drives"] == 2
    assert bucket["comparison_quality"]["sample_status"] == "insufficient_data"

    # Berlin fall back: two local days without a single candidate drive.
    fall = {
        **WINDOW,
        "tz": "Europe/Berlin",
        "from": "2026-10-24T22:00:00Z",
        "to": "2026-10-26T23:00:00Z",
    }
    empty = (await client.get(BASE, params=fall)).json()
    assert [bucket["bucket_start"] for bucket in empty["buckets"]] == [
        "2026-10-24T22:00:00Z",
        "2026-10-25T23:00:00Z",
    ]
    for period in [*empty["buckets"], empty["totals"]]:
        coverage = period["driving_coverage"]
        assert coverage["status"] == "no_records"
        assert coverage["completed_drives"] == 0
        assert coverage["distance_known_km"] == 0  # Additive quantities stay 0, not null.
        assert coverage["distance"]["excluded_by_reason"] == {
            "missing_distance": 0,
            "invalid_distance": 0,
        }
        assert period["net_consumption_paired"]["status"] == "no_records"
        assert period["net_consumption_paired"]["numerator_energy_kwh"] == 0
        assert period["net_consumption_paired"]["value_wh_km"] is None
        assert period["comparison_quality"]["distance_coverage"] is None
        assert period["comparison_quality"]["sample_status"] == "insufficient_data"


async def test_totals_paired_value_is_not_the_mean_of_its_buckets(client, db):
    await db.execute("UPDATE drives SET distance=10 WHERE id=18")
    response = await client.get(
        BASE, params={**WINDOW, "from": "2026-01-01T00:00:00Z", "to": "2026-01-03T00:00:00Z"}
    )
    body = response.json()
    # Jan 1: 3.75 kWh / 10 km; Jan 2: 3.75 kWh / 20 km; window: 7.5 kWh / 30 km.
    assert [b["net_consumption_paired"]["value_wh_km"] for b in body["buckets"]] == [375, 187.5]
    assert body["totals"]["net_consumption_paired"]["value_wh_km"] == 250
    assert body["totals"]["net_consumption_paired"]["denominator_distance_km"] == 30
    assert body["totals"]["distance_km"] == 30
    assert body["totals"]["consumption_wh_km_net"] == 250
    assert body["totals"]["driving_coverage"]["distance"]["valid_distance_km"] == 30


async def test_completed_drives_are_assigned_whole_by_started_at(client):
    # Drive 1 (08:00-10:00Z) starts inside the window and ends after it: kept whole.
    inside = await client.get(
        BASE,
        params={**WINDOW, "from": "2026-03-15T07:00:00Z", "to": "2026-03-15T09:00:00Z"},
    )
    totals = inside.json()["totals"]
    assert totals["drives"] == 1
    assert totals["driving_coverage"]["completed_drives"] == 1
    assert totals["driving_coverage"]["duration_known_min"] == 120
    assert totals["net_consumption_paired"]["denominator_distance_km"] == 20
    # Drive 1 started before this window: it is not split, only drive 2 (10:30) counts.
    after = await client.get(
        BASE,
        params={**WINDOW, "from": "2026-03-15T09:00:00Z", "to": "2026-03-15T11:00:00Z"},
    )
    assert after.json()["totals"]["driving_coverage"]["completed_drives"] == 1
    assert after.json()["totals"]["driving_coverage"]["duration_known_min"] == 2
    # Drive 6 spans local midnight (23:30-00:40Z) and belongs to its start period only.
    midnight = await client.get(
        BASE,
        params={**WINDOW, "from": "2026-03-15T00:00:00Z", "to": "2026-03-15T01:00:00Z"},
    )
    assert midnight.json()["totals"]["driving_coverage"]["status"] == "no_records"
    assert midnight.json()["totals"]["driving_coverage"]["ongoing_drives"] == 0


async def test_zero_denominator_and_unknown_distance_never_reach_adequate(client, db):
    rows = [(201 + index, 0.0) for index in range(5)] + [(206, None), (207, -5.0)]
    await db.executemany(
        "INSERT INTO drives(id,car_id,start_date,end_date,distance,duration_min,"
        "start_rated_range_km,end_rated_range_km) "
        "VALUES($1,1,'2026-05-01 09:00','2026-05-01 09:30',$2,30,350,325)",
        rows,
    )
    totals = (
        await client.get(
            BASE, params={**WINDOW, "from": "2026-05-01T00:00:00Z", "to": "2026-05-02T00:00:00Z"}
        )
    ).json()["totals"]
    coverage = totals["driving_coverage"]
    assert coverage["status"] == "available"
    assert coverage["completed_drives"] == 7
    assert coverage["distance"]["valid_count"] == 5
    assert coverage["distance"]["excluded_count"] == 2
    assert coverage["distance"]["excluded_by_reason"] == {
        "missing_distance": 1,
        "invalid_distance": 1,
    }
    # Zero kilometres are valid known distance, they are just not a usable denominator.
    assert coverage["distance"]["valid_distance_km"] == 0
    paired = totals["net_consumption_paired"]
    assert paired["status"] == "insufficient_data"
    assert paired["eligible_drives"] == 0
    assert paired["excluded_drives"] == 7
    assert paired["numerator_energy_kwh"] is None
    # Candidates exist but none is eligible: the additive paired terms stay null, not 0.
    assert paired["denominator_distance_km"] is None
    assert paired["coverage"]["valid_distance_km"] is None
    assert paired["coverage"]["valid_count"] == 0
    assert paired["value_wh_km"] is None
    assert paired["coverage"]["excluded_by_reason"] == {
        "missing_distance": 1,
        "invalid_distance": 1,
        "non_positive_distance": 5,
        "missing_energy": 0,
        "invalid_energy": 0,
    }
    # Every family stays additive: candidate = valid + excluded = valid + sum(reasons).
    for family in (coverage["distance"], coverage["net_energy"], coverage["duration"]):
        assert family["candidate_count"] == family["valid_count"] + family["excluded_count"]
        assert family["excluded_count"] == sum(family["excluded_by_reason"].values())
    quality = totals["comparison_quality"]
    assert quality["status"] == "available"
    assert quality["distance_coverage"] is None
    assert quality["sample_status"] == "insufficient_data"
    assert quality["eligible_drives"] == 0
    assert quality["unknown_distance_drives"] == 1
    assert quality["invalid_distance_drives"] == 1


async def test_day_buckets_stay_within_the_declared_cap(client):
    response = await client.get(
        BASE,
        params={**WINDOW, "from": "2024-03-16T00:00:00Z", "to": "2026-03-15T00:00:00Z"},
    )
    assert response.status_code == 200
    # 729 local days; the half-open upper bound does not generate an extra bucket.
    assert len(response.json()["buckets"]) == 729 <= 732
    assert response.json()["buckets"][0]["driving_coverage"]["status"] == "no_records"


async def test_missing_duration_column_fails_closed_with_the_legacy_probe(
    db, database_dsn, tmp_path
):
    # driving_coverage reads no column outside the required startup probe, so a schema
    # without duration_min refuses to start instead of silently reporting no_records.
    await db.execute("ALTER TABLE drives DROP COLUMN duration_min")
    application = create_app(
        Settings(database_dsn=database_dsn, state_file=tmp_path / "state.json")
    )
    with pytest.raises(RuntimeError, match=r"drives\.duration_min"):
        async with application.router.lifespan_context(application):
            pytest.fail("a missing required column must refuse startup")
