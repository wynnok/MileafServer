"""Opt in with `UV_CACHE_DIR=.cache/uv uv run pytest -m slow`; see fixture docs."""

from math import ceil
from pathlib import Path
from time import perf_counter

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from mileaf.config import Settings
from mileaf.database import create_pool

pytestmark = pytest.mark.slow


async def test_performance_seed_is_reproducible_after_rollback(db: asyncpg.Connection) -> None:
    from tests.fixtures.performance import load_performance_seed

    async def fingerprints() -> list[str]:
        return [
            await db.fetchval(
                f"SELECT md5(string_agg(md5(row::text), '' ORDER BY id)) FROM {table} row"
            )
            for table in ("drives", "positions", "charging_processes", "charges")
        ]

    transaction = db.transaction()
    await transaction.start()
    try:
        first_vehicle = await load_performance_seed(db)
        first = await fingerprints()
    finally:
        await transaction.rollback()
    assert await db.fetchval("SELECT count(*) FROM drives") == 50
    assert await load_performance_seed(db) == first_vehicle
    assert await fingerprints() == first


@pytest.mark.parametrize(
    ("endpoint", "params", "budget_ms", "samples", "metric"),
    [
        ("drives", {"limit": "20"}, 300, 20, "p95"),
        ("series", {"points": "200"}, 600, 3, "max"),
        (
            "stats",
            {
                "kind": "driving",
                "bucket": "month",
                "from": "2025-04-01T00:00:00Z",
                "to": "2026-04-01T00:00:00Z",
            },
            800,
            3,
            "max",
        ),
        ("summary", {}, 400, 3, "max"),
    ],
    ids=["drive-list", "drive-series", "monthly-stats", "summary"],
)
async def test_endpoint_performance(
    client: httpx.AsyncClient,
    app: FastAPI,
    performance_seed: int,
    endpoint: str,
    params: dict[str, str],
    budget_ms: int,
    samples: int,
    metric: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tests.fixtures.performance import FIRST_DRIVE_ID

    app.state.cache.ttl = 0
    url = (
        f"/v1/drives/{FIRST_DRIVE_ID}/series"
        if endpoint == "series"
        else f"/v1/vehicles/{performance_seed}/{endpoint}"
    )
    timings = []
    responses = []
    for _ in range(samples):
        start = perf_counter()
        response = await client.get(url, params=params)
        timings.append((perf_counter() - start) * 1000)
        responses.append(response)
    measured = sorted(timings)[ceil(0.95 * samples) - 1] if metric == "p95" else max(timings)
    diagnostic = (
        f"GET {url} {params} {metric}={measured:.2f}ms budget={budget_ms}ms "
        f"delta={measured - budget_ms:+.2f}ms n={samples} cache=off"
    )
    # Show successful measurements even under pytest's default output capture.
    with capsys.disabled():
        print(diagnostic, flush=True)
    assert all(response.status_code == 200 for response in responses), (
        f"{diagnostic}; statuses={[response.status_code for response in responses]}"
    )
    for response in responses:
        body = response.json()
        if endpoint == "drives":
            assert len(body["items"]) == 20
            assert body["next_cursor"] is not None
        elif endpoint == "series":
            assert body["points"] == len(body["series"]) == 200
            assert body["resolution"] == "server_sampled"
        elif endpoint == "stats":
            assert len(body["buckets"]) == 12
            assert body["totals"]["drives"] == 20_000
            # The second, coverage aggregate keeps the same match set at fixture scale.
            assert body["totals"]["driving_coverage"]["completed_drives"] == 20_000
            assert body["totals"]["net_consumption_paired"]["eligible_drives"] == 20_000
            assert body["totals"]["comparison_quality"]["sample_status"] == "adequate"
        else:
            assert body["vehicle"]["id"] == performance_seed
            assert body["battery"] is not None
            assert body["period"]["month"]["drives"] > 0
    assert measured < budget_ms if endpoint == "series" else measured <= budget_ms, diagnostic


async def test_runtime_transactions_reject_writes_with_performance_data(
    performance_seed: int, admin_dsn: str, tmp_path: Path
) -> None:
    # Use an owner connection so role privileges cannot mask missing read-only protection.
    pool = await create_pool(
        Settings(database_dsn=admin_dsn, state_file=tmp_path / "state.json", max_concurrency=1)
    )
    try:
        for _ in range(2):
            async with pool.acquire() as connection:
                assert (
                    await connection.fetchval(
                        "SELECT count(*) FROM drives WHERE car_id=$1", performance_seed
                    )
                    == 20_000
                )
                with pytest.raises(asyncpg.ReadOnlySQLTransactionError):
                    async with connection.transaction():
                        await connection.execute(
                            "UPDATE drives SET distance=0 WHERE car_id=$1", performance_seed
                        )
    finally:
        await pool.close()


async def test_performance_seed_has_requested_scale(
    db: asyncpg.Connection, performance_seed: int
) -> None:
    vehicle_id = performance_seed
    for table, expected in (
        ("drives", 20_000),
        ("positions", 200_000),
        ("charging_processes", 5_000),
    ):
        assert (
            await db.fetchval(f"SELECT count(*) FROM {table} WHERE car_id=$1", vehicle_id)
            == expected
        )
    assert (
        await db.fetchval(
            "SELECT count(*) FROM charges c JOIN charging_processes cp "
            "ON cp.id=c.charging_process_id WHERE cp.car_id=$1",
            vehicle_id,
        )
        == 100_000
    )
    assert (
        await db.fetchval(
            "SELECT max(n) FROM (SELECT count(*) n FROM positions WHERE car_id=$1 "
            "GROUP BY drive_id) counts",
            vehicle_id,
        )
        == 20_000
    )
