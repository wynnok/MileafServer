"""Full-query statistics budgets against the opt-in isolated performance fixture."""

from time import perf_counter

import httpx
import pytest
from fastapi import FastAPI


@pytest.mark.slow
@pytest.mark.parametrize("view", ["usage", "conditions"])
async def test_driving_analysis_large_fixture(
    client: httpx.AsyncClient, app: FastAPI, performance_seed: int, view: str
) -> None:
    app.state.cache.ttl = 0
    started = perf_counter()
    response = await client.get(
        f"/v1/vehicles/{performance_seed}/driving-analysis",
        params={
            "view": view,
            "from": "2025-04-01T00:00:00Z",
            "to": "2026-03-15T12:00:00Z",
        },
    )
    elapsed = perf_counter() - started
    assert response.status_code == 200, response.text
    assert elapsed < 5, f"driving analysis {view} took {elapsed:.3f}s"
    body = response.json()
    summary = body if view == "usage" else body["summary"]
    assert summary["coverage"]["candidate_count"] == 20_000
    assert len(body["distance_bins"]) == 3
    if view == "conditions":
        assert len(body["temperature_bins"]) <= 100
    else:
        assert len(body["weekday_bins"]) == 7


@pytest.mark.slow
async def test_parking_observations_large_fixture(
    client: httpx.AsyncClient, app: FastAPI, performance_seed: int
) -> None:
    app.state.cache.ttl = 0
    started = perf_counter()
    response = await client.get(
        f"/v1/vehicles/{performance_seed}/parking-observations",
        params={
            "from": "2025-04-01T00:00:00Z",
            "to": "2026-03-15T12:00:00Z",
            "bucket": "month",
            "limit": 100,
        },
    )
    elapsed = perf_counter() - started
    assert response.status_code == 200, response.text
    assert elapsed < 5, f"parking observations took {elapsed:.3f}s"
    body = response.json()
    assert len(body["items"]) <= 100
    assert len(body["buckets"]) <= 12
    assert body["matched_windows"] > 10_000
