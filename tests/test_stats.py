"""Statistics skeleton through the authenticated HTTP seam."""

import asyncio

import pytest

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
    }
    assert body["totals"]["energy_kwh"] is None
    assert response.headers["cache-control"] == "private, max-age=30"
    assert (
        await client.get(BASE, params=WINDOW, headers={"If-None-Match": response.headers["etag"]})
    ).status_code == 304
    assert (await client.get(BASE, params=WINDOW, headers={"Authorization": ""})).status_code == 401
    assert (await client.get("/v1/vehicles/999/stats", params=WINDOW)).status_code == 404


async def test_empty_buckets_and_charging_skeleton(client):
    for kind in ("driving", "charging"):
        response = await client.get("/v1/vehicles/2/stats", params={**WINDOW, "kind": kind})
        assert response.status_code == 200
        body = response.json()
        assert len(body["buckets"]) == 3
        key = "drives" if kind == "driving" else "sessions"
        assert body["totals"][key] == 0
        for bucket in body["buckets"]:
            assert bucket[key] == 0
            assert set(bucket) == {"bucket_start", *body["totals"]}
        if kind == "driving":
            assert body["totals"]["distance_km"] == 0
        else:
            assert set(body["totals"]) == {
                "sessions",
                "energy_added_kwh",
                "cost",
                "cost_per_kwh",
                "ac_energy_kwh",
                "dc_energy_kwh",
                "dc_energy_share",
            }
            assert body["totals"]["cost_per_kwh"] is None
    charging = (await client.get(BASE, params={**WINDOW, "kind": "charging"})).json()
    assert charging["totals"]["sessions"] == 8


async def test_dst_and_week_calendar_boundaries(client):
    params = {
        **WINDOW,
        "bucket": "day",
        "tz": "Europe/Berlin",
        "from": "2026-03-28T23:00:00Z",
        "to": "2026-03-29T22:00:00Z",
    }
    body = (await client.get(BASE, params=params)).json()
    assert len(body["buckets"]) == 1
    assert body["buckets"][0]["bucket_start"] == "2026-03-28T23:00:00Z"
    assert body["totals"]["drives"] == 2
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


async def test_one_aggregate_query_and_no_server_cache(app, client, db, database_dsn):
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
            assert len([query for query in queries if f"FROM {table} " in query]) == 1
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
