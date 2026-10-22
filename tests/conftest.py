"""PostgreSQL is opt-in via fixture dependency, so unit tests never require Docker."""

import asyncio
import subprocess
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from mileaf.app import create_app
from mileaf.config import Settings
from mileaf.dependencies import get_now
from tests.fixtures.seeds import load_seeds

FIXTURE_ROOT = Path(__file__).parent / "fixtures"
FIXED_NOW = datetime(2026, 3, 15, 12, tzinfo=UTC)
POSTGRES_IMAGE = "postgres:16-alpine"


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=60, check=False
    )


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[str]:
    """Start only an already-pulled image; no registry or Ryuk network access."""
    try:
        available = _docker("image", "inspect", POSTGRES_IMAGE)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pytest.skip("Docker is unavailable; PostgreSQL fixture requires Docker")
    if available.returncode:
        pytest.skip("Pre-pull postgres:16-alpine before running database tests")
    result = _docker(
        "run",
        "--detach",
        "--rm",
        "--pull=never",
        "--publish",
        "127.0.0.1::5432",
        "--env",
        "POSTGRES_HOST_AUTH_METHOD=trust",
        POSTGRES_IMAGE,
    )
    if result.returncode:
        pytest.fail("Could not start the fixture PostgreSQL container (check Docker)")
    container_id = result.stdout.strip()
    try:
        mapping = _docker("port", container_id, "5432/tcp")
        if mapping.returncode:
            pytest.fail("Could not discover the fixture PostgreSQL port")
        port = mapping.stdout.strip().rsplit(":", 1)[1]
        yield f"postgresql://postgres@127.0.0.1:{port}/postgres"
    finally:
        _docker("stop", "--time", "1", container_id)


async def _connect(dsn: str) -> asyncpg.Connection:
    for attempt in range(100):
        try:
            return await asyncpg.connect(dsn)
        except (OSError, asyncpg.CannotConnectNowError):
            if attempt == 99:
                raise
            await asyncio.sleep(0.1)
    raise AssertionError("unreachable")


@pytest.fixture
async def admin_dsn(postgres_container: str) -> AsyncIterator[str]:
    """Function-isolated admin database. Runtime applies its own read-only protection."""
    database_name = f"mileaf_test_{uuid4().hex}"
    admin = await _connect(postgres_container)
    await admin.execute(f'CREATE DATABASE "{database_name}"')
    try:
        yield postgres_container.rsplit("/", 1)[0] + "/" + database_name
    finally:
        try:
            await admin.execute(f'DROP DATABASE "{database_name}" WITH (FORCE)')
        finally:
            await admin.close()


@pytest.fixture
async def db(admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    connection = await _connect(admin_dsn)
    try:
        await connection.execute((FIXTURE_ROOT / "schema.sql").read_text())
        versions = [
            int(line) for line in (FIXTURE_ROOT / "migration_versions.txt").read_text().splitlines()
        ]
        await connection.executemany(
            "INSERT INTO schema_migrations(version,inserted_at) VALUES ($1,'2026-01-01')",
            [(version,) for version in versions],
        )
        await load_seeds(connection)
        yield connection
    finally:
        await connection.close()


@pytest.fixture
async def database_dsn(db: asyncpg.Connection, admin_dsn: str) -> AsyncIterator[str]:
    """A real non-owner reader, with no access to the credential schema."""
    role = f"reader_{uuid4().hex}"
    await db.execute(f'CREATE ROLE "{role}" LOGIN')
    try:
        await db.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
        await db.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{role}"')
        yield admin_dsn.replace("postgres@", role + "@")
    finally:
        await db.execute(f'DROP OWNED BY "{role}"')
        await db.execute(f'DROP ROLE "{role}"')


@pytest.fixture
def app(database_dsn: str, tmp_path: Path) -> FastAPI:
    application = create_app(
        Settings(database_dsn=database_dsn, state_file=tmp_path / "state.json")
    )
    application.dependency_overrides[get_now] = lambda: FIXED_NOW
    return application


@pytest.fixture
async def health_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Anonymous HTTP seam against the real seeded PostgreSQL database."""
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        yield client


@pytest.fixture
async def client(health_client: httpx.AsyncClient) -> httpx.AsyncClient:
    """Authenticate through the real setup/login boundary, without bypassing Bearer checks."""
    password = "fixture access password"
    setup = await health_client.post("/v1/auth/setup", json={"password": password})
    assert setup.status_code == 200
    login = await health_client.post("/v1/auth/login", json={"password": password})
    assert login.status_code == 200
    health_client.headers["Authorization"] = "Bearer " + login.json()["token"]
    return health_client


@pytest.fixture
async def performance_seed(request: pytest.FixtureRequest, db: asyncpg.Connection) -> int:
    """Import and load large data only for explicitly selected slow tests."""
    if request.node.get_closest_marker("slow") is None:
        raise pytest.UsageError("performance_seed requires the slow marker")
    from tests.fixtures.performance import load_performance_seed

    return await load_performance_seed(db)
