"""Primitive errors use the application's existing HTTP envelope."""

from mileaf.queries.pagination import fetch_page
from mileaf.queries.series import fetch_series


async def test_primitive_parameter_errors_and_empty_pages(app, client, db):
    @app.get("/v1/page-probe")
    async def page_probe(limit: str = "20", cursor: str | None = None):
        page = await fetch_page(
            db,
            source="SELECT id,start_date AT TIME ZONE 'UTC' AS started_at "
            "FROM drives WHERE car_id=2",
            limit=limit,
            cursor=cursor,
        )
        return {"rows": page.rows, "next_cursor": page.next_cursor}

    @app.get("/v1/series-probe")
    async def series_probe(points: str = "200", fields: str = "speed"):
        return await fetch_series(
            db,
            table="positions",
            time_column="date",
            columns={"speed": "speed"},
            where="drive_id=13",
            points=points,
            fields=fields,
        )

    for path, params in [
        ("page-probe", {"limit": "0"}),
        ("page-probe", {"limit": "101"}),
        ("page-probe", {"limit": "abc"}),
        ("page-probe", {"cursor": "broken"}),
        ("series-probe", {"points": "1001"}),
        ("series-probe", {"fields": "unknown"}),
    ]:
        response = await client.get(f"/v1/{path}", params=params)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"
    page = await client.get("/v1/page-probe")
    assert page.status_code == 200
    assert page.json() == {"rows": [], "next_cursor": None}
    series = await client.get("/v1/series-probe")
    assert series.status_code == 200
    assert series.json() == {"series": [], "resolution": "empty"}


async def test_statement_timeout_is_safe_and_connection_recovers(app, client, db):
    # Six seconds is intentionally longer than the primitive's five-second budget.
    @app.get("/v1/slow-series-probe")
    async def slow_probe():
        return await fetch_series(
            db,
            table="positions",
            time_column="date",
            columns={"speed": "speed"},
            where="drive_id=1 AND (SELECT pg_sleep(6)) IS NOT NULL",
        )

    original_timeout = await db.fetchval("SHOW statement_timeout")
    response = await client.get("/v1/slow-series-probe")
    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "upstream_timeout", "message": "Database query timed out"}
    }
    assert await db.fetchval("SHOW statement_timeout") == original_timeout
    page = await fetch_page(
        db, source="SELECT id,start_date AT TIME ZONE 'UTC' AS started_at FROM drives"
    )
    assert len(page.rows) == 20
