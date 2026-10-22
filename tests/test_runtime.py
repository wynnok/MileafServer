"""Runtime contracts exercised through HTTP and the executable."""

import logging
import os
import subprocess
import sys

import httpx

from mileaf.app import create_app
from mileaf.config import Settings


def test_missing_database_dsn_fails_without_disclosing_environment() -> None:
    environment = {key: value for key, value in os.environ.items() if key != "MILEAF_DATABASE_DSN"}
    result = subprocess.run(
        [sys.executable, "-m", "mileaf"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "MILEAF_DATABASE_DSN" in result.stderr


async def test_unavailable_database_returns_safe_error(caplog) -> None:
    caplog.set_level(logging.INFO, logger="mileaf")
    dsn = "postgresql://reader:private-password@127.0.0.1:1/teslamate"
    app = create_app(Settings(database_dsn=dsn))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.get("/v1/health", headers={"Authorization": "Bearer secret-token"})
    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "db_unavailable", "message": "Database is unavailable"}
    }
    assert response.headers["x-request-id"]
    assert response.headers["x-request-id"] in caplog.text
    assert dsn not in caplog.text
    assert "private-password" not in caplog.text
    assert "secret-token" not in caplog.text


async def test_reachable_database_is_healthy(health_client: httpx.AsyncClient) -> None:
    response = await health_client.get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0"}


async def test_pool_connections_reject_writes_after_reuse(database_dsn: str, db, tmp_path) -> None:
    import asyncpg
    from fastapi import Depends

    from mileaf.dependencies import get_pool

    app = create_app(
        Settings(database_dsn=database_dsn, max_concurrency=1, state_file=tmp_path / "state.json")
    )

    # The HTTP probe uses the same dependency seam as every production route.
    @app.post("/write-probe")
    async def write_probe(pool=Depends(get_pool)) -> dict[str, str]:
        async with pool.acquire() as connection:
            try:
                await connection.execute("INSERT INTO cars (id) VALUES (999)")
            except asyncpg.ReadOnlySQLTransactionError:
                return {"status": "read_only"}
        return {"status": "write_allowed"}

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        await client.post("/v1/auth/setup", json={"password": "a long password"})
        login = await client.post("/v1/auth/login", json={"password": "a long password"})
        client.headers["Authorization"] = "Bearer " + login.json()["token"]
        assert (await client.post("/write-probe")).json() == {"status": "read_only"}
        assert (await client.post("/write-probe")).json() == {"status": "read_only"}


def test_invalid_database_dsn_exits_without_echoing_credentials() -> None:
    environment = {**os.environ, "MILEAF_DATABASE_DSN": "private-password-secret-token"}
    result = subprocess.run(
        [sys.executable, "-m", "mileaf"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "MILEAF_DATABASE_DSN" in result.stderr
    assert "private-password" not in result.stderr
    assert "secret-token" not in result.stderr
