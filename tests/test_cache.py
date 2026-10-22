"""Cache and ETag behavior at their caller-facing boundaries."""

import httpx
import pytest
from fastapi import FastAPI, Request

from mileaf.cache import TTLCache, cached_response


@pytest.fixture
async def cache_client():
    app = FastAPI()
    cache = TTLCache(ttl=30, capacity=2)

    @app.get("/summary")
    async def summary(request: Request):
        async def render():
            return b'{"distance_km":100.0,"freshness":{"last_position_at":"2026-03-15T11:55:00Z"}}'

        return await cached_response(
            request, cache=cache, key=("summary", request.url.query), render=render, ttl=30
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, cache


async def test_conditional_request_returns_empty_304_and_retains_freshness(cache_client):
    client, cache = cache_client
    first = await client.get("/summary?tz=UTC")
    second = await client.get("/summary?tz=UTC")
    assert first.status_code == 200
    assert first.content == second.content
    assert second.json()["freshness"] == {"last_position_at": "2026-03-15T11:55:00Z"}
    assert first.headers["cache-control"] == "private, max-age=30"
    conditional = await client.get(
        "/summary?tz=UTC", headers={"If-None-Match": first.headers["etag"]}
    )
    assert conditional.status_code == 304
    assert conditional.content == b""
    assert conditional.headers["etag"] == first.headers["etag"]
    assert conditional.headers["cache-control"] == "private, max-age=30"
    assert cache.hits == 2
    assert cache.misses == 1


async def test_etag_covers_all_query_parameters_and_weak_list_matching(cache_client):
    client, _ = cache_client
    requests = [
        "/summary?tz=UTC&from=2026-01-01&to=2026-02-01",
        "/summary?tz=Asia/Shanghai&from=2026-01-01&to=2026-02-01",
        "/summary?tz=UTC&from=2026-01-02&to=2026-02-01",
        "/summary?tz=UTC&from=2026-01-01&to=2026-02-02",
    ]
    responses = [await client.get(url) for url in requests]
    assert len({response.headers["etag"] for response in responses}) == 4
    etag = responses[0].headers["etag"]
    for condition in (f'"other", W/{etag}', "*"):
        response = await client.get(requests[0], headers={"If-None-Match": condition})
        assert response.status_code == 304
        assert not response.content
    changed = await client.get(requests[1], headers={"If-None-Match": etag})
    assert changed.status_code == 200


def test_ttl_expiry_capacity_and_per_entry_lifetime():
    time = [0.0]
    cache = TTLCache(ttl=30, capacity=2, clock=lambda: time[0])
    cache.put(("vehicle", 1), b"first")
    cache.put(("vehicle", 2), b"second", ttl=10)
    assert len(cache) == 2
    assert cache.get(("vehicle", 1)) == b"first"
    cache.put(("vehicle", 3), b"third")
    assert len(cache) == 2
    assert cache.get(("vehicle", 2)) is None
    assert cache.get(("vehicle", 1)) == b"first"
    time[0] = 30
    assert cache.get(("vehicle", 1)) is None
    assert cache.get(("vehicle", 3)) is None
    cache.put("short", b"short", ttl=5)
    time[0] = 35
    assert cache.get("short") is None


async def test_disabling_cache_keeps_bytes_etag_and_data_time(cache_client):
    client, cache = cache_client
    first = await client.get("/summary")
    cache.ttl = 0
    second = await client.get("/summary")
    third = await client.get("/summary")
    assert first.content == second.content == third.content
    assert first.headers["etag"] == second.headers["etag"] == third.headers["etag"]
    assert second.json()["freshness"]["last_position_at"] == "2026-03-15T11:55:00Z"
    assert cache.misses == 3


async def test_cached_bytes_survive_time_and_changed_upstream_until_expiry():
    time = [0.0]
    cache = TTLCache(ttl=30, clock=lambda: time[0])
    app = FastAPI()
    upstream = [b'{"freshness":"2026-03-15T11:55:00Z"}']

    @app.get("/summary")
    async def summary(request: Request):
        async def render():
            return upstream[0]

        return await cached_response(request, cache=cache, key="summary", render=render, ttl=30)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.get("/summary")
        upstream[0] = b'{"freshness":"2026-03-15T11:56:00Z"}'
        time[0] = 29
        hit = await client.get("/summary")
        assert hit.content == first.content
        time[0] = 30
        refreshed = await client.get("/summary", headers={"If-None-Match": first.headers["etag"]})
        assert refreshed.status_code == 200
        assert refreshed.content == upstream[0]
        assert refreshed.headers["etag"] != first.headers["etag"]


@pytest.mark.parametrize("ttl,capacity", [(-1, 1), (30, 0)])
def test_invalid_cache_configuration_is_rejected(ttl, capacity):
    with pytest.raises(ValueError):
        TTLCache(ttl=ttl, capacity=capacity)
