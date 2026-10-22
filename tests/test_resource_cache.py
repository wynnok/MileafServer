"""All new endpoints preserve conditional and disabled-cache semantics."""

import pytest


@pytest.mark.parametrize(
    "path",
    [
        "/v1/vehicles/1/summary",
        "/v1/vehicles/1/drives",
        "/v1/drives/1",
        "/v1/drives/1/series",
        "/v1/vehicles/1/charging-sessions",
        "/v1/charging-sessions/1",
        "/v1/charging-sessions/1/series",
    ],
)
async def test_private_etag_cache_and_disabled_equivalence(client, app, path):
    first = await client.get(path)
    assert first.status_code == 200
    assert first.headers["cache-control"] == "private, max-age=30"
    second = await client.get(path, headers={"If-None-Match": first.headers["etag"]})
    assert second.status_code == 304
    assert second.content == b""
    assert second.headers["etag"] == first.headers["etag"]
    app.state.cache.ttl = 0
    disabled = await client.get(path)
    assert disabled.content == first.content
    assert disabled.headers["etag"] == first.headers["etag"]


async def test_database_statement_timeout_is_an_http_503(client, db):
    # Hold a lock only in the isolated fixture DB. The production read-only pool
    # must time out rather than waiting indefinitely on an upstream schema lock.
    async with db.transaction():
        await db.execute("LOCK TABLE drives IN ACCESS EXCLUSIVE MODE")
        response = await client.get("/v1/drives/1")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "upstream_timeout"


@pytest.mark.parametrize("identifier", [2147483648, 2**100])
async def test_unknown_ids_outside_database_key_range_return_404(client, identifier):
    for resource in ["drives", "charging-sessions"]:
        for suffix in ["", "/series"]:
            response = await client.get(f"/v1/{resource}/{identifier}{suffix}")
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "not_found"
    for vehicle_id in [32768, identifier]:
        for resource in ["summary", "drives", "charging-sessions"]:
            response = await client.get(f"/v1/vehicles/{vehicle_id}/{resource}")
            assert response.status_code == 404
