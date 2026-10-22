"""Real image/compose delivery checks; no registry access during tests."""

import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "mileaf-server:local"


def run(*args, input=None, env=None, timeout=60):
    return subprocess.run(
        args,
        input=input,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
        cwd=ROOT,
    )


@pytest.fixture
def deployment_image():
    try:
        result = run("docker", "image", "inspect", IMAGE)
    except FileNotFoundError:
        pytest.skip("Docker is required for deployment tests")
    if result.returncode:
        pytest.skip("Build mileaf-server:local before running deployment tests")
    return IMAGE


def test_image_is_non_root_clean_and_missing_dsn_is_readable(deployment_image):
    identity = run("docker", "run", "--rm", "--pull=never", "--entrypoint", "id", IMAGE, "-u")
    assert identity.returncode == 0
    assert identity.stdout.strip() == "10001"
    inspect = run(
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--entrypoint",
        "python",
        IMAGE,
        "-c",
        "from pathlib import Path; assert not Path('/data/state.json').exists(); "
        "assert not Path('/app/tests').exists(); assert not Path('/app/teslamate').exists(); "
        "assert not list(Path('/app').rglob('.env*')); "
        "assert not list(Path('/app').rglob('*.sql'))",
    )
    assert inspect.returncode == 0, inspect.stderr
    missing = run("docker", "run", "--rm", "--pull=never", IMAGE)
    assert missing.returncode != 0
    assert "Invalid or missing environment variables: MILEAF_DATABASE_DSN" in missing.stderr


async def test_documented_reader_and_compose_are_healthy(deployment_image, db, admin_dsn, tmp_path):
    # The psql client is supplied by the already-pulled fixture image.
    docker_admin_dsn = admin_dsn.replace("127.0.0.1", "host.docker.internal")
    installed = run(
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "-i",
        "postgres:16-alpine",
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        docker_admin_dsn,
        input=(ROOT / "deploy/mileaf-reader.sql").read_text(),
    )
    assert installed.returncode == 0, installed.stderr
    project = "mileaf-delivery-" + uuid4().hex[:12]
    override = tmp_path / "compose.test.yml"
    override.write_text(
        'services:\n  mileaf:\n    ports: !override ["127.0.0.1::8080"]\n'
        "    healthcheck:\n      interval: 1s\n      start_period: 1s\n"
    )
    environment = {
        **os.environ,
        "MILEAF_IMAGE": IMAGE,
        "MILEAF_DATABASE_DSN": docker_admin_dsn.replace("postgres@", "mileaf_reader@"),
    }
    compose = [
        "docker",
        "compose",
        "--project-name",
        project,
        "-f",
        str(ROOT / "compose.mileaf.yml"),
        "-f",
        str(override),
    ]
    try:
        privileges = await db.fetchval(
            "SELECT has_table_privilege('mileaf_reader', 'private.tokens', 'SELECT')"
        )
        assert privileges is False
        started = run(*compose, "up", "-d", "--wait", "--wait-timeout", "45", env=environment)
        assert started.returncode == 0, started.stderr
        mapping = run(*compose, "port", "mileaf", "8080", env=environment)
        assert mapping.returncode == 0
        async with httpx.AsyncClient(base_url="http://" + mapping.stdout.strip()) as client:
            health = await client.get("/v1/health")
            assert health.status_code == 200
            assert health.json()["status"] == "ok"
            assert (
                await client.post("/v1/auth/setup", json={"password": "deployment test password"})
            ).status_code == 200
            login = await client.post(
                "/v1/auth/login", json={"password": "deployment test password"}
            )
            meta = await client.get(
                "/v1/meta", headers={"Authorization": "Bearer " + login.json()["token"]}
            )
            assert meta.status_code == 200
            assert meta.json()["schema_ok"] is True
        state = run(
            *compose,
            "exec",
            "-T",
            "mileaf",
            "python",
            "-c",
            "from pathlib import Path; import stat; "
            "assert stat.S_IMODE(Path('/data/state.json').stat().st_mode)==0o600",
            env=environment,
        )
        assert state.returncode == 0, state.stderr
        status = run(*compose, "ps", "--format", "json", env=environment)
        assert json.loads(status.stdout)["Health"] == "healthy"
    finally:
        # Only this synthetic compose project's resources and role are removed.
        cleaned = run(*compose, "down", "--volumes", env=environment)
        await db.execute("DROP OWNED BY mileaf_reader")
        await db.execute("DROP ROLE mileaf_reader")
        assert cleaned.returncode == 0, cleaned.stderr
