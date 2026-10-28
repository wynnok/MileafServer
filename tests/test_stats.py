"""Calendar statistics through the authenticated HTTP seam."""

import asyncio
from datetime import UTC, datetime

import pytest

from mileaf.dependencies import get_now


@pytest.fixture(autouse=True)
def charging_clock(app):
    app.dependency_overrides[get_now] = lambda: datetime(2026, 10, 28, 12, tzinfo=UTC)


BASE = "/v1/vehicles/1/stats"
WINDOW = {
    "kind": "driving",
    "bucket": "month",
    "from": "2026-01-01T00:00:00Z",
    "to": "2026-04-01T00:00:00Z",
}


async def test_monthly_counts_totals_and_conditional_response(client):
    response = await client.get(BASE, params=WINDOW)
    assert response.status_code == 200
    body = response.json()
    assert [b["drives"] for b in body["buckets"]] == [31, 7, 11]
    assert body["totals"]["drives"] == 49
    assert body["totals"]["distance_km"] == 960
    assert sum(b["distance_km"] for b in body["buckets"]) == 960
    assert set(body["totals"]) == {
        "drives",
        "distance_km",
        "energy_kwh",
        "consumption_wh_km_net",
        "consumption_wh_km_gross",
        "speed_max_kmh",
        "driving_coverage",
        "net_consumption_paired",
        "comparison_quality",
    }
    # Jan: 31 * (350-325) * .15 = 116.25 kWh / 620 km.
    # Feb: 7 * 3.75 = 26.25 kWh / 140 km.
    # Mar: 10 valid deltas * 3.75 = 37.5 kWh / 200 km (11 drives).
    assert [b["energy_kwh"] for b in body["buckets"]] == [116.25, 26.25, 37.5]
    assert [b["consumption_wh_km_net"] for b in body["buckets"]] == [188, 188, 188]
    assert [b["consumption_wh_km_gross"] for b in body["buckets"]] == [32, 286, 500]
    assert body["totals"]["energy_kwh"] == 180
    assert body["totals"]["consumption_wh_km_net"] == 188
    assert body["totals"]["consumption_wh_km_gross"] == 167
    assert body["totals"]["speed_max_kmh"] == 90
    assert response.headers["cache-control"] == "private, max-age=30"
    assert (
        await client.get(BASE, params=WINDOW, headers={"If-None-Match": response.headers["etag"]})
    ).status_code == 304
    assert (await client.get(BASE, params=WINDOW, headers={"Authorization": ""})).status_code == 401
    assert (await client.get("/v1/vehicles/999/stats", params=WINDOW)).status_code == 404


async def test_empty_buckets_and_charging_counts(client):
    for kind in ("driving", "charging"):
        response = await client.get("/v1/vehicles/2/stats", params={**WINDOW, "kind": kind})
        assert response.status_code == 200
        body = response.json()
        assert len(body["buckets"]) == 3
        expected = (
            {
                "drives": 0,
                "distance_km": 0,
                "energy_kwh": 0,
                "consumption_wh_km_net": None,
                "consumption_wh_km_gross": None,
                "speed_max_kmh": None,
            }
            if kind == "driving"
            else {
                "sessions": 0,
                "energy_added_kwh": 0,
                "cost": 0,
                "cost_per_kwh": None,
                "ac_energy_kwh": 0,
                "dc_energy_kwh": 0,
                "dc_energy_share": None,
            }
        )
        assert {key: body["totals"][key] for key in expected} == expected
        for bucket in body["buckets"]:
            assert {key: bucket[key] for key in expected} == expected
    charging = (await client.get(BASE, params={**WINDOW, "kind": "charging"})).json()
    assert charging["totals"]["sessions"] == 8


@pytest.mark.parametrize(
    ("day", "drives", "distance", "energy", "net", "gross"),
    [
        ("02", 1, 0, 3.75, None, None),  # Zero km, positive range delta.
        ("03", 1, 20, None, None, 1000),  # Non-positive delta is unknown energy.
        ("04", 0, 0, 0, None, None),  # Charging energy without driving distance.
        ("05", 1, 20, 3.75, 188, None),  # Driving without charging energy.
    ],
)
async def test_driving_zero_distance_and_missing_energy(
    client, day, drives, distance, energy, net, gross
):
    response = await client.get(
        BASE,
        params={
            **WINDOW,
            "bucket": "day",
            "from": f"2026-03-{day}T00:00:00Z",
            "to": f"2026-03-{day}T23:59:59Z",
        },
    )
    assert response.status_code == 200
    expected = {
        "drives": drives,
        "distance_km": distance,
        "energy_kwh": energy,
        "consumption_wh_km_net": net,
        "consumption_wh_km_gross": gross,
        "speed_max_kmh": 90 if drives else None,
    }
    assert {key: response.json()["totals"][key] for key in expected} == expected
    assert len(response.json()["buckets"]) == 1
    bucket = response.json()["buckets"][0]
    assert {key: bucket[key] for key in expected} == expected
    assert bucket["bucket_start"] == f"2026-03-{day}T00:00:00Z"


async def test_driving_totals_merge_raw_metrics_and_keep_metric_units(client, db):
    await db.execute("UPDATE settings SET unit_of_length='mi'")
    await db.execute("UPDATE drives SET distance=40,speed_max=110 WHERE id=18")
    body = (await client.get(BASE, params=WINDOW)).json()
    assert [b["distance_km"] for b in body["buckets"]] == [640, 140, 200]
    assert [b["consumption_wh_km_net"] for b in body["buckets"]] == [182, 188, 188]
    assert [b["speed_max_kmh"] for b in body["buckets"]] == [110, 90, 90]
    # 180 / 980 * 1000 = 183.673..., not the mean of the monthly rates.
    expected = {
        "drives": 49,
        "distance_km": 980,
        "energy_kwh": 180,
        "consumption_wh_km_net": 184,
        "consumption_wh_km_gross": 163,
        "speed_max_kmh": 110,
    }
    assert {key: body["totals"][key] for key in expected} == expected


async def test_charging_metrics_exclude_unknown_cost_from_both_ratio_terms(client):
    response = await client.get(BASE, params={**WINDOW, "kind": "charging"})
    assert response.status_code == 200
    body = response.json()
    assert [b["sessions"] for b in body["buckets"]] == [1, 2, 5]
    assert [b["energy_added_kwh"] for b in body["buckets"]] == [20, 40, 100]
    assert [b["cost"] for b in body["buckets"]] == [12, 12, 48]
    assert [b["cost_per_kwh"] for b in body["buckets"]] == [0.6, 0.6, 0.48]
    assert [b["ac_energy_kwh"] for b in body["buckets"]] == [20, 20, 80]
    assert [b["dc_energy_kwh"] for b in body["buckets"]] == [0, 20, 0]
    assert [b["dc_energy_share"] for b in body["buckets"]] == [0, 0.5, 0]
    expected = {
        "sessions": 8,
        "energy_added_kwh": 160,
        "cost": 72,
        "cost_per_kwh": pytest.approx(72 / 140),
        "ac_energy_kwh": 120,
        "dc_energy_kwh": 20,
        "dc_energy_share": pytest.approx(1 / 7),
    }
    assert {key: body["totals"][key] for key in expected} == expected
    for name in ("sessions", "energy_added_kwh", "cost", "ac_energy_kwh", "dc_energy_kwh"):
        assert sum(b[name] for b in body["buckets"]) == body["totals"][name]
    # The no-sample session contributes precisely 20 kWh to the unclassified gap.
    assert body["totals"]["energy_added_kwh"] - 120 - 20 == 20
    assert body["totals"]["dc_energy_share"] != pytest.approx(1 / 6)


@pytest.mark.parametrize(
    ("start", "end", "cost", "price"),
    [
        ("2026-02-15", "2026-02-16", None, None),
        ("2026-03-01", "2026-03-02", 0, 0),
    ],
)
async def test_charging_unknown_and_zero_cost_are_distinct(client, start, end, cost, price):
    response = await client.get(
        BASE,
        params={
            **WINDOW,
            "kind": "charging",
            "bucket": "day",
            "from": start + "T00:00:00Z",
            "to": end + "T00:00:00Z",
        },
    )
    assert response.status_code == 200
    expected = {
        "sessions": 1,
        "energy_added_kwh": 20,
        "cost": cost,
        "cost_per_kwh": price,
        "ac_energy_kwh": 20,
        "dc_energy_kwh": 0,
        "dc_energy_share": 0,
    }
    assert {key: response.json()["totals"][key] for key in expected} == expected
    assert len(response.json()["buckets"]) == 1
    assert response.json()["buckets"][0]["bucket_start"] == start + "T00:00:00Z"
    assert {key: response.json()["buckets"][0][key] for key in expected} == expected


async def test_charging_zero_energy_and_unknown_classification(client, db):
    await db.execute("UPDATE charging_processes SET charge_energy_added=0 WHERE id=1")
    body = (await client.get(BASE, params={**WINDOW, "kind": "charging"})).json()
    expected = {
        "bucket_start": "2026-01-01T00:00:00Z",
        "sessions": 1,
        "energy_added_kwh": 0,
        "cost": 12,
        "cost_per_kwh": None,
        "ac_energy_kwh": 0,
        "dc_energy_kwh": 0,
        "dc_energy_share": None,
    }
    assert {key: body["buckets"][0][key] for key in expected} == expected
    unknown = await client.get(
        BASE,
        params={
            **WINDOW,
            "kind": "charging",
            "from": "2026-03-02T00:00:00Z",
            "to": "2026-03-03T00:00:00Z",
        },
    )
    expected_unknown = {
        "sessions": 1,
        "energy_added_kwh": 20,
        "cost": 12,
        "cost_per_kwh": 0.6,
        "ac_energy_kwh": 0,
        "dc_energy_kwh": 0,
        "dc_energy_share": None,
    }

    assert {key: unknown.json()["totals"][key] for key in expected_unknown} == expected_unknown


async def test_ongoing_charging_excluded_at_fixed_now(client):
    from tests.conftest import FIXED_NOW

    response = await client.get(
        BASE,
        params={
            **WINDOW,
            "kind": "charging",
            "from": "2026-03-15T11:00:00Z",
            "to": FIXED_NOW.isoformat(),
        },
    )
    assert response.status_code == 200
    expected = {
        "sessions": 0,
        "energy_added_kwh": 0,
        "cost": 0,
        "cost_per_kwh": None,
        "ac_energy_kwh": 0,
        "dc_energy_kwh": 0,
        "dc_energy_share": None,
    }
    assert {key: response.json()["totals"][key] for key in expected} == expected
    assert len(response.json()["buckets"]) == 1
    assert response.json()["buckets"][0]["bucket_start"] == "2026-03-01T00:00:00Z"
    assert {key: response.json()["buckets"][0][key] for key in expected} == expected


async def test_driving_preferred_range_missing_efficiency_and_summary_agree(client, db, app):
    # Historical March records are complete before the current-clock upper bound.
    app.dependency_overrides[get_now] = lambda: datetime(2026, 3, 31, 12, tzinfo=UTC)
    response = await client.get(BASE, params=WINDOW)
    summary = (await client.get("/v1/vehicles/1/summary")).json()
    march = response.json()["buckets"][2]
    assert {name: march[name] for name in ("drives", "distance_km", "energy_kwh")} == summary[
        "period"
    ]["month"]
    await db.execute("UPDATE settings SET preferred_range='ideal'")
    ideal = (await client.get(BASE, params=WINDOW)).json()
    # 48 * (380-325) * .15 + (380-350) * .15 = 400.5 kWh.
    assert ideal["totals"]["energy_kwh"] == 400.5
    assert ideal["totals"]["consumption_wh_km_net"] == 417
    await db.execute("UPDATE cars SET efficiency=NULL WHERE id=1")
    unknown = (await client.get(BASE, params=WINDOW)).json()
    assert unknown["totals"]["energy_kwh"] is None
    assert unknown["totals"]["consumption_wh_km_net"] is None
    assert unknown["totals"]["drives"] == 49
    assert unknown["totals"]["consumption_wh_km_gross"] == 167


async def test_driving_ratios_use_unrounded_distance(client, db):
    await db.execute("UPDATE drives SET distance=20.04 WHERE id IN (18,19)")
    response = await client.get(
        BASE,
        params={**WINDOW, "from": "2026-01-01T00:00:00Z", "to": "2026-01-03T00:00:00Z"},
    )
    assert response.status_code == 200
    # 7.5 kWh / 40.08 km * 1000 = 187.1257..., then round for display.
    assert response.json()["totals"]["distance_km"] == 40.1
    assert response.json()["totals"]["energy_kwh"] == 7.5
    assert response.json()["totals"]["consumption_wh_km_net"] == 187


@pytest.mark.parametrize(
    ("kind", "count", "expected"), [("driving", "drives", 2), ("charging", "sessions", 1)]
)
async def test_dst_and_week_calendar_boundaries(client, kind, count, expected):
    params = {
        **WINDOW,
        "kind": kind,
        "bucket": "day",
        "tz": "Europe/Berlin",
        "from": "2026-03-28T23:00:00Z",
        "to": "2026-03-29T22:00:00Z",
    }
    body = (await client.get(BASE, params=params)).json()
    assert len(body["buckets"]) == 1
    assert body["buckets"][0]["bucket_start"] == "2026-03-28T23:00:00Z"
    assert body["totals"][count] == expected
    if kind == "driving":
        assert body["totals"]["energy_kwh"] == 7.5
        assert body["totals"]["consumption_wh_km_gross"] == 500
    else:
        assert body["totals"]["energy_added_kwh"] == 20
        assert body["totals"]["cost_per_kwh"] == 0.6
    week = (await client.get(BASE, params={**params, "bucket": "week"})).json()
    assert week["buckets"][0]["bucket_start"] == "2026-03-22T23:00:00Z"


@pytest.mark.parametrize(
    "change",
    [
        dict(kind="invalid"),
        dict(bucket="hour"),
        dict(tz="Mars/Olympus"),
        dict(from_=None),
        {"from": "2024-01-01T00:00:00Z"},
        {"from": "2026-04-01T00:00:00Z"},
        {"from": "bad"},
        {"from": "2026-01-01"},
    ],
)
async def test_invalid_parameters_return_400(client, change):
    params = {**WINDOW, **change}
    if "from_" in params:
        params.pop("from_")
        params.pop("from")
    response = await client.get(BASE, params=params)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


async def test_aggregate_query_budget_and_no_server_cache(app, client, db, database_dsn):
    import asyncpg

    from mileaf.dependencies import get_pool

    queries = []

    async def record_queries(connection):
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
        for kind, table in [("driving", "drives"), ("charging", "charging_processes")]:
            await client.get(BASE, params={**WINDOW, "kind": kind})
            await asyncio.sleep(0)
            queries.clear()
            first = await client.get(BASE, params={**WINDOW, "kind": kind})
            await asyncio.sleep(0)
            assert first.status_code == 200
            assert len([query for query in queries if f"FROM {table} " in query]) == (
                2 if kind == "driving" else 1
            )
        first = await client.get(BASE, params=WINDOW)
        await db.execute("UPDATE drives SET distance=40 WHERE id=1")
        second = await client.get(BASE, params=WINDOW)
        assert second.json()["totals"]["distance_km"] == 980
        assert first.headers["etag"] != second.headers["etag"]
    finally:
        app.dependency_overrides.pop(get_pool)
        await pool.close()


async def test_ratio_helper_combines_numerators_and_denominators(app, client, db):
    from mileaf.stats import ratio_sql

    @app.get("/v1/statistics-ratio-probe")
    async def ratio_probe():
        expression = ratio_sql(numerator="numerator", denominator="denominator")
        rows = await db.fetch(
            f"SELECT bucket, {expression} AS ratio FROM "
            "(VALUES ('a',10,1),('b',10,9),('zero',0,0)) "
            "data(bucket,numerator,denominator) GROUP BY GROUPING SETS ((bucket),())"
        )
        return {
            row["bucket"] or "total": float(row["ratio"]) if row["ratio"] is not None else None
            for row in rows
        }

    response = await client.get("/v1/statistics-ratio-probe")
    assert response.status_code == 200
    assert response.json() == {"a": 10, "b": pytest.approx(10 / 9), "zero": None, "total": 2}


async def test_partial_boundaries_fall_dst_and_openapi(client):
    partial = await client.get(
        BASE,
        params={
            **WINDOW,
            "bucket": "year",
            "from": "2026-03-15T08:00:00Z",
            "to": "2026-03-15T10:30:00Z",
        },
    )
    assert partial.status_code == 200
    assert partial.json()["totals"]["drives"] == 1
    assert partial.json()["totals"]["distance_km"] == 20
    fall = await client.get(
        BASE,
        params={
            **WINDOW,
            "bucket": "day",
            "tz": "Europe/Berlin",
            "from": "2026-10-24T22:00:00Z",
            "to": "2026-10-26T23:00:00Z",
        },
    )
    assert [bucket["bucket_start"] for bucket in fall.json()["buckets"]] == [
        "2026-10-24T22:00:00Z",
        "2026-10-25T23:00:00Z",
    ]
    schema = (await client.get("/openapi.json")).json()
    parameters = schema["paths"]["/v1/vehicles/{vehicle_id}/stats"]["get"]["parameters"]
    assert {p["name"] for p in parameters if p.get("required") and p["in"] == "query"} == {
        "kind",
        "bucket",
        "from",
        "to",
    }
